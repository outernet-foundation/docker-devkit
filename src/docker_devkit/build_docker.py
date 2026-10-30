from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Annotated, Any, TypedDict

import typer
from bashrun.bash import bash, bash_check, bash_output
from build_artifact_registry.builds import push_build
from build_artifact_registry.setup_oras import install_oras

from .config import CHART_REFERENCE_PREFIX, DEFAULT_MIRROR_PREFIX, LOCK_PATH, chart_lock_key, load_config
from .documents import (
    KEEP_STORAGE_PATTERN,
    BakeMetadata,
    DaemonConfig,
    compute_service_shas,
    parse_image_lock,
    render_bake,
)
from .host import GPU_TYPES, Gpu, detect_gpu, host_platform
from .image_refs import ImageReference, bake_base_image_refs, resolve_remote_digest
from .mirror import mirrored_ref


METADATA_PATH = Path("metadata.json")

DIGEST_PROJECT = "images-digests"
DIGEST_PLATFORM = "all"
DIGEST_FILE_NAME = "images-digests.json"

DIGEST_TAIL_PATTERN = re.compile(r"@sha256:[a-f0-9]{64}$")


class DigestEntry(TypedDict):
    ref: str
    digest: str
    tags: list[str]


app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def build(
    push: Annotated[
        bool, typer.Option("--push", help="Push images (+ registry caches) and shelve the digest manifest.")
    ] = False,
    targets_opt: Annotated[
        list[str] | None,
        typer.Option("--targets", "-t", help="Build only these services (from the image manifest)."),
    ] = None,
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
    upgrade: bool = typer.Option(False, "--upgrade", "-u", help="Re-resolve and rewrite base digests."),
    lock_only: bool = typer.Option(False, "--lock-only", help="Update lock file without building images."),
    gpu_only: bool = typer.Option(
        False, "--gpu-only", help="Build only the services declaring this gpu family (requires a concrete --gpu)."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild by disabling cache usage."),
    dev: Annotated[
        bool, typer.Option("--dev", help="Build each Dockerfile-declared dev stage under the :dev tag (local-only).")
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="With --lock-only: assert the committed lock is fresh instead of writing it."),
    ] = False,
) -> None:
    targets = [service for value in targets_opt or [] for service in value.split()] or None
    run_build(
        push=push,
        targets_opt=targets,
        gpu=gpu,
        upgrade=upgrade,
        lock_only=lock_only,
        gpu_only=gpu_only,
        no_cache=no_cache,
        dev=dev,
        check=check,
    )


def run_build(
    *,
    push: bool = False,
    targets_opt: list[str] | None = None,
    gpu: Gpu = "auto",
    upgrade: bool = False,
    lock_only: bool = False,
    gpu_only: bool = False,
    no_cache: bool = False,
    dev: bool = False,
    check: bool = False,
) -> None:
    root = Path.cwd()
    config = load_config(root)
    if config is None:
        if not lock_only:
            raise RuntimeError("No docker-devkit.yaml at the repo root; build derives its targets from it")
        print("no docker-devkit.yaml; no lock declarations to relock")
        return

    if dev and push:
        raise typer.BadParameter("--dev builds local-only images under the :dev tag; drop --push")
    if dev and lock_only:
        raise typer.BadParameter("--lock-only writes the lock without building; --dev selects a build shape")
    if check and not lock_only:
        raise typer.BadParameter("--check asserts the lock the --lock-only lane would write; pass --lock-only")

    lock_file = root / LOCK_PATH
    mirror_prefix = config.mirror.prefix if config.mirror is not None else DEFAULT_MIRROR_PREFIX

    service_shas = compute_service_shas(root, config)
    os.environ.update(service_shas)

    lock_data = parse_image_lock(lock_file).root if lock_file.exists() else {}

    if not config.service_builds and not lock_only:
        raise RuntimeError(
            "no Dockerfile service derives under this repo; build builds the derived services "
            "(relock declarations without building via --lock-only)"
        )

    if push and gpu == "auto":
        raise typer.BadParameter("With --push, --gpu cannot be 'auto'; specify 'cuda' or 'rocm'.")

    if gpu_only and gpu not in GPU_TYPES:
        raise typer.BadParameter("--gpu-only requires a concrete gpu (cuda or rocm), not 'auto' or 'none'.")

    # Check Docker GC limits so local GPU builds don't evict the build cache.
    if not push and not lock_only and not targets_opt:
        _check_gc_limits(min_gb=60)

    # Record one lock entry per declaration: the mirror-prefixed reference, digest resolved
    # against upstream — crane copies are byte-identical, so the digest holds under both
    # names. The declared tag rides the entry, so a tag bump breaks the prefix match and
    # re-resolves while an unchanged declaration copies its frozen digest forward.
    declarations: list[ImageReference] = bake_base_image_refs(config)
    if config.k8s is not None:
        declarations += [
            ImageReference(chart_lock_key(name), reference.removeprefix(CHART_REFERENCE_PREFIX))
            for name, reference in config.k8s.charts.items()
        ]
    if lock_only and not declarations:
        print("no lock declarations (neither base_images nor k8s.charts); nothing to relock")
        return
    for occurrence in declarations:
        lock_base = mirrored_ref(occurrence.reference, mirror_prefix)
        if DIGEST_TAIL_PATTERN.search(occurrence.reference):
            lock_data[occurrence.name] = lock_base
            continue
        if not upgrade and occurrence.name in lock_data and lock_data[occurrence.name].startswith(lock_base + "@"):
            continue
        lock_data[occurrence.name] = f"{lock_base}@{resolve_remote_digest(occurrence.reference)}"

    # The lock owns exactly the declared names — an entry whose declaration died is an
    # orphan the up-time probe would still fail-closed over (an undeclared mirror digest
    # gating bring-up), so a relock prunes it.
    declared_names = {occurrence.name for occurrence in declarations}
    lock_data = {name: reference for name, reference in lock_data.items() if name in declared_names}

    lock_text = "\n".join(f"{key}={value}" for key, value in sorted(lock_data.items())) + "\n"

    if check:
        committed = lock_file.read_text(encoding="utf-8") if lock_file.exists() else ""
        if committed != lock_text:
            print(
                "".join(
                    difflib.unified_diff(
                        committed.splitlines(keepends=True),
                        lock_text.splitlines(keepends=True),
                        fromfile=f"committed {LOCK_PATH.as_posix()}",
                        tofile=f"computed {LOCK_PATH.as_posix()}",
                    )
                )
            )
            raise SystemExit("image lock is stale. Run 'build --lock-only' and commit the result.")
        return

    lock_file.write_text(lock_text, encoding="utf-8")

    if lock_only:
        return

    os.environ.update(lock_data)

    host = host_platform()

    target_variants = {
        target: variant for service in config.service_builds.values() for target, variant in service.target_variants
    }
    dev_targets = {
        target
        for service in config.service_builds.values()
        if service.dev_stage
        for target, _variant in service.target_variants
    }

    if dev and not dev_targets:
        raise RuntimeError(
            "no service declares a dev stage (a multi-stage Dockerfile carrying an AS dev stage); --dev builds those"
        )

    if targets_opt:
        requested = set(targets_opt)
        unknown = requested - set(target_variants)
        if unknown:
            raise typer.BadParameter(f"Unknown targets: {unknown}. Available: {sorted(target_variants)}")
        if dev:
            undeclared = sorted(requested - dev_targets)
            if undeclared:
                raise typer.BadParameter(f"--dev builds declared dev stages; no dev stage for {undeclared}")
        targets = [target for target in target_variants if target in requested]
    else:
        # Default targets are the native subset — the repo platforms are undeclared (the
        # host's arch) or include this host's arch — intersected with the gpu match over
        # the variant fan. An empty set refuses loudly: bake with no target arguments
        # builds every service, silently defeating the selection. Host-gpu detection lives
        # here, so an explicit --targets run never probes the host gpu.
        if gpu == "auto":
            gpu = detect_gpu()
        candidates = dev_targets if dev else set(target_variants)
        native = not config.platforms or host in config.platforms
        if gpu_only:
            targets = [
                target
                for target, variant in target_variants.items()
                if native and target in candidates and variant == gpu
            ]
        else:
            targets = [
                target
                for target, variant in target_variants.items()
                if native and target in candidates and variant in (None, gpu)
            ]
        if not targets:
            raise RuntimeError(
                f"No service targets this machine's arch ({host}) under gpu '{gpu}'; "
                f"name targets with --targets to cross-build. Services: {sorted(target_variants)}"
            )

    rendered = render_bake(config, push=push, host=host, dev=dev)

    # Drop stale metadata so the coverage check cannot pass against a previous run.
    METADATA_PATH.unlink(missing_ok=True)

    command_arguments: list[str] = [
        "--progress auto",
        "--provenance=false",
        "--sbom=false",
        "--push" if push else "--load",
    ]

    if no_cache:
        command_arguments.append("--no-cache")

    command_arguments.extend(targets)

    with TemporaryDirectory(prefix="bake-") as bake_dir:
        bake_file = Path(bake_dir) / "bake.yaml"
        bake_file.write_text(rendered, encoding="utf-8")
        _run_bake(bake_file, command_arguments, rendered)

    metadata_json: dict[str, Any] = (
        json.loads(METADATA_PATH.read_text(encoding="utf-8")) if METADATA_PATH.exists() else {}
    )
    baked = BakeMetadata.model_validate(metadata_json)
    if not set(targets) <= baked.root.keys():
        raise RuntimeError("Bake output does not cover the requested targets")

    # Shelve the digest manifest under the REGISTRY shelf, tagged with the checkout HEAD.
    # Push one shard per leg under sha-{sha}-{key}; parallel legs would last-writer-win a
    # shared tag, and the pull side merges a SHA's shards after every leg finishes.
    if push:
        registry = os.environ.get("REGISTRY", "")
        if not registry:
            raise SystemExit("REGISTRY not set — --push shelves the digest manifest under it")
        shelf = f"{registry}/builds"
        sha = bash_output("git rev-parse HEAD").strip()
        install_oras()
        digest_manifest: dict[str, DigestEntry] = {}
        for target in targets:
            image = baked.root[target]
            digest_manifest[target] = {"ref": image.ref, "digest": image.digest, "tags": image.tags}
        key = hashlib.sha256(",".join(sorted(targets)).encode("utf-8")).hexdigest()[:8]
        with TemporaryDirectory(prefix="digest-manifest-") as staging:
            manifest_path = Path(staging) / DIGEST_FILE_NAME
            manifest_path.write_text(json.dumps(digest_manifest, indent=2), encoding="utf-8")
            push_build(
                shelf,
                DIGEST_PROJECT,
                DIGEST_PLATFORM,
                f"sha-{sha}-{key}",
                Path(staging),
                [DIGEST_FILE_NAME],
            )


def _run_bake(bake_file: Path, command_arguments: list[str], rendered: str) -> None:
    # The temp bake file vanishes with its directory, so a failed bake re-raises with the
    # rendered content attached — the failure stays inspectable after the run.
    try:
        bash(
            " ".join(["docker buildx bake", f"-f {bake_file}", f"--metadata-file {METADATA_PATH}", *command_arguments])
        )
    except CalledProcessError as error:
        raise RuntimeError(
            f"docker buildx bake failed (exit {error.returncode}); rendered bake file:\n{rendered}"
        ) from error


def _check_gc_limits(min_gb: int = 60):
    candidates = [Path("/etc/docker/daemon.json"), Path(os.path.expanduser("~/.docker/daemon.json"))]
    # Probe the Windows-side home through wslpath; failure is expected off WSL.
    wslpath = 'wslpath $(cmd.exe /c "echo %UserProfile%" 2>/dev/null)'
    if os.getenv("WSL_DISTRO_NAME") is not None and bash_check(wslpath):
        win_home = bash_output(wslpath).strip()
        candidates.append(Path(win_home) / ".docker" / "daemon.json")

    config = next((p for p in candidates if p.exists() and os.access(p, os.R_OK)), None)
    if not config:
        print(f"    [WARN] No readable daemon.json found. Ensure defaultKeepStorage > {min_gb}GB.")
        return

    daemon = DaemonConfig.model_validate(
        json.loads(config.read_text(encoding="utf-8")), context={"path": config, "min_gb": min_gb}
    )
    raw = daemon.builder.gc.default_keep_storage
    match = KEEP_STORAGE_PATTERN.match(str(raw))
    if match is None:
        raise RuntimeError(f"Could not parse 'defaultKeepStorage' value: {raw}")

    multipliers = {"T": 1024, "G": 1, "M": 1 / 1024, "K": 1 / 1024**2, "B": 1 / 1024**3}
    unit = (match.group(2) or "B")[0].upper()
    gigabytes = float(match.group(1)) * multipliers.get(unit, 1 / 1024**3)

    if gigabytes < min_gb:
        raise RuntimeError(f"Docker GC limit {gigabytes:.1f}GB in {config} is below the required {min_gb}GB")

    print(f"    [OK] Docker GC Limit verified: {gigabytes:.1f}GB")
