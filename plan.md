# Lifecycle declaration rewrite

Status note for pickup: this plan was authored mid-initiative after a full
design review with the operator; every decision below is settled unless marked
otherwise. It is the master plan for the coordinated change set — the rewrite
here plus the consumer migrations in placeframe and placeframe-capture-tool,
and the downstream stages in make-it-sing (that repo's own `plan.md` carries
its CI bring-up detail). AUDIT CURRENT STATE before executing: what has
landed, what has been released to PyPI, and what each consumer pins.

## Why

`up`/`down` currently pick their behavior by sniffing for a file named exactly
`compose.bake.yml`, and the "native" branch hardcodes placeframe's repository
layout (`-f compose.yml -f compose.postgres.yml -f compose.<gpu>.yml -f
compose.dev.yml`). Two defects: placeframe's shape is embedded in a supposedly
generic package, and semantics change at a distance through an unrelated
filename. `build` compounds it with a `--bake-file` option, legitimizing
multiple bake files per repo. The fix: compose assembly becomes declared data,
one code path, one bake file name, ever.

## Settled design

- **Declaration**: `[tool.docker-devkit.lifecycle]` in the consumer's root
  pyproject (sub-table per the package's own `generate-score` precedent —
  multiple config surfaces, strictly validated per concern). Keys:
  - `files`: optional list of compose files in layering order, `{gpu}`
    templating supported; a `{gpu}` entry is dropped when the GPU resolves to
    `none`. Defaults to empty. Omitted and `[]` are equivalent.
  - `dev_file`: optional dev overlay, appended when `--dev` is passed;
    always included on `down` (teardown must remove containers from prior dev
    bring-ups).
- **One code path**: table present → assembly from `files`/`dev_file`; no
  table → single-file default (`-f compose.yml`). `--compose-file` is deleted;
  raw `docker compose` is the pressure valve.
- **`--dev` opt-in** replaces `--no-dev` (production-shaped bring-up is the
  default; bind-mount/debug overlay is explicit).
- **`.env.lock` unconditionally required**: `up`/`down` refuse without it in
  every repo. No knob. Consequence: consumers pin stock images via
  `${…_DIGEST:?err}` holes filled by a committed lock (capture-tool already
  does this).
- **Guard**: canonical `compose.bake.yml` present but no `[tool.docker-
  devkit.lifecycle]` table → hard error naming both fixes (declare, or you
  shouldn't have a canonical bake file). Guards are budgeted for silent
  failures only — a missing lock already fails loudly via `${VAR:?err}` and
  needs none.
- **Bake file remains the build/up contract**: SHA injection and `--build`
  acceptance key on `compose.bake.yml` existing. One bake file name, ever:
  `build` loses `--bake-file` and always reads `compose.bake.yml`; `--targets`
  stays (selects services within the file).
- **Empty assembly is loud**: `files` empty/omitted with no `--dev` graph
  means `up` errors ("no compose files declared") — this is the honest state
  for a builds-only repo.
- **Release**: no `major_minor` bump — pre-1.0 policy, breaking is accepted
  while iterating toward stability. Rides the normal patch flow.

## Changes in this repo

- New `src/docker_devkit/lifecycle.py`: `LifecycleConfig` (pydantic: `files`
  default empty, `dev_file` optional), `load_lifecycle_config` (walks `tool →
  docker-devkit → lifecycle`; `None` when absent), the guard, and
  `expand_compose_files`.
- `up.py` / `down.py`: mode fork and `--compose-file` deleted; `--dev`
  opt-in; `.env.lock` required unconditionally; assembly from declaration.
- `build_docker.py`: `--bake-file` deleted.
- `tests/test_lifecycle.py`: `{gpu}` expansion, `none` drop, `dev_file`
  toggle, loader present/absent, guard accept/reject.
- Docs: AGENTS.md "Native stack vs consumer stack" → the declaration
  contract; README and the pyproject description string updated to match.
- Branch + PR, never direct to main (a green main push auto-publishes).

## Consumer migrations

### placeframe

- Add to root pyproject:

  ```toml
  [tool.docker-devkit.lifecycle]
  files = ["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"]
  dev_file = "compose.dev.yml"
  ```

  Inert under released versions (unknown sub-tables ignored); required before
  the pin upgrade. Deliberate `uv lock --upgrade-package docker-devkit` after
  this repo's release.
- Zed is LEFT AS-IS by operator decision: `compose.zed.bake.yml`, the
  `build-zed` CI job, and the zed sources stay. Accepted breakage: `build-zed`
  needs `--bake-file`, which this rewrite deletes — that job fails until the
  operator reconciles it at merge time. Do not "fix" it here.

### placeframe-capture-tool

- Rename `compose.zed.bake.yml` → `compose.bake.yml` (repo root).
- Move `docker/zed-capture/compose.rig.yml` → `compose.rig.yml` (repo root);
  check its relative references during the move (expected none — mostly
  absolute host paths).
- Add the bare declaration (builds images, no repo-local runtime graph):

  ```toml
  [tool.docker-devkit.lifecycle]
  ```

- CI `build-zed` drops the `--bake-file` argument; sweep `install-zed`'s
  internal build call for `--bake-file` references at the same time.

### make-it-sing

Consumer by default — no bake file, no table. Its Stage 3 later adds a
canonical `compose.bake.yml` + `files = ["compose.yml"]`; its Stage 5 adds the
committed `.env.lock` and digest-hole conversion of its stock image refs. See
that repo's `plan.md`.

## Sequencing

1. This rewrite: branch, PR, operator push, release.
2. placeframe lifecycle table (any time; inert until the pin bump) → pin bump.
3. placeframe-capture-tool renames + declaration + CI arg drop (guard only
   bites once its own pin passes the rewrite).
4. make-it-sing Stages 3 and 5 (gated on its pin).

All pushes are operator-handed from the sandbox checkouts.
