from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from subprocess import CalledProcessError
from typing import Annotated, Any, Literal

import typer
import yaml
from bashrun.bash import bash, bash_output
from .detect_gpu import GPU_TYPES, Gpu, detect_gpu
from pydantic_settings import BaseSettings

from .context_sha import compute_service_shas
from .image_refs import bake_base_image_refs, compose_service_refs, resolve_remote_digest
from .modes import parse_env_file


class Settings(BaseSettings):
    wsl_distro_name: str | None = None


settings = Settings.model_validate({})

LOCK_FILE = Path(".env.lock")
ENV_SHAS_FILE = Path(".env.shas")
COMPOSE_FILE = Path("compose.yml")
DEFAULT_BAKE_FILE = Path("compose.bake.yml")
METADATA_PATH = Path("metadata.json")

Mode = Literal["local", "ci"]


app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def build(
    upgrade: bool = typer.Option(False, "--upgrade", "-u", help="Re-resolve and rewrite base digests."),
    lock_only: bool = typer.Option(False, "--lock-only", help="Update lock file without building images."),
    mode: Annotated[
        Mode, typer.Option("--mode", help="local: --load images; ci: --push images + registry caches.")
    ] = "local",
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
    gpu_only: bool = typer.Option(
        False, "--gpu-only", help="Build only the services suffixed for this gpu (requires a concrete --gpu)."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild by disabling cache usage."),
    targets_opt: Annotated[
        list[str] | None,
        typer.Option("--targets", "-t", help="Build only these services (from the selected bake file)."),
    ] = None,
    bake_file: Annotated[
        Path, typer.Option("--bake-file", help="Bake file to load (e.g. compose.bake.yml or compose.zed.bake.yml).")
    ] = DEFAULT_BAKE_FILE,
) -> None:
    run_build(
        upgrade=upgrade,
        lock_only=lock_only,
        mode=mode,
        gpu=gpu,
        gpu_only=gpu_only,
        no_cache=no_cache,
        targets_opt=targets_opt,
        bake_file=bake_file,
    )


def run_build(
    *,
    upgrade: bool = False,
    lock_only: bool = False,
    mode: Mode = "local",
    gpu: Gpu = "auto",
    gpu_only: bool = False,
    no_cache: bool = False,
    targets_opt: list[str] | None = None,
    bake_file: Path = DEFAULT_BAKE_FILE,
) -> None:
    service_shas = compute_service_shas(Path.cwd(), bake_file)
    os.environ.update(service_shas)

    # Tags of the images built this run — the local analog of .env.lock's pulled
    # digests — so raw `docker compose` can resolve the compose graph's ${*_SHA} holes.
    ENV_SHAS_FILE.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(service_shas.items())), encoding="utf-8"
    )

    # Read bake, compose, and lock files
    bake_data: dict[str, Any] = yaml.safe_load(bake_file.read_text(encoding="utf-8"))
    compose_data: dict[str, Any] = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    for include in compose_data.pop("include", []):
        include_path = COMPOSE_FILE.parent / (include if isinstance(include, str) else include["path"])
        included: dict[str, Any] = yaml.safe_load(include_path.read_text(encoding="utf-8"))
        compose_data.setdefault("services", {}).update(included.get("services", {}))
    lock_data = parse_env_file(LOCK_FILE) if LOCK_FILE.exists() else {}

    # TOOD: Create separate commands for ci and local modes so typer can do this validation instead of us
    if mode == "ci" and gpu == "auto":
        raise typer.BadParameter("In CI mode, --gpu cannot be 'auto'; specify 'cuda' or 'rocm'.")

    if gpu_only and gpu not in GPU_TYPES:
        raise typer.BadParameter("--gpu-only requires a concrete gpu (cuda or rocm), not 'auto' or 'none'.")

    # For local builds, ensure Docker GC limits are high enough that GPU builds don't cause cache evictions
    if mode == "local" and not lock_only and not targets_opt:
        _check_gc_limits(min_gb=60)

    # Resolve gpu
    if gpu == "auto" and not lock_only:
        gpu = detect_gpu()

    # Resolve base image external dependencies
    for occurrence in [ref for ref in bake_base_image_refs(bake_data) if upgrade or ref.name not in lock_data]:
        lock_data[occurrence.name] = (
            f"@{'' if '$' in occurrence.reference else resolve_remote_digest(occurrence.reference)}"
        )

    # Resolve third-party image external dependencies. x-image-ref marks services whose image
    # is sourced externally (vs. built from a bake file), so it's the right discriminator
    # regardless of how many bake files exist.
    third_party_images: dict[str, str] = {
        occurrence.name.upper().replace("-", "_") + "_IMAGE": occurrence.reference
        for occurrence in compose_service_refs(compose_data)
    }

    # Re-resolve when explicitly requested, when unseen, or when the image name in compose.yml
    # changed from what's in the lock file (e.g. postgres:16-alpine → postgis/postgres:16-3.4-alpine)
    for image, ref in {
        image: ref
        for image, ref in third_party_images.items()
        if upgrade or image not in lock_data or not lock_data[image].startswith(ref + "@")
    }.items():
        lock_data[image] = f"{ref}@{'' if '$' in ref else resolve_remote_digest(ref)}"

    # Update main lock file
    LOCK_FILE.write_text(
        "# Generated by lock.py\n" + "\n".join(f"{key}={value}" for key, value in sorted(lock_data.items())) + "\n",
        encoding="utf-8",
    )

    if lock_only:
        return

    # Update environment with external dependency image digests
    os.environ.update(lock_data)

    # Build command arguments
    command_arguments: list[str] = []

    # Determine bake targets
    if targets_opt:
        unknown = set(targets_opt) - set(bake_data["services"])
        if unknown:
            raise typer.BadParameter(f"Unknown targets: {unknown}. Available: {sorted(bake_data['services'])}")
        targets = [t for t in bake_data["services"] if t in set(targets_opt)]
    else:
        targets = compute_default_targets(bake_data, gpu, gpu_only)

    # Configure registry caches in CI mode
    if mode == "ci":
        for target in targets:
            target_cache = f"{bake_data['x-registry-cache']}:{target}"
            command_arguments.append(
                f"--set {target}.cache-to+=type=registry,ref={target_cache},mode=max,image-manifest=true,oci-mediatypes=true"
            )
            command_arguments.append(f"--set {target}.cache-from+=type=registry,ref={target_cache}")

    # `docker buildx --load` only writes a single arch to the host image store,
    # so bake targets declared multi-platform get overridden to host arch in
    # local builds. CI uses `--push` and produces the full manifest list.
    # Override key is `platform` (singular) even though the bake field is plural.
    if mode == "local":
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            host_platform = "linux/amd64"
        elif machine in ("aarch64", "arm64"):
            host_platform = "linux/arm64"
        else:
            raise RuntimeError(f"Unsupported host architecture: {machine}")
        for target in targets:
            target_platforms = bake_data["services"][target].get("build", {}).get("platforms", [])
            if len(target_platforms) > 1:
                command_arguments.append(f"--set {target}.platform={host_platform}")

    # Load or push images based on mode
    command_arguments.append("--load" if mode == "local" else "--push")

    # Handle no-cache option
    if no_cache:
        command_arguments.append("--no-cache")

    # Append targets
    command_arguments.extend(targets)

    # Clean up any existing metadata file
    METADATA_PATH.unlink(missing_ok=True)

    # Bake images
    command = [
        "docker buildx bake",
        f"-f {bake_file}",
        f"--metadata-file {METADATA_PATH}",
        "--progress auto",
        "--provenance=false",
        "--sbom=false",
    ] + command_arguments
    bash(" ".join(command))

    # Sanity check
    baked_images: dict[str, Any] = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else {}
    if not set(targets) <= baked_images.keys():
        raise RuntimeError("Baked images do not match target images; something went wrong during the bake.")


# Vibe code - Gemini 3
def _check_gc_limits(min_gb: int = 60):
    candidates = [Path("/etc/docker/daemon.json"), Path(os.path.expanduser("~/.docker/daemon.json"))]
    if settings.wsl_distro_name is not None:
        try:
            win_home = bash_output('wslpath $(cmd.exe /c "echo %UserProfile%" 2>/dev/null)').strip()
            candidates.append(Path(win_home) / ".docker" / "daemon.json")
        except CalledProcessError:
            pass

    config = next((p for p in candidates if p.exists() and os.access(p, os.R_OK)), None)
    if not config:
        print(f"    [WARN] No readable daemon.json found. Ensure defaultKeepStorage > {min_gb}GB.")
        return

    data = json.loads(config.read_text())
    raw = data.get("builder", {}).get("gc", {}).get("defaultKeepStorage")
    if not raw:
        sample = json.dumps({"builder": {"gc": {"defaultKeepStorage": f"{min_gb}GB"}}}, indent=2)
        raise RuntimeError(
            f"Missing 'builder.gc.defaultKeepStorage' in {config}; Docker's default is too low for GPU builds "
            f"(need >= {min_gb}GB). Add the following (merging into any existing keys) and restart Docker:\n\n{sample}"
        )

    m = re.match(r"^(\d+(?:\.\d+)?)\s*([TGMK]i?B)?$", str(raw), re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Could not parse 'defaultKeepStorage' value: {raw}")

    mult = {"T": 1024, "G": 1, "M": 1 / 1024, "K": 1 / 1024**2, "B": 1 / 1024**3}
    unit = (m.group(2) or "B")[0].upper()
    val = float(m.group(1)) * mult.get(unit, 1 / 1024**3)

    if val < min_gb:
        raise RuntimeError(
            f"UNSAFE GC LIMIT: {val:.1f}GB in {config} (Required: {min_gb}GB). RESTART DOCKER AFTER FIXING."
        )

    print(f"    [OK] Docker GC Limit verified: {val:.1f}GB")


# x-cross-compile-targets: services declared here are excluded from the default
# target list because they need special (usually per-arch) treatment; an operator
# can still opt them in explicitly via --targets. The field is a top-level bake key.
# Services without build.tags (e.g. neural-networks-base-*) stay out too: they are
# build-only dependencies pulled in via additional_contexts, and bake rejects a
# tagless target under --push.
def compute_default_targets(bake_data: dict[str, Any], gpu: Gpu, gpu_only: bool = False) -> list[str]:
    cross_compile_targets: set[str] = set(bake_data.get("x-cross-compile-targets", []))
    tagged_services = [
        service for service, config in bake_data["services"].items() if config.get("build", {}).get("tags")
    ]
    if gpu_only:
        return [
            service
            for service in tagged_services
            if service.endswith(f"-{gpu}") and service not in cross_compile_targets
        ]
    return [
        service
        for service in tagged_services
        if (not any(service.endswith(f"-{g}") for g in GPU_TYPES) or service.endswith(f"-{gpu}"))
        and service not in cross_compile_targets
    ]


def main() -> None:
    app()


if __name__ == "__main__":
    main()
