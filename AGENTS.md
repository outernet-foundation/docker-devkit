# docker-devkit

## What this is

`docker-devkit` owns the Docker-stack lifecycle commands — `up`, `down`, `build` — plus the helpers they share (`detect_gpu`, `modes`, `context_sha`). It is a small, generic package that any repo shipping a compose graph can depend on to get the same bring-up/tear-down / cross-build flow, with the compose assembly declared per consumer in `[tool.docker-devkit.lifecycle]` so a repo that authors its own images and a repo that only OCI-includes an upstream artifact both work through the same commands.

The package is `docker_devkit` (src-layout under `src/docker_devkit/`); all dependencies resolve from PyPI (`bashrun>=0.1.0`; git-source pins only in scratch branches testing unreleased changes). The repo and package renamed twice on the way here — `stack-lifecycle` → `stack-toolkit` (2026-09-20, before any publish) and `stack-toolkit` → `docker-devkit` (2026-09-21, member of the `-devkit` family). The PyPI identity is fresh: `docker-devkit` starts its own tag ledger at `0.1.0`; the terminal `stack-toolkit` distributions (≤0.1.1) are deprecation signposts pointing here, not this package's history.

## Release flow

Publishing rides `release.yml`, triggered by a successful CI run on a `main` push: the machinery — release-devkit's `publish-stable` (an inlined, version-pinned `uvx` step in the publish job, keeping OIDC identity local), never a project dependency (docker-devkit sits inside its own dependency graph; a project-level release-devkit edge is a resolver cycle) — computes the plan from the tag ledger and path-diff, patches the version ephemerally, and publishes to PyPI under OIDC trusted publishing (publisher bound to `release.yml`, no environment). The committed `pyproject.toml` version is permanently the `0.0.0.dev0` sentinel; the `docker-devkit-v*` tags are the version ledger (declared `major_minor` line in `publish-config.json`, patch-auto within the line). While pre-1.0, breaking changes ride the normal patch flow without a bump; from 1.0 on, API-breaking changes ship with a manually bumped `major_minor` — patch-auto assumes additive changes.

## Shape

Entry points (`[project.scripts]`): `up` → `up.py:app`, `down` → `down.py:app`, `build` → `build_docker.py:app`, `generate-score` → `score_generate.py:app`. All accept `--help`.

| Module | Role |
|---|---|
| `up.py` | `docker compose up`. Flags: `--attached`/`-a`, `--quiet-pull`/`-q`, `--build`, `--gpu auto\|cuda\|rocm\|none`, `--dev`. |
| `down.py` | `docker compose down`. Flags: `--volumes`/`-v` (also removes named volumes), `--gpu`. |
| `build_docker.py` | `run_build()` + the `build` command — cross-builds all images per `compose.bake.yml`, writes `.env.lock` (one `NAME=path:tag@sha256:…` entry per `x-base-images` declaration; `@digest`-pinned declarations pass through unresolved) and `.env.shas` (locally-built `tree-<hash>` tags). `--mode ci` hard-requires `x-registry-cache` in the bake file. |
| `score_generate.py` | `generate-score` — deterministic Score-manifest generation from a consumer's `score/` sources: `${ALL_CAPS}` placeholder expansion into tempdir-rendered workload copies, idempotent `score-compose`/`score-k8s` init (state preserved across runs), and a determinism suite (document sort, state-path normalisation, 0644 modes, LF newlines). Consumer policy — workloads, provisioners, patch templates, output paths, storage classes, publishes, tool versions — is declared in the consumer's root `pyproject.toml` under `[tool.docker-devkit.generate-score]` (`ScoreConfig`); `load_score_config()` reads it, `ensure_score_tools()` fetches the pinned score-compose/score-k8s release binaries (for CI staleness gates). `--local` writes the k8s artifact to the configured gitignored local path with the local storage class. |
| `lifecycle.py` | `LifecycleConfig` — the `[tool.docker-devkit.lifecycle]` declaration (`files`: compose files in layering order with `{gpu}` templating; `dev_file`: optional dev overlay); `load_lifecycle_config()` (walks `tool → docker-devkit → lifecycle`, `None` when absent), `enforce_bake_declaration()` (a canonical `compose.bake.yml` requires a declaration), `expand_compose_files()` (assembles the `-f` list; a `{gpu}` entry is dropped when the GPU resolves to `none`; `dev_file` appended when a dev overlay applies). |
| `detect_gpu.py` | `detect_gpu()` → `cuda`/`rocm`/`none` from host devices; the `Gpu` literal. |
| `modes.py` | `resolve_auth_mode()` — validates `PUBLIC_URL` + `AUTH_MODE` from `.env`, rejecting `keycloak` over cleartext `http://`. |
| `context_sha.py` | `compute_service_shas(repo_root, bake: BakeDocument)` — per-service `tree-<hash>` image tags over the `.dockerignore`-allowlisted git tree. |
| `documents.py` | `BakeDocument` + `parse_bake(path)` — the typed bake-file model every consumer of bake data goes through (build, up, down, generate-score, the reference collectors). A field validator enforces at parse time that every `x-base-images` value is `path:tag` or `path@digest` (a `${…}` hole or a bare path is a validation error, killing the silent-unpinned `FROM` → `:latest` failure mode). `digest_pinned(reference)` is the shared tail test. |
| `image_refs.py` | Image-reference collection, split by source. `declared_references(root)` enumerates bake `x-base-images` declarations (parsed and validated via `BakeDocument`) — the single source consumer mirroring consumes. `stray_references(root, …globs)` scans Dockerfile `FROM`, Dockerfile `COPY --from=` (slash-bearing captures only, so build-stage names are skipped), and yaml `image:` lines — hand-written literal refs that bypass the declaration system; `unpinned_references(root)` polices exactly those strays (every slash-bearing reference must carry a tag, a digest, or a trailing `${build-arg}` suffix — consumer CIs fail on its result). `bake_base_image_refs(bake: BakeDocument)` adapts declarations to `ImageReference` pairs; `version_coupling_violations(root, couplings)` (each `VersionCoupling` names a pyproject-declared version and the `VersionSite` regexes that must restate it — bake `x-base-images` values or raw file text — so non-derivable couplings like uv `required-version` ↔ uv-base-image tag can't drift silently; the mechanism lives here, the registry in consumer repos' preflight) is unchanged. Remote digest resolution goes through `imagetools inspect`. Compose files are parsed by nothing in this package. |

## Constraints

### Lifecycle declaration contract

**Context.** A repo that authors its own stack builds its own images and assembles a multi-file compose graph (e.g. `compose.yml` + `compose.postgres.yml` + a GPU layer + `compose.dev.yml`), injecting per-service `${*_SHA}` image tags and a `.env.lock` of third-party digests. A consumer repo has none of that: it ships a single self-contained compose file that OCI-includes the upstream artifact already baked (image digests and internal vars frozen as literals) or includes a sibling checkout (which supplies its own `.env.lock`/`.env.shas` through the include's `env_file`).

**Constraint.** `up`/`down` assemble the graph from `[tool.docker-devkit.lifecycle]` in the consumer's root pyproject — one code path, declared data. Keys: `files` (compose files in layering order, `{gpu}` templating, a `{gpu}` entry dropped when the GPU resolves to `none`; defaults to empty), `dev_file` (dev overlay, appended by `up --dev` and always by `down` so teardown removes containers from prior dev bring-ups). No table → single-file default (`-f compose.yml`). `--compose-file` does not exist; raw `docker compose` is the pressure valve. `--dev` is opt-in — production-shaped bring-up is the default. `.env.lock` is required unconditionally, no knob; consumers pin stock images via `${…_IMAGE:?err}` holes filled by a committed lock. A canonical `compose.bake.yml` present without a lifecycle declaration is a hard error (declare the table, or the file doesn't belong under that name) — guards are budgeted for silent failures only, and a missing lock already fails loudly. The bake file remains the build/up contract: SHA injection and `--build` acceptance key on `compose.bake.yml` existing, and `build` always reads exactly that name. An empty assembly (`files` empty/omitted, no dev overlay) is a loud `up`/`down` error — the honest state for a builds-only repo.

**Consequences.** placeframe's shape lives in placeframe's pyproject, not in this package: `files = ["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"]`, `dev_file = "compose.dev.yml"`. A consumer repo declares a bare table (builds images, no repo-local runtime graph) and runs `uv run up` against its own `.env` + committed `.env.lock`.

### File and env conventions the commands assume

- `.env` (required) — carries `PUBLIC_URL` and `AUTH_MODE`; `modes.py` reads it and rejects `keycloak` over cleartext `http://`.
- `.env.lock` (required in every repo, checked in) — third-party and base-image pins, written by `build`: one `NAME=path:tag@sha256:…` entry per bake declaration. `up`/`down` refuse without it; consumers satisfy it with a committed lock filling the `${…_IMAGE:?err}` holes.
- `.env.shas` (where images are built locally, gitignored, per-build) — `tree-<hash>` tags of locally-built images, written by `build`. `up` injects the `${*_SHA}` values into the environment directly and reads neither file; `.env.shas` exists so a consumer running the compose files through raw `docker compose` can resolve those holes via `--env-file .env.shas`.
- `compose.bake.yml` (where a local build graph exists, required) — the buildx-bake description; its top-level `x-cross-compile-targets` list (optional) is the set of services excluded from the default target list because they need per-arch treatment, opt-in via `--targets`. One bake file name, ever: `build` reads exactly this name.
- the compose layers declared in `[tool.docker-devkit.lifecycle]` — e.g. placeframe's `compose.yml` / `compose.postgres.yml` / `compose.<gpu>.yml` / `compose.dev.yml`; the GPU one is picked by `detect_gpu()` (or the `--gpu` override).
- `.dockerignore` (where images are built locally) — allowlist that `context_sha.py` hashes over to compute per-service `tree-<hash>` tags. See "Docker build context" in `AGENTS-SHARED.md` for the allowlist rationale.

The build command writes to two files rather than one because a CI invariant forbids built-image digests in `.env.lock` (which is committed and reviewed) and because `.env.shas` changes on every source edit, so it stays out of version control.

### Package split rationale

Lifecycle (`up`/`down`/`build`) and codegen (client generation, datamodel generation, workspace locking) have disjoint dependency surfaces. Bundling them would force a consumer that only brings a stack up to install `sqlacodegen`, `psycopg`, `datamodel-code-generator`. Keeping the lifecycle in its own small package lets consumers git-reference just this without dragging the codegen surface behind it.

### `--quiet-pull` logging and GPU auto-detection

`--quiet-pull` suppresses compose's per-layer progress (CUDA images carry hundreds of layers) while still surfacing pull totals. `--gpu auto` resolves to `cuda`/`rocm`/`none` from host devices and selects the matching `compose.<gpu>.yml`; override only to reproduce a CI environment or test the CPU-only path.

## See also

- `README.md` — human-facing setup and usage.
- [`bashrun`](https://github.com/outernet-foundation/bashrun) — the guardrailed subprocess wrapper the lifecycle commands shell out through.
