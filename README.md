# docker-devkit

Docker-stack lifecycle commands — `up`, `down`, `build` — with the compose assembly declared per repo in `[tool.docker-devkit.lifecycle]`. A repo that authors its own stack (Dockerfiles + `workloads/images.yml`) declares its file layers and gets multi-file assembly, per-service SHA injection, and `workloads/images.lock` resolution; a consumer repo that OCI-includes an already-baked upstream stack declares a bare table and runs the same commands as a thin wrapper over its own `.env` + committed lock.

See [`AGENTS.md`](./AGENTS.md) for the file/layout conventions the commands assume (`workloads/images.yml` + `workloads/images.lock` — with the legacy root `compose.bake.yml` + `.env.lock` pair still resolved — and `PUBLIC_URL` / `AUTH_MODE` in `.env`) and the declaration contract.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

From a stack repo's root:

```bash
uv run up                      # docker compose up (auto-detects GPU; assembles the declared lifecycle files)
uv run up --dev                # layer the declared dev overlay (bind-mount/debug bring-up)
uv run up --build              # build images locally first (requires workloads/images.yml)
uv run up --gpu none           # override GPU auto-detection
uv run down                    # docker compose down
uv run down -v                 # also remove named volumes
uv run build                   # cross-build all images per workloads/images.yml
uv run build --lock-only       # refresh the image lock without building
uv run generate-score          # regenerate Score compose/k8s manifests per [tool.docker-devkit.generate-score]
uv run generate-score --local  # local-cluster variant (local storage class, gitignored output)
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
