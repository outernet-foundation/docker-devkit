# docker-devkit

Docker-stack lifecycle commands — `up`, `down`, `build` — with a native/consumer split. A repo that authors its own stack (Dockerfiles + `compose.bake.yml`) uses the native mode: multi-file compose assembly, per-service SHA injection, `.env.lock` resolution. A consumer repo that OCI-includes an already-baked upstream stack uses the same commands as a thin wrapper: single-graph, `.env`-only, no build path.

See [`AGENTS.md`](./AGENTS.md) for the file/layout conventions the commands assume (`compose.bake.yml`, `.env.lock`, `.env.shas`, `PUBLIC_URL` / `AUTH_MODE` in `.env`) and the mode-detection contract.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

From a stack repo's root:

```bash
uv run up                      # docker compose up (auto-detects GPU; native or consumer per compose.bake.yml)
uv run up --build              # build images locally first (native only)
uv run up --gpu none           # override GPU auto-detection
uv run down                    # docker compose down
uv run down -v                 # also remove named volumes
uv run build                   # cross-build all images per compose.bake.yml
uv run build --lock-only       # refresh .env.lock without building
```

## Consuming from another repo

Install from PyPI and use its entry points:

```toml
[project]
dependencies = ["docker-devkit>=0.1.0"]
```

`bashrun` resolves transitively from PyPI. To test an unreleased change, pin the repo at a git ref in a scratch branch instead (`docker-devkit = { git = "…", rev = "<sha>" }` under `[tool.uv.sources]`) and drop the pin when the release lands.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest
```
