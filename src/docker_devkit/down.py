import os
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff
from .detect_gpu import Gpu, detect_gpu

from .context_sha import compute_service_shas
from .modes import resolve_auth_mode

ENV_FILE = Path(".env")
LOCK_FILE = Path(".env.lock")
BAKE_FILE = Path("compose.bake.yml")

app = typer.Typer(add_completion=False)


@app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes."),
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
    compose_file: Annotated[
        Path,
        typer.Option(
            "--compose-file",
            help=(
                "Base compose file. In a repo that authors its own stack (where compose.bake.yml lives) the default "
                "compose.yml tears down the native multi-file stack. A consumer stack is torn down as the single graph "
                "it was brought up as."
            ),
        ),
    ] = Path("compose.yml"),
) -> None:
    # Mirror up: native multi-file teardown only when compose.bake.yml is present and the
    # default compose.yml was requested. A consumer stack tears down its single graph.
    native = compose_file == Path("compose.yml") and BAKE_FILE.exists()

    if not ENV_FILE.exists():
        raise RuntimeError("No .env file found")

    if native and not LOCK_FILE.exists():
        raise RuntimeError("No lock file found; run 'uv run build --lock-only' first")

    if gpu == "auto":
        gpu = detect_gpu()

    resolve_auth_mode(ENV_FILE)

    if BAKE_FILE.exists():
        os.environ.update(compute_service_shas(Path.cwd(), BAKE_FILE))

    if native:
        compose_files = (
            "-f compose.yml "
            "-f compose.postgres.yml "
            f"{f'-f compose.{gpu}.yml ' if gpu != 'none' else ''}"
            "-f compose.dev.yml "  # Include so containers from a prior dev bring-up get torn down even with --no-dev later
        )
    else:
        compose_files = f"-f {compose_file} "

    # .env.lock keeps compose from erroring on missing stack-internal vars; it only
    # exists in the native repo, so a consumer stack tears down with .env alone.
    lock_flag = f"--env-file {LOCK_FILE} " if LOCK_FILE.exists() else ""
    command = (
        "docker compose "
        f"{compose_files}"
        "--profile keycloak "  # Always include so any keycloak containers from a previous AUTH_MODE=keycloak run get torn down
        "--env-file .env "
        f"{lock_flag}"
        "down --remove-orphans"
    )

    if volumes:
        command += " -v"

    bash_handoff(command)


def main():
    app()


if __name__ == "__main__":
    main()
