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
from .modes import resolve_auth_mode

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

    if build:
        run_build(gpu=gpu)

    if manifest is not None:
        os.environ.update(compute_service_shas(root, parse_bake(manifest)))

    compose_files = expand_compose_files(config, gpu, include_dev=dev)
    if not compose_files:
        raise RuntimeError("No compose files declared; a builds-only repo has no stack to bring up")

    files_args = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    profile_flag = "--profile keycloak " if auth_mode == "keycloak" else ""
    compose_args = f"{files_args} {profile_flag}--project-directory {root} --env-file .env --env-file {lock_file}"

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
