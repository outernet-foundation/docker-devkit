import os
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff
from .detect_gpu import Gpu, detect_gpu

from .context_sha import compute_service_shas
from .lifecycle import BAKE_FILE, enforce_bake_declaration, expand_compose_files, load_lifecycle_config
from .modes import resolve_auth_mode

ENV_FILE = Path(".env")
LOCK_FILE = Path(".env.lock")

app = typer.Typer(add_completion=False)


@app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes."),
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
) -> None:
    config = load_lifecycle_config(Path.cwd())
    enforce_bake_declaration(Path.cwd(), config)

    if not ENV_FILE.exists():
        raise RuntimeError("No .env file found")

    if not LOCK_FILE.exists():
        raise RuntimeError("No .env.lock found; run 'uv run build --lock-only' in the repo that authors the images")

    if gpu == "auto":
        gpu = detect_gpu()

    resolve_auth_mode(ENV_FILE)

    if BAKE_FILE.exists():
        os.environ.update(compute_service_shas(Path.cwd(), BAKE_FILE))

    # Always layer the dev overlay so containers from a prior --dev bring-up get torn down too
    compose_files = expand_compose_files(config, gpu, include_dev=True)
    if not compose_files:
        raise RuntimeError("No compose files declared; a builds-only repo has no stack to tear down")

    files_args = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    command = (
        "docker compose "
        f"{files_args} "
        "--profile keycloak "  # Always include so any keycloak containers from a previous AUTH_MODE=keycloak run get torn down
        "--env-file .env "
        f"--env-file {LOCK_FILE} "
        "down --remove-orphans"
    )

    if volumes:
        command += " -v"

    bash_handoff(command)


def main():
    app()


if __name__ == "__main__":
    main()
