# Artifact identity implementation plan

Procedural delivery plan for the artifact-identity ADR (`plan-artifact-identity.md`).
The ADR owns decisions and rationale; this plan owns the ordered work and is the
document implementing sessions build against. Per phase: load the phase section,
the files it names, and the ADR anchor it cites — not the whole ADR. If this plan
and the ADR disagree, reconcile both docs before touching code (spec-first).

Phase completion lives in git history; nothing here tracks status.

## Standing constraints

These rulings bind every phase:

- Fail closed: an unattributable construct, unknown name, missing lock entry, or
  dirty content-addressed build path refuses loudly, never silently.
- The PR channel publishes nothing permanent anywhere — only content-keyed
  ephemera (`tree-`, `sha-`).
- Exact tags only. Digests live exclusively in `docker-devkit.lock`, written only
  by `build --lock-only`. `latest` is never a pin and is aliased only by stable
  releases.
- No tag GC and no retention policies on image repos carrying release tags; ghcr
  deletion is version-level, so any future sweeper must be version-level and
  release-protecting. None is planned.
- Mirroring is for third-party upstreams only; org-internal artifacts resolve
  direct.
- One ordinal — the commit count at the certified head — keys `pr-{N}-{count}`
  and the landing's `dev-{count}`.

## Sequence

```
P1 precision      phases 1-4     docker-devkit tree-tag precision, release, relock wave
P2 semver         phases 5-9     toolkit + build-artifact-registry + devkit:
                                 version lines, sha-only publishes
P3+P4a consumer   phases 10-14   docker-devkit: qualified grammar, lock pinning,
                                 overrides, release
adoption wave     phases 15-17   one commit per consumer repo
P4b overlay       phase 18       cross-repo dev overlay — trigger-gated, not scheduled
```

Order within a cluster is strict; cluster order is fixed above.

## Session protocol

One phase per session. Verify with the target repo's battery (`uv run pytest`,
`uv run ruff check`, `uv run basedpyright` — per each repo's AGENTS.md). Commit as
you go; prose and code in separate commits. A phase is done when its end state
holds and its verify list passes green.

## Phase map

| # | Phase | Repo | ADR |
|---|---|---|---|
| 1 | Attribution parser and construct guard | docker-devkit | Part 1 |
| 2 | Attribution-based service hashes | docker-devkit | Part 1 |
| 3 | Dirty-tree guard | docker-devkit | Part 1 |
| 4 | Precision release and relock wave | docker-devkit + consumers | Part 1 |
| 5 | Registry aliasing primitive | build-artifact-registry | Part 2 |
| 6 | Image and bundle declarations | github-actions-toolkit | Part 2 |
| 7 | Change detection | github-actions-toolkit | Part 2 |
| 8 | Release verb channels | github-actions-toolkit | Part 2 |
| 9 | Publish-side cleanup release | docker-devkit + toolkit | Part 2 |
| 10 | Qualified selection grammar | docker-devkit | Part 3 |
| 11 | Bundle pinning through the lock | docker-devkit | Part 3 |
| 12 | Lock probing at bring-up | docker-devkit | Part 3 |
| 13 | Local bundle overrides | docker-devkit | Part 4 |
| 14 | Consumer-surface release | docker-devkit | Sequencing |
| 15 | placeframe adoption | placeframe | Consumer adoption |
| 16 | Make-it-Sing adoption and tag hygiene | Make-it-Sing | Consumer adoption, Hygiene |
| 17 | capture-tool adoption; infra at leisure | placeframe-capture-tool, infra | Consumer adoption |
| 18 | Cross-repo dev overlay (trigger-gated) | docker-devkit | Part 4 |

## Phase 1 — Attribution parser and construct guard

Cluster P1 · docker-devkit · ADR Part 1, "The design" and "The fail-closed
construct guard". Load: `src/docker_devkit/documents.py`,
`src/docker_devkit/config.py` (`derive_services` and its Dockerfile reading), the
Dockerfile fixtures under `tests/`.

End state:

- A pure parsing layer in a new module beside `documents.py` computes, per
  service: the Dockerfile's `COPY`/`ADD` source paths and globs, its `ARG`
  declarations, its own `build` mapping serialized from the parsed root document
  (YAML anchors already resolved — never the raw file), and its
  `additional_contexts` edges in `target:` form only.
- Every construct that can read the build context is either fully attributed or
  a loud failure: path-form `additional_contexts` rejects;
  `RUN --mount=type=bind,source=…` rejects; heredoc `COPY`, unknown syntax, and
  unresolvable globs reject; URL-form `ADD` rides (network channel, uncovered by
  design).
- `COPY` sources outside {own dir} ∪ {copied shared roots} ∪ {declared
  dependency outputs} reject — the cross-service guard absorbed into the
  attribution pass.
- Tag lists and global non-bit keys (`x-cross-compile-targets`,
  `x-registry-cache`) are not inputs.

Verify: fixture tests — one per loud failure, one per attributed construct.

## Phase 2 — Attribution-based service hashes

Cluster P1 · docker-devkit · ADR Part 1, "Over-inclusion fates" and "Soundness
argument". Load: `documents.py` (`compute_service_shas`), `matrix.py` (the
closure walk inside `matrix_main`), `tests/test_context_sha.py`.

End state:

- The closure walk (union-find over context edges, inline in `matrix_main`) is
  lifted to a shared function in the documents layer; `matrix.py` and the hasher
  both consume it. The stale `neural-networks-base-*` tagless comment in
  `matrix.py` is gone.
- `compute_service_shas` hashes, per service: the Dockerfile itself; the resolved
  `COPY`/`ADD` set against the allowlisted committed tree; the lock lines whose
  names intersect the Dockerfile's `ARG` declarations (declared-but-unused args
  still hash; compose-runtime-only lock lines drop); the service's own build
  mapping; and each context dependency's computed tree hash embedded as a
  synthetic entry (hash-of-hash over the DAG).
- `.dockerignore` is not a hash input.
- Services sharing one Dockerfile dir hash identically (the variant difference
  is a manifest arg inside the build mapping); an edit to the variant arg re-keys
  both.
- An empty service set still returns `{}` without touching git.

Verify (the negative-test battery): undeclared-base lock edit → unaffected
services unchanged; another service's manifest arg edit → this service unchanged;
a shared-root file no service copies edited → no re-key; a `.dockerignore` edit
that matches nothing → no re-key; own-dir edit → re-key; dependency-dir edit →
dependent re-keys; declared-arg lock edit → re-key.

## Phase 3 — Dirty-tree guard

Cluster P1 · docker-devkit · ADR Part 1, "Dirty-tree guard". Load:
`build_docker.py`, `compose.py`.

End state:

- `build` and `up --build` run `git status --porcelain` filtered to the
  `.dockerignore` allowlist (modified and untracked both count) and refuse
  loudly listing the offending paths.
- `build --dev` and bare `up` are exempt — dev tags are not content-addressed
  and the bind-mount workflow runs dirty; bare `up` pulls committed-tree tags
  and is self-consistent.
- `--lock-only`, `generate-score`, and `publish-compose` carry no guard.

Verify: dirty tree → loud failure listing paths on `build` and `up --build`;
permitted on `build --dev` and bare `up`.

## Phase 4 — Precision release and relock wave

Cluster P1 · docker-devkit + consumers · ADR Part 1, "One-time re-key
choreography" and "Delivery". Load: the release flow section of docker-devkit's
AGENTS.md, the consumers' `uv.lock` files.

End state:

- docker-devkit ships one patch release carrying phases 1-3 (pre-1.0; breaking
  rides the patch flow).
- placeframe, placeframe-capture-tool, and Make-it-Sing relock onto it.
- placeframe: commit the devkit bump, run `generate-score`, commit
  `stack/generated/**` in a second commit (the score staleness gate is disabled
  there — deliberate operator discipline). Old `tree-` tags freeze in place; CI
  never re-pushes them, so existing pins convert from movable to immutable.
- infra's three `tree-` pins keep resolving to frozen digests; calibration-corpus
  rows keep their old `pipeline_version` values (the existing re-key hard-fail
  stands).

Verify: consumer CI green end-to-end; the placeframe regeneration diff is
committed and reviewed.

## Phase 5 — Registry aliasing primitive

Cluster P2 · build-artifact-registry · ADR Part 2, "Aliasing mechanism". Load:
`src/build_artifact_registry/cache.py` (`promote_scope` — the `oras tag`
pattern), `setup_oras.py`.

End state:

- A new primitive beside `promote_scope`: resolve a `ref@digest` and `oras tag`
  the new tags onto it. Multiple tags per call. Missing references fail loudly.
  Aliases preserve digest and media type for OCI image indexes and single
  manifests alike.
- `docker buildx imagetools create -t` is the recorded fallback if oras aliasing
  ever fails against ghcr.

Verify: stub-registry tests — index and manifest aliasing, multi-tag calls,
missing-ref failure.

## Phase 6 — Image and bundle declarations

Cluster P2 · github-actions-toolkit · ADR Part 2, "Version space" and
"Declaration surface". Load: `src/github_actions_toolkit/config.py`
(`PublishConfig` parses `publish.yml`), `plan.py` (version math).

End state:

- `publish.yml` carries `images:` — one entry per version line, keyed by unit
  name (bake service name minus the `-cuda`/`-rocm` variant suffix), with
  required `major_minor` — and `bundles:` — name, ref(s), `major_minor`, and
  `sources` (the compose source paths; new information stated nowhere else).
- Unit-tag derivation strips variant suffixes; all variants of a unit share one
  version line and one `{unit}-v{version}` git tag.
- Version math reuses the package rules: highest `{unit}-v*` + patch, forced
  publish when the declared `major_minor` sits above the line.
- Tag-everything: every release aliases the full image set — changed units at
  their new version, unchanged at their existing one (a metadata no-op).

Verify: suffix-stripping derivation; version math including forced publish;
schema refusals (missing `major_minor`, unknown keys).

## Phase 7 — Change detection

Cluster P2 · github-actions-toolkit · ADR Part 2, "Change detection via the
shelf" and "Compose bundles". Load: `verbs/release.py` (`pull_digest_manifest`
and its shard enumeration).

End state:

- Per image unit: resolve the highest `{unit}-v*` git tag → its commit → pull
  that SHA's digest manifest from the shelf → compare the recorded `tree-` tags
  against the certified head's. Any variant's tree tag differs ⇒ ship and bump;
  no tag yet ⇒ first release ships.
- The toolkit imports no docker-devkit anywhere in this path.
- Stable ships when any package, app, image unit, or bundle changed; the dev
  gate tightens from manifest-presence to the same change-based gate (a
  docs-only landing in an images-only repo cuts nothing).
- Bundle gate: any image unit advanced OR path-diff over `sources` (which
  includes `docker-devkit.lock` — a runtime third-party bump changes bundle
  bits).

Verify: stubbed shelf — changed, unchanged, first-release, missing-shard;
nothing-to-ship including images and bundles; the bundle-gate trio
(image-advanced, source-diff, lock-diff).

## Phase 8 — Release verb channels

Cluster P2 · github-actions-toolkit · ADR Part 2, "Channels and spellings",
"`:latest` repoint", "Release-note surfaces". Load: `verbs/release.py` (stage
machinery), the note-rendering modules.

End state:

- Stable: alias each unit's variant digests onto `{version}` and `latest`; push
  the `{unit}-v{version}` git tag; alias the bundle's certified `{sha}` artifact
  onto `{version}`; push the `{bundle}-v{version}` git tag.
- Dev: alias changed units onto `{base}-dev.{count}`; unchanged units alias
  nothing (their stable tag already resolves); no git tags on dev.
- PR: no image version tags, permanently; PR bodies keep `tree-` tag links.
- `latest` is aliased by stable only; dev never aliases it.
- Release notes: pr/dev/stable render from one spine; the images table is
  `Image | Version` (the changed/unchanged pattern); the bundle row links the
  version page; the `tree-` tag stays one click away on the version page.
- All aliasing goes through the phase-5 primitive; push-once discipline holds
  under the release mutex; the shelf is SHA-keyed, so re-runs alias identical
  digests.

Verify: aliasing calls against a stub registry; `latest` on stable only; dev
aliases changed units only; note rendering for the three channels.

## Phase 9 — Publish-side cleanup release

Cluster P2 · docker-devkit + github-actions-toolkit · ADR Part 2, "`:latest`
repoint" and the branch-tag retirement in "Compose bundles". Load:
`publish_compose.py` (the `--branch` flag and the `crane tag` aliasing),
`documents.py` (`render_bake` derived tags), the toolkit reusable that invokes
`publish-compose --branch`.

End state:

- `render_bake` emits no derived `:latest` in push mode; local load-mode builds
  keep theirs. `latest` exists only as the stable alias.
- `publish-compose` publishes `{sha}` only; branch tags and the `--branch` flag
  are gone; `--plan` output is unchanged.
- The toolkit's reusable workflow stops passing `--branch`. Consumers relock
  devkit and bump the toolkit pin in one commit (the adoption wave), so no CI
  window ever passes a dead flag.
- docker-devkit ships this as its second patch release.

Verify: `render_bake` golden tests for push vs load mode; sha-only publish tests;
the toolkit reusable renders without the flag.

## Phase 10 — Qualified selection grammar

Cluster P3+P4a · docker-devkit · ADR Part 3, "Qualified selection names". Load:
`config.py` (services schema, the flat-vocabulary collision law), `compose.py`
(`BUNDLES=` resolution), `tests/test_config.py`, `tests/test_compose.py`.

End state:

- Every `BUNDLES=` entry is qualified `group-dash-option` — no unqualified short
  forms even when unambiguous; bare entries ride unconditionally as today.
- The flat-vocabulary collision law is repealed: qualified names are
  structurally unique (group names unique as dict keys, option names unique
  within a group); two groups may each carry a `cuda` option.
- At-most-one-option-per-group refusal unchanged; unknown names refuse loudly
  listing the qualified vocabulary; a bare entry's name refuses as unselectable.
- Breaking change; ships in the phase-14 release and is adopted in the wave.

Verify: qualified resolution; two groups each carrying same-named options;
at-most-one refusal; unknown-name vocabulary listing; bare-name unselectable
refusal.

## Phase 11 — Bundle pinning through the lock

Cluster P3+P4a · docker-devkit · ADR Part 3, "Ref grammar and lock entries".
Load: `config.py` (the entry `ref:` grammar), `build_docker.py` (the lock
writer), `documents.py` (`parse_image_lock`), `mirror.py`.

End state:

- `ref:` carries a tag, never a digest — a digest spelling refuses at config
  load. Any published channel spelling is a legal pin (stable semver, dev,
  `{40-hex}` sha); `latest` refuses; range specifiers refuse.
- `build --lock-only` writes `{QUALIFIED_NAME}_BUNDLE={reference}@sha256:…` per
  bundle declaration — the upper-cased qualified name plus `_BUNDLE` (bare
  entries included: `LIVEKIT_BUNDLE`). Declaration-incremental and `--upgrade`
  semantics identical to image entries; `_BUNDLE` keys collision-refuse against
  `base_images` keys at load.
- A version-tagged ref without its lock entry refuses loudly naming
  `build --lock-only` — the charts law, verbatim.
- Bundle refs resolve direct — org ghcr, un-prefixed; the `mirror` verb never
  enumerates bundle declarations.

Verify: digest/`latest`/range refusals; lock-key derivation including bare
entries; declaration-incremental copy-forward, tag-bump re-resolve, `--upgrade`;
collision refusal; missing-entry refusal naming the relock.

## Phase 12 — Lock probing at bring-up

Cluster P3+P4a · docker-devkit · ADR Part 3 plus Closed decision 12. Load:
`compose.py` (the probe and include materialization), `crane.py`.

End state:

- `up` probes `_BUNDLE` lock entries via `crane digest` (compose-bundle media
  types are not image indexes; `imagetools inspect` may refuse them), under the
  same stderr-classification law as image probes.
- `up` includes the lock's digest ref; the yaml tag is never used directly at
  bring-up.

Verify: probe pass/fail/not-mirrored classification for bundle entries; the
materialized root carries digest refs.

## Phase 13 — Local bundle overrides

Cluster P3+P4a · docker-devkit · ADR Part 4, Stage 1. Load: `compose.py`,
`host.py` (`parse_env_file`), `door.py` (the reserved-name law),
`documents.py` (`ComposeDocument`).

End state:

- `.env` may carry `BUNDLE_OVERRIDE_<QUALIFIED_NAME>` per machine — one variable
  per override, commentable per-line, never committed. Value grammar: a
  filesystem path (relative resolves against the consumer repo root; absolute
  legal) or an `oci://` ref of any channel.
- Unknown names refuse loudly listing the vocabulary; a known-but-unselected
  name is inert.
- An overridden option skips its lock-entry probe and its lock-entry
  requirement — testing an unpublished bundle is the point.
- Path form carries full local-output teeth: must exist and parse; a `door`
  service refuses; service-name collisions with other selected sources refuse;
  route/port validation runs unchanged.
- Path form attaches the producer's `docker-devkit.lock` as an extra
  `--env-file`, derived from the override path's fixed
  `stack/generated/compose/` layout (three parents up), ordered so the
  consumer's own declarations win any shared key — pin the precedence against
  the version-pinned compose binary in tests. The ref form attaches nothing
  (registry bundles had their holes filled at publish).
- The loop this buys: producer commit → `generate-score` + `build` (tree-tagged
  images land in the local daemon) → consumer `uv run up` — `--pull missing`
  already serves local-only tree tags; the unit of iteration is commit → build.

Verify: both value forms; the refusal list; the inert override; probe and
requirement skip; producer-lock attachment and ordering; door and collision
teeth.

## Phase 14 — Consumer-surface release

Cluster P3+P4a · docker-devkit · ADR "Sequencing", step 3.

End state: one docker-devkit patch release carrying phases 10-13. Consumers
relock only in the wave — the grammar flip breaks `.env` spellings, so relock
and flip are atomic per repo.

Verify: the battery green; the release published.

## Phase 15 — placeframe adoption

Adoption wave · placeframe · ADR "Consumer adoption". Load: placeframe's
`publish.yml`, `docker-devkit.yaml`, `.env` (and any committed example), CI
workflows.

End state:

- `publish.yml` gains `images:` (13 units) and `bundles:` (`placeframe`; variants
  cuda/rocm; the compose source paths).
- One adoption commit: relock onto the phase-14 devkit release, bump the toolkit
  pin, and flip `.env` `BUNDLES=` to qualified spellings
  (`core-cuda,auth-keycloak,tunnel-ngrok`). No `--branch` usage survives.
- The unused `.dockerignore` allowlist entry
  `packages/generated/csharp/api-client/` is removed — it re-keys nothing under
  precision and shrinks every build context.
- The first release after adoption mints image and bundle versions; the release
  body carries the `Image | Version` table and the bundle row.

Verify: CI green through the first release; versions resolvable in ghcr; the
`packages/generated` entry gone from `.dockerignore`.

## Phase 16 — Make-it-Sing adoption and tag hygiene

Adoption wave · Make-it-Sing · ADR "Consumer adoption" and "Hygiene". Load:
Make-it-Sing's `publish.yml`, `docker-devkit.yaml`, `.env`, `docker-devkit.lock`.

End state:

- `publish.yml` gains `images:` for `livekit-token`; `:latest` references are
  gone.
- `.env` flips to `BUNDLES=placeframe-cuda,tunnel-rathole`; the yaml bundle ref
  flips from digest to version tag; `build --lock-only` writes the
  `PLACEFRAME_CUDA_BUNDLE` and `TUNNEL_RATHOLE_BUNDLE` entries.
- Stale git releases and tags `pr-4-237`, `pr-4-238`, `pr-6-250` are deleted;
  `pr-5-248` and `pr-7-254` are checked against still-open PRs first and deleted
  if orphaned. Deletion needs contents-write — hand off to the operator if the
  session token cannot.

Verify: CI green; the lock diff shows the two `_BUNDLE` entries; the stale tags
are gone.

## Phase 17 — capture-tool adoption; infra at leisure

Adoption wave · placeframe-capture-tool, infra · ADR "Consumer adoption".

End state:

- capture-tool: `images:` for its workloads; `:latest` removal; `.env` flip;
  relock onto the phase-14 devkit release.
- infra: its three `tree-` pins switch to version tags — no deadline; the frozen
  tree pins remain valid indefinitely.

Verify: CI green per repo.

## Phase 18 — Cross-repo dev overlay (trigger-gated)

P4b · docker-devkit · ADR Part 4, Stage 2, and Closed decision 10.

Build only when the trigger holds: the phase-13 commit → rebuild loop proves too
slow in daily consumer-driven development. A session taking this phase first
confirms the trigger; otherwise it does not run.

End state:

- With a path-form override active, `up --dev` generates overlay entries from
  the producer's declaration: the producer's dev-stage services, the producer's
  repo root as the bind source (the same derivation the producer-lock attachment
  uses), and the producer's `dev-toggles` as `${NAME:-default}` holes — overlaid
  onto the consumer's assembly. `:dev` images come from the producer's
  `build --dev`.

Verify: overlay generation over a producer/consumer fixture pair; toggle holes
present; bind sources point at the producer root.
