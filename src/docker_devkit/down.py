import os
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff
from .detect_gpu import Gpu, detect_gpu

from .context_sha import compute_service_shas
from .documents import parse_bake
from .lifecycle import (
    enforce_bake_declaration,
    enumerate_profiles,
    expand_compose_files,
    load_lifecycle_config,
    resolve_lock,
    resolve_manifest,
)

ENV_FILE = Path(".env")

app = typer.Typer(add_completion=False)


@app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes."),
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
) -> None:
    root = Path.cwd()
    config = load_lifecycle_config(root)
    enforce_bake_declaration(root, config)
    manifest = resolve_manifest(root)
    lock_file = resolve_lock(root)

    if not ENV_FILE.exists():
        raise RuntimeError("No .env file found")

    if not lock_file.exists():
        raise RuntimeError(
            f"No image lock at {lock_file}; run 'uv run build --lock-only' in the repo that authors the images"
        )

    if gpu == "auto":
        gpu = detect_gpu()

    if manifest is not None:
        os.environ.update(compute_service_shas(root, parse_bake(manifest)))

    # Always layer the dev overlay so containers from a prior --dev bring-up get torn down too
    compose_files = expand_compose_files(config, gpu, include_dev=True)
    if not compose_files:
        raise RuntimeError("No compose files declared; a builds-only repo has no stack to tear down")

    files_args = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    # Every profile declared across the assembly, so teardown covers containers from a prior
    # bring-up under a different COMPOSE_PROFILES selection
    compose_prefix = f"docker compose {files_args} --project-directory {root} --env-file .env --env-file {lock_file}"
    profile_flags = "".join(f"--profile {profile} " for profile in enumerate_profiles(compose_prefix))
    command = f"{compose_prefix} {profile_flags}down --remove-orphans"

    if volumes:
        command += " -v"

    bash_handoff(command)


def main():
    app()


if __name__ == "__main__":
    main()
