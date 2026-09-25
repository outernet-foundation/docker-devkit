import os
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff
from .detect_gpu import Gpu, detect_gpu

from .build_docker import run_build
from .context_sha import compute_service_shas
from .documents import parse_bake
from .lifecycle import (
    enforce_bake_declaration,
    expand_compose_files,
    load_lifecycle_config,
    resolve_lock,
    resolve_manifest,
)
from .mirror import load_mirror_config, upstream_fallback, write_fallback_overlay
from .modes import parse_env_file, resolve_auth_mode

ENV_FILE = Path(".env")

app = typer.Typer(add_completion=False)


@app.command()
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
    allow_upstream_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-upstream-fallback",
            help="Pull unmirrored lock entries from upstream at the same digest instead of failing closed.",
        ),
    ] = False,
) -> None:
    root = Path.cwd()
    config = load_lifecycle_config(root)
    enforce_bake_declaration(root, config)
    manifest = resolve_manifest(root)
    lock_file = resolve_lock(root, manifest)

    if dev and (config is None or config.dev_file is None):
        raise RuntimeError("--dev requires a dev_file in [tool.docker-devkit.lifecycle]")

    if not ENV_FILE.exists():
        raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

    if not lock_file.exists():
        raise RuntimeError(
            f"No image lock at {lock_file}; run 'uv run build --lock-only' in the repo that authors the images"
        )

    if build and manifest is None:
        raise typer.BadParameter("--build requires an image manifest (workloads/images.yml)")

    if gpu == "auto":
        gpu = detect_gpu()

    auth_mode = resolve_auth_mode(ENV_FILE)

    substitutions: dict[str, str] = {}
    fallback_overlay: Path | None = None
    mirror_config = load_mirror_config(root)
    if mirror_config is not None:
        substitutions = upstream_fallback(
            parse_env_file(lock_file), mirror_config.prefix, allow_upstream_fallback=allow_upstream_fallback
        )
        if substitutions:
            for name, upstream in sorted(substitutions.items()):
                print(f"Falling back to upstream for {name}: {upstream}", flush=True)
            os.environ.update(substitutions)
            fallback_overlay = write_fallback_overlay(substitutions)

    if build:
        run_build(gpu=gpu, env_overlay=substitutions)

    if manifest is not None:
        os.environ.update(compute_service_shas(root, parse_bake(manifest)))

    compose_files = expand_compose_files(config, gpu, include_dev=dev)
    if not compose_files:
        raise RuntimeError("No compose files declared; a builds-only repo has no stack to bring up")

    files_args = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    profile_flag = "--profile keycloak " if auth_mode == "keycloak" else ""
    compose_args = f"{files_args} {profile_flag}--project-directory {root} --env-file .env --env-file {lock_file}"
    if fallback_overlay is not None:
        compose_args += f" --env-file {fallback_overlay}"

    up_command = f"docker compose {compose_args} up"
    if not build:
        # tree-<sha> tags are content-addressed and may not be pushed, so pull only what is missing locally
        up_command += " --pull missing"
        if quiet_pull:
            up_command += " --quiet-pull"
    if not attached:
        up_command += " -d"

    bash_handoff(up_command)


def main():
    app()


if __name__ == "__main__":
    main()
