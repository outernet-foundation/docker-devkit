import os
import re
import shlex
from os import environ
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from .build_docker import run_build
from .config import load_config
from .documents import compute_service_shas, parse_bake
from .lifecycle import (
    DEFAULT_COMPOSE_FILE,
    GPU_PLACEHOLDER,
    Gpu,
    LOCK_PATH,
    MANIFEST_PATH,
    detect_gpu,
    enumerate_profiles,
    parse_env_file,
    resolve_manifest,
)
from .mirror import load_mirror_config

ENV_FILE = Path(".env")

_MANIFEST_ABSENT_PATTERN = re.compile(r"\b404\b|not found|manifest unknown", re.IGNORECASE)

up_app = typer.Typer(add_completion=False)
down_app = typer.Typer(add_completion=False)


@up_app.command()
def up(
    attached: bool = typer.Option(False, "--attached", "-a", help="Run in foreground (not detached)"),
    quiet_pull: bool = typer.Option(
        False,
        "--quiet-pull",
        "-q",
        help="Suppress per-layer pull progress (still shows pull/push totals).",
    ),
    build: bool = typer.Option(
        False, "--build", help="Build all images locally before bringing the stack up; skips pulling"
    ),
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
    dev: bool = typer.Option(
        False,
        "--dev",
        help="Layer the declared dev overlay (compose.dev.yml shape) over the production stack for bind-mount/debug bring-up.",
    ),
) -> None:
    root = Path.cwd()
    manifest, compose_prefix = _compose_invocation(root, gpu, include_dev=dev, require_dev=dev)

    if build and manifest is None:
        raise typer.BadParameter("--build requires an image manifest (workloads/images.yml)")

    if load_mirror_config(root) is not None:
        # Fail closed over every lock entry, distinguishing a manifest the registry truly lacks
        # (the declaring change has not been through CI's mirror job; no escape flag) from a
        # probe that failed before reaching a verdict (credentials, network, daemon availability).
        for value in sorted(parse_env_file(root / LOCK_PATH).values()):
            message = _mirror_probe_message(value)
            if message is None:
                continue
            raise RuntimeError(message)

    if build:
        run_build(gpu=gpu)

    # compose ignores COMPOSE_PROFILES whenever a --profile flag is present, so forward the
    # selection explicitly; shell env wins over .env, comma-split and deduplicated.
    file_values = parse_env_file(ENV_FILE)
    raw_profiles = environ.get("COMPOSE_PROFILES") or file_values.get("COMPOSE_PROFILES", "")
    profiles: list[str] = []
    for entry in raw_profiles.split(","):
        profile = entry.strip()
        if profile and profile not in profiles:
            profiles.append(profile)
    if profiles:
        defined = enumerate_profiles(compose_prefix)
        unknown = [profile for profile in profiles if profile not in defined]
        if unknown:
            raise RuntimeError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )
    profile_flags = "".join(f"--profile {profile} " for profile in profiles)
    up_command = f"{compose_prefix} {profile_flags}up"
    if not build:
        # tree-<sha> tags are content-addressed and may not be pushed, so pull only what is missing locally
        up_command += " --pull missing"
        if quiet_pull:
            up_command += " --quiet-pull"
    if not attached:
        up_command += " -d"

    bash_handoff(up_command)


@down_app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes."),
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
) -> None:
    root = Path.cwd()
    # Always layer the dev overlay so a prior --dev bring-up tears down too
    _, compose_prefix = _compose_invocation(root, gpu, include_dev=True, require_dev=False)
    # Activate every declared profile so teardown covers prior bring-ups under a different selection
    profile_flags = "".join(f"--profile {profile} " for profile in enumerate_profiles(compose_prefix))
    command = f"{compose_prefix} {profile_flags}down --remove-orphans"
    if volumes:
        command += " -v"

    bash_handoff(command)


def _compose_invocation(root: Path, gpu: Gpu, *, include_dev: bool, require_dev: bool) -> tuple[Path | None, str]:
    # Run the shared preamble: enforce the declaration, refuse a missing .env or image lock,
    # resolve the gpu, inject tree-sha tags, and assemble the compose command prefix.
    devkit = load_config(root)
    lifecycle_config = devkit.lifecycle if devkit is not None else None
    if lifecycle_config is None and (root / MANIFEST_PATH).exists():
        raise RuntimeError(
            "image manifest present without a lifecycle section in docker-devkit.yaml; declare the section"
        )
    manifest = resolve_manifest(root)
    lock_file = root / LOCK_PATH

    if require_dev and (lifecycle_config is None or lifecycle_config.dev_file is None):
        raise RuntimeError("--dev requires a dev_file in the lifecycle section of docker-devkit.yaml")

    if not ENV_FILE.exists():
        raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

    if not lock_file.exists():
        raise RuntimeError(
            f"No image lock at {lock_file}; run 'uv run build --lock-only' in the repo that authors the images"
        )

    if gpu == "auto":
        gpu = detect_gpu()

    if manifest is not None:
        os.environ.update(compute_service_shas(root, parse_bake(manifest)))

    if lifecycle_config is None:
        compose_files = [DEFAULT_COMPOSE_FILE]
    else:
        compose_files = [
            Path(entry.format(gpu=gpu))
            for entry in lifecycle_config.files
            if gpu != "none" or GPU_PLACEHOLDER not in entry
        ]
        if include_dev and lifecycle_config.dev_file is not None:
            compose_files.append(Path(lifecycle_config.dev_file))
    if not compose_files:
        raise RuntimeError("No compose files declared; a builds-only repo has no stack")

    files_args = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    prefix = f"docker compose {files_args} --project-directory {root} --env-file .env --env-file {lock_file}"
    return manifest, prefix


def _mirror_probe_message(value: str) -> str | None:
    reference, _, digest = value.rpartition("@")
    try:
        bash_output(f"docker buildx imagetools inspect {shlex.quote(value)}")
    except CalledProcessError as error:
        stderr = (error.stderr or str(error)).strip()
        if _MANIFEST_ABSENT_PATTERN.search(stderr):
            return (
                f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored yet; "
                "the CI mirror job populates it when the declaring change runs"
            )
        return (
            f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe "
            "(docker buildx imagetools inspect) failed before it could determine whether the image "
            "exists, so this is not evidence the image is missing; check registry credentials, network, "
            f"and docker daemon availability. Probe error: {stderr}"
        )
    return None
