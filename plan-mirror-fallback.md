# Mirror concept rehomes to docker-devkit; fail-closed upstream fallback

Untracked working file for this initiative. Do not commit; delete when the work
ships. Sibling of the completed `plan.md` (declarations consolidation) — that
initiative's NEXT note about make-it-sing's Stage 3 flip is already executed;
nothing there blocks this.

Pickup note (execution state): Work items 1, 2, and 3 are EXECUTED. Next up per
the operator: Work item 4 (dead-key hygiene — hard gate: needs release-devkit
0.1.13 verified on PyPI first), then the two parked items at the bottom
(make-it-sing PR / npm trusted publisher). Locked decisions are below —
do not relitigate them; the operator settled each during design.

Executed so far: docker-devkit 0.1.18 on PyPI (mirror verb, fail-closed
`up --allow-upstream-fallback`, upstream-aware lock resolution; live-verified
end to end against the unmirrored livekit ref). All three stack repos pushed:
make-it-sing `ci-support@104c03d` (consolidated by rebase; lock byte-identical
under full `--upgrade` re-resolution), placeframe `dev@565ea869` (15-entry lock
no-op diff), capture-tool `dev@a64f8eb` (3 refreshed digests — investigated:
stale mutable-tag pins, mirror == upstream on all three today; kept the
refresh). placeframe and capture-tool CI green running the swapped mirror verb.
The `--env-file` overlay precedence and the exec-flush of pre-handoff prints
are both verified mechanically.

Work item 3 executed: release-devkit `main@502e877` (two commits — code
`ed6b57e` deletes mirror_images.py, the script entry, the PublishConfig field,
its own publish-config key, and updates tests including a leftover-key-ignored
test; prose `502e877` scrubs AGENTS.md + README). ruff / basedpyright strict /
pytest (40) green locally; docker-devkit dep stays (create_release uses
compute_service_shas + parse_bake), ci-devkit dep stays (five other consumers).
AWAITING OPERATOR PUSH → CI → release.yml publishes 0.1.13. Work item 4's
key drops unlock only after that release is verified on PyPI
(`https://pypi.org/pypi/release-devkit/json`).

Update 2026-09-25: push landed, CI green, Release run 36162343128 published
**0.1.13** and tagged `release-devkit-v0.1.13`; verified on PyPI. Work item 4's
hard gate is OPEN — operator said hold execution until green-lit.

Work item 4 executed (2026-09-25, operator green-lit): 15 repos committed,
each one commit "Drop the dead mirror_prefix key; repin release-devkit to
0.1.13" (config + workflow pins together — the pin bump is forced by the
survey finding above; every repo's CI/release workflows load the config on a
pin ≤0.1.12 that requires the field). Branch/SHA (awaiting operator push):
bashrun main@6ec7418, ci-devkit main@2f4f5cb, docker-devkit main@65b657d
(its own key — the 16th config), logger-conf main@3984eba,
openapi-client-codegen main@82a8bda, python-devkit main@c8e0167,
unity-devkit main@d7c4692, extruded-text main@14fbdd9 (these eight have no
dev branch — main IS the mainline), lbe-toolkit dev@b462eb6 (now 3 ahead:
2 pre-existing unpushed + this), Nessle dev@799ad0c, ObserveThing dev@92bc691,
StatefulUnity dev@eb2d762, placeframe dev@e48ad9c0,
placeframe-capture-tool dev@a66a5cb, Make-it-Sing dev@44c5645 (moved from
ci-support@73b1954 after the PR-#2 merge; dev carries the publish tier now). Verified: zero mirror_prefix across
all 16 configs, zero non-0.1.13 release-devkit pins, all configs parse as
JSON, commit file lists exact. Strays left untouched: Make-it-Sing's local
.env.airgapped mod (preserved through the branch dance) + untracked
metadata.json (buildx provenance artifact, ignored on ci-support, not on
dev — candidate gitignore addition when the PR lands), ObserveThing's 3
untracked Unity files, capture-tool's extraction-plan.md.

## The incident that started this

Make-it-Sing's `workloads/images.lock` pins
`LIVEKIT_SERVER_IMAGE=ghcr.io/outernet-foundation/mirror/docker.io/livekit/livekit-server:v1.12.0@sha256:b1281e66…`.
That digest is **upstream docker.io's** digest for v1.12.0, and the mirror
repository has **never existed** (`crane ls` → NAME_UNKNOWN, authenticated).
`uv run up` on an engineer host died at pull with ghcr's anonymous `denied`
(ghcr hides existence from anonymous clients; authenticated it says `not found`
— do not re-derive this, it cost an hour). Root cause is structural, not
typo-class:

- `build_docker.py:116` resolves lock digests by inspecting **the declared
  reference itself** (the mirror name). A mirror-prefixed declaration therefore
  cannot be locked until CI has mirrored it — but mirroring is CI's job — so
  the dev flow (declare → re-lock → test → PR) dead-ends, and the committed
  livekit line was necessarily **hand-authored** to bridge the gap.
- The mirror concept lives in release-devkit (`mirror_images.py`,
  `mirror_prefix` in publish-config.json), but it is a stack concept: its
  inputs (`declared_references`, images.yml) and consumers (`up`, the lock)
  are docker-devkit's. mirror_images.py already imports
  `docker_devkit.image_refs.declared_references` — the arrow points down into
  the package that should own it.

## Locked design decisions

1. **Mirror-first addressing everywhere.** Durability for OSS cloners beats
   upstream egress (an upstream yank once broke external users of one of our
   repos — manifests get GC'd, digest pins alone don't save you). Strangers,
   CI, and prod always pull the mirror.
2. **Fail-closed default.** Missing mirror ref = hard error. People ignore
   warnings; warnings are errors someone was too lazy to fix. The only escape
   is the per-run explicit flag `--allow-upstream-fallback` on `up`.
3. **No config-level ban on the flag.** Air-gap fails closed anyway (no route
   to upstream — the pull dies regardless of the flag). Revisit only if a real
   repo needs it; do not add speculative config.
4. **Under the flag, substitutions print as plain statements**, not warnings —
   the operator asked for fallback, the run reports what it did.
5. **Error text (exact shape, adapt formatting):** `Image {mirror_ref}
   (digest {digest}) is not mirrored yet. If this is a new upstream dependency
   that has not been through CI, pass --allow-upstream-fallback to pull the
   same digest from upstream for this run.` It must NOT suggest running any
   populate command — mirroring is CI's job, single writer, and an error
   message that teaches engineers to push to the org namespace reopens the
   split-brain.
6. **docker-devkit gains ZERO new dependencies.** Specifically NO
   ci-devkit edge (verified: ci-devkit depends only on bashrun + pydantic;
   the siblings must stay siblings — the only thing drawing the edge was
   `ci_step`'s cosmetic logging). The mirror verb uses bashrun + plain
   prints. Likewise `install_crane` stays inline in the verb, NOT in
   ci-devkit's setup.py (same edge), and do NOT swap crane for
   `docker buildx imagetools create` — a manifest re-wrap changes the
   top-level digest and breaks lock equality; crane copy's byte-identical
   manifests are exactly why upstream and mirror digests match (verified on
   caddy: lock digest == mirror digest).
7. **Config home:** `[tool.docker-devkit.mirror]` table, key `prefix`
   (e.g. `ghcr.io/outernet-foundation/mirror`). Absent table → all new
   behavior off; `up`/`build` behave exactly as today. Declared data, one
   code path.
8. **Upstream-aware lock resolution:** for mirror-prefixed declarations,
   `build` resolves the digest against the upstream ref (strip prefix →
   registry host + path; tag/digest tail preserved) and records it under the
   mirror name. Valid because of decision 6's digest equality. This kills the
   chicken-and-egg: declare → re-lock works pre-mirror.
9. **Fallback is digest-preserving address substitution only** — the same
   bytes from a different host. Preflight covers exactly the lock entries
   whose value starts with the configured prefix; non-mirror entries
   (e.g. `placeframe/*` publishes) keep failing naturally at pull.
10. **CI never passes the flag.** The mirror job runs before anything needs
    the images; no workflow change beyond the step swap.

## Verified ground truth (do not re-derive)

- Dependency DAG: bashrun (leaf) ← ci-devkit {bashrun, pydantic};
  docker-devkit {bashrun, pydantic, pydantic-settings, typer, pyyaml,
  pathspec}; release-devkit {bashrun, docker-devkit≥0.1.15, ci-devkit,
  pydantic, pydantic-settings, typer}. No cycles; keep it that way.
- release-devkit `PublishConfig` (`config.py:26-34`): `mirror_prefix: str`
  is REQUIRED; the model has no extra policy → pydantic default `ignore`.
  Consequence: consumer configs can drop the key ONLY after the
  field-removal release is on PyPI (older versions reject its absence), and
  leftover keys are harmless after (ignored).
- `mirror_images.py` behavior to preserve in the moved verb: filter
  `declared_references(root)` by prefix; upstream =
  `reference[len(prefix)+1:]`; crane v0.22.1 installed by curl tarball to
  /usr/local/bin (sudo-aware); `crane copy` per target, sorted; final count
  print. Reads cwd — the verb needs no `--config` flag (pyproject table).
- CI mirror steps today (exact sites):
  - Make-it-Sing `.github/workflows/ci.yml:61` —
    `uvx --from release-devkit==0.1.12 mirror-images --config build/publish-config.json`
  - placeframe `.github/workflows/placeframe-ci.yml:52` — same, `==0.1.11`
  - placeframe-capture-tool `.github/workflows/ci.yml:75` — same, `==0.1.11`
- Repos carrying a dead/required `mirror_prefix` key in publish-config.json
  (16 total): bashrun, ci-devkit, logger-conf, unity-devkit, python-devkit,
  openapi-client-codegen, docker-devkit, release-devkit, ObserveThing,
  StatefulUnity, Nessle, lbe-toolkit, extruded-text, Make-it-Sing, placeframe,
  placeframe-capture-tool. Only the last three run mirror steps.
- Repo states at authoring: docker-devkit `main@f1ad198` clean;
  release-devkit `main@788c969` (1 dirty file — its own untracked plan);
  placeframe `dev@934b4359` clean; capture-tool `dev@915b9a6` (1 dirty);
  Make-it-Sing `ci-support@5d59408` clean, synced with origin.
- PyPI reality: docker-devkit latest 0.1.17 → expect this wave's release to
  be **0.1.18**; release-devkit latest 0.1.12 → expect **0.1.13**. Local tag
  clones are stale (stop at v0.1.9) — verify the ledger at execution via PyPI
  (`https://pypi.org/pypi/docker-devkit/json`), not `git tag`.
- All consumer release-devkit pins are exact → old workflows keep working
  against old PyPI releases forever (registries keep both). Sequencing is
  soft everywhere EXCEPT the publish-config key drops (hard constraint in
  Work item 3).

## Work item 1 — docker-devkit (do first; gates the wave)

Direct to `main` (established pattern), prose and code in separate commits,
ruff + basedpyright strict + pytest green before handoff.

1. `lifecycle.py` (or a sibling loader in the new module): `MirrorConfig`
   (pydantic, `prefix: str`), `load_mirror_config(root)` walking
   `tool → docker-devkit → mirror`; `None` when absent.
2. New `mirror.py`: `is_mirrored(reference, prefix)`, `upstream_ref(reference,
   prefix)` (prefix strip; preserves `:tag@sha256:…` tail),
   `reference_exists(reference)` (boolean wrapper over the
   `docker buildx imagetools inspect` shell-out pattern in
   `image_refs.py:123`; honors docker config creds),
   `mirror_targets(root)` (declared refs filtered by prefix), and the
   `mirror` typer app: crane install + per-target `crane copy`, plain-print
   step lines (no ci_step, no GHA syntax), count footer. Register
   `mirror = docker_devkit.mirror:app` in `[project.scripts]`. No new deps.
3. `up.py`: `--allow-upstream-fallback` (Annotated). After lock resolution,
   before composing the command: for each lock entry whose value is
   mirror-prefixed, `reference_exists`; missing + no flag → `RuntimeError`
   with the locked text (decision 5); missing + flag → substitute
   upstream@same-digest for exactly those entries, print each substitution
   as a plain statement, and make the rewrite authoritative on BOTH paths:
   an appended `--env-file` overlay (verify compose's later-file-wins
   precedence for multiple `--env-file`s — this is the one open mechanical
   detail; acceptance criterion: the substituted ref is what compose AND
   `--build`'s bake holes actually consume, proven with a dry-run) plus
   `os.environ.update` before `run_build` (the `compute_service_shas`
   pattern at `up.py:74` is the precedent). Sequential preflight is fine
   (~15 refs).
4. `build_docker.py` lock-write path (line ~116): mirror-prefixed
   declarations resolve via `upstream_ref` (decision 8). Config absent →
   unchanged behavior.
5. Tests: inversion round-trips across the real upstream registries seen in
   locks (docker.io, ghcr.io, quay.io paths), tail preservation, prefix
   filter, fail-closed error text, flag-gated substitution set, overlay
   contents, upstream-aware lock resolution (inspect mocked), verb target
   enumeration.
6. Prose commit: AGENTS.md — new "Mirror" section (config home, verb, the
   fail-closed invariant and its rationale, upstream-resolved locking, the
   no-ci-devkit-edge rule); README flag list.
7. Hand off for operator push → CI → release.yml auto-publishes the next
   patch. Verify on PyPI (uvx cache needs `--refresh` during propagation).

## Work item 2 — the three stack repos (after 0.1.18 is on PyPI)

Per repo, one commit (config + step + lock ride together):

- `[tool.docker-devkit.mirror]` table in root pyproject:
  `prefix = "ghcr.io/outernet-foundation/mirror"`.
- CI step swap (the exact diff, job shells untouched):
  `uvx --from release-devkit==<old> mirror-images --config <cfg>` →
  `uvx --from docker-devkit==0.1.18 mirror`
  (sites listed in ground truth; keep the checkout/setup-uv/ghcr-login steps
  and `packages: write` — the verb still pushes via crane with docker creds).
- `uv lock --upgrade-package docker-devkit` + `uv sync`.
- `uv run build --lock-only` to regenerate locks through the new
  upstream-resolving path. make-it-sing's livekit line comes out
  byte-identical (`sha256:b1281e66…`) — machine-written this time.
  placeframe's 17-entry lock must be a NO-OP diff; **if any digest changes,
  stop and investigate** (would indicate mirror drift, and the whole digest-
  equality design depends on there being none).
- Local verification (make-it-sing at least): `uv run up` errors cleanly on
  the unmirrored livekit ref with the locked message;
  `uv run up --allow-upstream-fallback` prints the substitution and proceeds
  (env vars permitting).
- publish-config `mirror_prefix` keys stay FOR NOW (hard constraint below).

Branch placement: make-it-sing on `ci-support`; placeframe and capture-tool
on their current `dev` branches (check state at execution; capture-tool's
bake file resolves under the legacy `compose.bake.yml` name — already
supported).

When make-it-sing's PR next runs CI, the swapped mirror job populates the
livekit mirror — the original incident closes itself.

## Work item 3 — release-devkit (after Work item 2)

Direct to `main`:

- Delete `mirror_images.py`; drop `mirror_prefix` from `PublishConfig`;
  drop the key from its own `publish-config.json`; update AGENTS.md (the
  "What this is" mirror mention + commands table) and README; config tests
  updated for the field's absence.
- Publishes next patch (expect 0.1.13).
- **Hard ordering constraint — the only one:** consumer publish-config key
  drops (Work item 4 and the three stack repos' key drops) MUST come after
  this release is on PyPI; the field is required on every older pin, so
  dropping the key early fails `load_config` validation on those versions.

## Work item 4 — dead-key hygiene (after 0.1.13 is on PyPI)

`mirror_prefix` deletion from publish-config.json in: bashrun, ci-devkit,
logger-conf, unity-devkit, python-devkit, openapi-client-codegen,
ObserveThing, StatefulUnity, Nessle, lbe-toolkit, extruded-text,
docker-devkit (its own key) — plus the three stack repos' key drops held from
Work item 2. Execution-discovered constraint (2026-09-25 survey): every one of
these repos' workflows invokes config-loading release-devkit verbs on exact
pins ≤0.1.12, and every one of those versions requires the field at
`load_config` — so each repo's drop commit ALSO repins its workflow's
`release-devkit==<old>` to `==0.1.13` (config + workflow in one commit, the
Work item 2 pattern). Pure rot removal otherwise; batchable. Branch placement
per operator: dev where the repo has one (stack repos, Unity repos,
lbe-toolkit, make-it-sing — checked out from ci-support for this); main where
main is the only line (devkits, extruded-text).

## Parked items (operator: tackle after Work items 3 and 4)

1. **Make-it-Sing PR — MERGED 2026-09-25** (PR #2, ci-support@104c03d → dev,
   merge 7a7604d). The run's mirror job went green and crane pushed
   `mirror/docker.io/livekit/livekit-server:v1.12.0` at exactly the locked
   digest b1281e66 (verified in the job log). The hygiene commit was moved
   onto post-merge dev as `dev@44c5645` (cherry-pick; ci-support rewound to
   104c03d = origin) — awaiting operator push. NEW OPERATOR ACTION: the
   freshly created livekit package defaulted to ghcr PRIVATE — anonymous
   manifest denied, App-token hidden-404 (caddy sibling answers anonymous 200,
   so the namespace convention is public). Flip the package to public, and
   consider setting the org default visibility for new container packages to
   public so every first-mirrored image doesn't land private (CI pushes with
   the repo token, which auto-links the package but leaves it private).
   Overall CI on the merge is red in the unity build jobs only — operator is
   fixing that in a separate session; unrelated to the mirror work.
2. **Capture-tool Release on `main` fails at `publish-dev`** — `npm publish
   --provenance` gets a 404 on the OIDC exchange for
   `org.outernet.placeframe.zedcaptureclient` (ENEEDAUTH): no npm trusted
   publisher is configured for that package. npm allows one trusted publisher
   per package, bound to a single workflow filename — needs an npmjs.org-side
   publisher config bound to the workflow that publishes it. Surfaced by the
   2026-09-25 `main` merge; not caused by the mirror swap (capture-tool's own
   CI on `dev` was green). Operator-side config + a re-run.

## Session gotchas a fresh executor needs

- Commit discipline: no trailers, subjects <72 chars naming actual things,
  prose and code in separate commits, `--fixup` + `--autosquash` for
  squashing (verify branch-local first), commit at each step boundary.
- Code style: no docstrings, no inline imports, no comments unless
  load-bearing, typer `Annotated` parameters, callers-before-callees,
  classes at top, no closures, bashrun (never subprocess) — `bash_output`
  for values, `bash_check` for expected-failure probes.
- Sandbox cannot push (App token is read-only; `GITHUB_TOKEN` env var is
  ghcr-only). Commit locally at the bind mounts, hand branch + SHA to the
  operator. `gh` needs
  `GH_TOKEN=$(uv run --project /workspace/pulsar/scripts mint-github-app-token) gh …`;
  never `gh run watch`.
- prepo owns worktrees: `prepo new` / `prepo drop` only.
- uvx caches the PyPI index — `--refresh` during post-publish windows.
- Do not commit this plan file. Do not hand-edit lock digests — the whole
  point of Work item 1.4 is that the path is now machine-walkable.

## Verification checklist (ship criteria)

- [x] docker-devkit: ruff, basedpyright strict, pytest green; AGENTS/README
      updated; released to PyPI (0.1.18 verified).
- [x] `uv run up` in make-it-sing errors closed on the livekit ref with the
      locked message; `--allow-upstream-fallback` substitutes and proceeds.
- [x] `uv run build --lock-only` regenerates make-it-sing's lock
      byte-identically; placeframe's lock is a no-op diff.
- [x] All three CI mirror steps swapped and green on their next runs.
      (make-it-sing's mirror job green on the PR-#2 merge run; the run's
      unity jobs failed separately — operator fixing in another session.)
- [x] release-devkit 0.1.13 on PyPI with the field removed.
- [x] All 16 publish-configs carry no `mirror_prefix` (15 local commits
      awaiting operator push + release-devkit's WI3 drop).
- [x] The livekit mirror package exists at the locked digest (crane-verified
      in CI); operator flipped it public 2026-09-25 — anonymous manifest fetch
      at sha256:b1281e66… returns 200 (verified from the sandbox). The
      original incident is closed.
