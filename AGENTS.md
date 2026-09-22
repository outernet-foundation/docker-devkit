# docker-devkit

## What this is

`docker-devkit` owns the Docker-stack lifecycle commands — `up`, `down`, `build` — plus the helpers they share (`detect_gpu`, `modes`, `context_sha`). It is a small, generic package that any repo shipping a compose graph can depend on to get the same bring-up/tear-down / cross-build flow, with a native/consumer split so a repo that authors its own images and a repo that only OCI-includes an upstream artifact both work through the same commands.

The package is `docker_devkit` (src-layout under `src/docker_devkit/`); all dependencies resolve from PyPI (`bashrun>=0.1.0`; git-source pins only in scratch branches testing unreleased changes). The repo and package renamed twice on the way here — `stack-lifecycle` → `stack-toolkit` (2026-09-20, before any publish) and `stack-toolkit` → `docker-devkit` (2026-09-21, member of the `-devkit` family). The PyPI identity is fresh: `docker-devkit` starts its own tag ledger at `0.1.0`; the terminal `stack-toolkit` distributions (≤0.1.1) are deprecation signposts pointing here, not this package's history.

## Release flow

Publishing rides `release.yml`, triggered by a successful CI run on a `main` push: the machinery — release-devkit's publish composite action (pinned by SHA; the uvx invocation runs inside the caller's job, keeping OIDC identity local), never a project dependency (docker-devkit sits inside its own dependency graph; a project-level release-devkit edge is a resolver cycle) — computes the plan from the tag ledger and path-diff, patches the version ephemerally, and publishes to PyPI under OIDC trusted publishing (publisher bound to `release.yml`, no environment). The committed `pyproject.toml` version is permanently the `0.0.0.dev0` sentinel; the `docker-devkit-v*` tags are the version ledger (first release `0.1.0`, patch-auto thereafter). API-breaking changes ship with a manually bumped version — patch-auto assumes additive changes.

## Shape

Entry points (`[project.scripts]`): `up` → `up.py:app`, `down` → `down.py:app`, `build` → `build_docker.py:app`. All accept `--help`.

| Module | Role |
|---|---|
| `up.py` | `docker compose up`. Flags: `--attached`/`-a`, `--quiet-pull`/`-q`, `--build`, `--gpu auto\|cuda\|rocm\|none`, `--no-dev`, `--compose-file`. |
| `down.py` | `docker compose down`. Flags: `--volumes`/`-v` (also removes named volumes), `--gpu`, `--compose-file`. |
| `build_docker.py` | `run_build()` + the `build` command — cross-builds all images per `compose.bake.yml`, writes `.env.lock` (third-party pulled digests) and `.env.shas` (locally-built `tree-<hash>` tags). |
| `detect_gpu.py` | `detect_gpu()` → `cuda`/`rocm`/`none` from host devices; the `Gpu` literal. |
| `modes.py` | `resolve_auth_mode()` — validates `PUBLIC_URL` + `AUTH_MODE` from `.env`, rejecting `keycloak` over cleartext `http://`. |
| `context_sha.py` | `compute_service_shas()` — per-service `tree-<hash>` image tags over the `.dockerignore`-allowlisted git tree. |
| `image_refs.py` | Image-reference collection — `collect_repo_references(root, …globs)` enumerates every reference across compose `x-image-ref` services, bake `x-base-images` (pydantic-validated), Dockerfile `FROM`, Dockerfile `COPY --from=` (slash-bearing captures only, so build-stage names are skipped), and yaml `image:` line scans; the Dockerfile and yaml globs are `None`-able to skip those sources (consumer mirroring scans declarations only, policing scans everything); plus build-arg stripping, `unpinned_references(root)` (every slash-bearing reference must carry a tag, a digest, or a trailing `${build-arg}` suffix — consumer CIs fail on its result), `version_coupling_violations(root, couplings)` (each `VersionCoupling` names a pyproject-declared version and the `VersionSite` regexes that must restate it — bake `x-base-images` values or raw file text — so non-derivable couplings like uv `required-version` ↔ uv-base-image tag can't drift silently; the mechanism lives here, the registry in consumer repos' preflight), and remote digest resolution via `imagetools inspect`. `run_build()` consumes the two structural collectors; consumer repos' mirroring tooling consumes the repo scan. |

## Constraints

### Native stack vs. consumer stack

**Context.** A repo that authors its own stack builds its own images and assembles a multi-file compose graph (`compose.yml` + `compose.postgres.yml` + a GPU layer + `compose.dev.yml`), injecting per-service `${*_SHA}` image tags and a `.env.lock` of third-party digests. A consumer repo has none of that: it ships a single self-contained compose file that OCI-includes the upstream artifact already baked (image digests and internal vars frozen as literals) or includes a sibling checkout (which supplies its own `.env.lock`/`.env.shas` through the include's `env_file`). Running `compute_service_shas` there is meaningless (no `compose.bake.yml`, no Dockerfiles) and requiring `.env.lock` is wrong (the consumer has no internal vars to resolve).

**Constraint.** `up`/`down` pick the mode by the marker `compose.bake.yml`. **Native** = the default `--compose-file compose.yml` *and* `compose.bake.yml` present → multi-file assembly, SHA injection, `.env.lock` required. **Consumer** = anything else → the single `--compose-file` is the whole graph, run with `--env-file .env` alone; SHA resolution is skipped and `.env.lock` is passed only if it happens to exist. `--build` is rejected outside native mode (a consumer has no local build graph).

**Consequences.** A consumer runs `uv run up` (default `compose.yml`) or `uv run up --compose-file compose.local.yml` from its own repo root, against its own `.env`, with no extra setup. A native stack's behavior is unchanged: default `compose.yml` stays native, and an explicit non-default `--compose-file` still falls through to the single-graph path.

### File and env conventions the commands assume

- `.env` (required) — carries `PUBLIC_URL` and `AUTH_MODE`; `modes.py` reads it and rejects `keycloak` over cleartext `http://`.
- `.env.lock` (native, required, checked in) — third-party and base-image digests, written by `build`. `up`/`down` refuse to run natively without it.
- `.env.shas` (native, gitignored, per-build) — `tree-<hash>` tags of locally-built images, written by `build`. `up` injects the `${*_SHA}` values into the environment directly (native mode only) and reads neither file; `.env.shas` exists so a consumer running the compose files through raw `docker compose` can resolve those holes via `--env-file .env.shas`.
- `compose.bake.yml` (native, required) — the buildx-bake description; its top-level `x-cross-compile-targets` list (optional) is the set of services excluded from the default target list because they need per-arch treatment, opt-in via `--targets`.
- `compose.yml` / `compose.postgres.yml` / `compose.<gpu>.yml` / `compose.dev.yml` — native's multi-file layers; the GPU one is picked by `detect_gpu()` (or the `--gpu` override).
- `.dockerignore` (native) — allowlist that `context_sha.py` hashes over to compute per-service `tree-<hash>` tags. See "Docker build context" in `AGENTS-SHARED.md` for the allowlist rationale.

The build command writes to two files rather than one because a CI invariant forbids built-image digests in `.env.lock` (which is committed and reviewed) and because `.env.shas` changes on every source edit, so it stays out of version control.

### Package split rationale

Lifecycle (`up`/`down`/`build`) and codegen (client generation, datamodel generation, workspace locking) have disjoint dependency surfaces. Bundling them would force a consumer that only brings a stack up to install `sqlacodegen`, `psycopg`, `datamodel-code-generator`. Keeping the lifecycle in its own small package lets consumers git-reference just this without dragging the codegen surface behind it.

### `--quiet-pull` logging and GPU auto-detection

`--quiet-pull` suppresses compose's per-layer progress (CUDA images carry hundreds of layers) while still surfacing pull totals. `--gpu auto` resolves to `cuda`/`rocm`/`none` from host devices and selects the matching `compose.<gpu>.yml`; override only to reproduce a CI environment or test the CPU-only path.

## See also

- `README.md` — human-facing setup and usage.
- [`bashrun`](https://github.com/outernet-foundation/bashrun) — the guardrailed subprocess wrapper the lifecycle commands shell out through.
