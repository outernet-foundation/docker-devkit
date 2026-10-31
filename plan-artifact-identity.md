# Artifact identity: images and compose bundles

Pre-implementation ADR, five linked initiatives: give `docker-devkit.lock` a
platform-resolved, structured-YAML shape (Part 0); give `tree-` tags a
precise, fail-closed input closure in docker-devkit (Part 1); give images and
compose bundles per-unit semver release lines in github-actions-toolkit
(Part 2); let consumers pin external compose bundles by version tag through
`docker-devkit.lock` (Part 3); and give consumers a per-machine
local-override surface for iterating on producer checkouts (Part 4). This
file absorbs and replaces `plan-image-identity.md` and
`plan-platform-resolved-lock.md` (both deleted in absorbing commits; git is
the history), capturing investigation sessions 2026-10-15 and 2026-10-22,
the consumer-consumption rulings of 2026-10-30, and the platform-resolved
lock rulings of 2026-10-31. All open questions are closed and
recorded at the end. The compose consumer model's present truth lives in this
repo's `AGENTS.md`; this ADR records the changes to it. Code anchors are as of
their capture date and may drift.

## Orientation: the tagging system this completes

Two tag grammars with two jobs, across three release channels:

```
git tags          pr                      dev                      stable
────────────────────────────────────────────────────────────────────────────
release event     pr-{N}-{count}          dev-{count}              YYYY.MM.N
                  swept at merge/close    swept at next stable     permanent
unit version      —                       —                        {unit}-v{semver}
                  (packages, apps today; images and bundles post-Part-2)

registry          pr                      dev                      stable
────────────────────────────────────────────────────────────────────────────
images            tree-{hash} only        {base}-dev.{count}      semver + latest
                  (content identity,      (post-Part-2)           (post-Part-2;
                  all channels)                                    latest repointed)
shelf (ORAS)      sha-{full}[-{key8}]     read back for stapling  read back
packages          —                       {base}.dev{count} /     {base}
                                          -dev.{count}
compose bundle    {sha} only              {base}-dev.{count}      semver
                  (post-Part-2; branch tags retired)
consumer pin      —                       —                        {version} in
(bundles,                                                                   docker-devkit.yaml,
post-Part-3)                                                                digest in the lock
```

Standing principles: one ordinal (commit count at the certified head) keys
everything per-event, so `pr-{N}-{count}` and the landing's `dev-{count}` share
the number because they are the same bits at two lifecycle stages. The PR
channel publishes nothing permanent anywhere — only content-keyed ephemera
(`tree-`, `sha-`). Registry-side channel markers exist only where the registry
forces one (npm dist-tag; image and bundle dev spellings). Post-Part-2 every
consumable artifact type carries a per-unit semver line; the CalVer git tag
stays the release-event grouping label. Post-Part-3 every org-internal
artifact a consumer declares resolves by tag in config to a digest in the
lock — the same law for base images, charts, and bundles. Post-Part-0 the
lock is structured YAML recording, per image, the index digest plus one
concrete manifest digest per declared platform — arch is never resolved by
whichever machine touches an image.

## Part 0 — docker-devkit: platform-resolved lock

### The defect being fixed

`build`'s lock writer resolves a declared tag to its top-level digest — for a
multi-platform tag the **index** digest (the manifest list), not a
per-platform manifest. A consumer that pulls the pinned ref without a
platform expression gets whichever platform the *resolving machine* runs:
capture-tool's save/load ship path resolves on the amd64 build host and
delivers amd64 loki/alloy to the arm64 ZED box (`exec format error` on every
boot since first deploy; diagnosis in placeframe-capture-tool's
`zed-aoa-diagnosis-handoff.md`). Pull-on-target consumers are unaffected —
the receiving daemon resolves the index for its own arch — so the defect
stays invisible until a middleman ships across arches. The keycloak incident
(`plan-mirror-digest-retention.md`) independently recorded the same fact:
lock digests are list digests.

### The law

Same inputs (config + lock) produce the same bytes on any machine, any arch,
any day. A machine's own architecture participates only in the final act of
executing bytes. The defect is the one place resolution consults the
resolving machine implicitly; this part removes the last implicit arch
decision.

### Design

1. **`platforms` is required whenever `base_images` is declared** — refuse at
   `load_config`. It stops being an optional build hint and becomes the
   explicit deploy-target declaration. Charts are exempt (arch-neutral OCI
   artifacts); bundles, when Part 3 adds them, are exempt likewise.
2. **The lock becomes structured YAML** — per image the index digest plus one
   concrete manifest digest per declared platform; charts and bundles stay
   flat refs:

   ```yaml
   images:
     LOKI_IMAGE:
       index: ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@sha256:…
       platforms:
         linux/arm64: ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@sha256:…
   charts:
     KEYCLOAK_CHART: oci://ghcr.io/…@sha256:…
   ```

   The platform axis cannot live in env-var grammar: flat compound keys
   (`LOKI_IMAGE_LINUX_ARM64`) encode schema as string discipline, scatter
   selection into every consumer, and cannot express index-vs-dish
   distinctions. The env-line format was adequate at one axis; its two
   documented virtues — native `--env-file` consumption at the `up` handoff
   and the raw-`docker compose` valve — are preserved by synthesis (below).
   The committed lock becomes the complete truth: every exact byte-stream the
   system can run, and for which machines, readable without touching a
   registry.
3. **The resolver records the complete resolution** against the upstream
   reference: index digest plus one concrete manifest digest per declared
   platform. Selection rules: exact `os`/`architecture` match;
   `unknown/unknown` and attestation-annotated entries skipped; multiple
   matches refuse loudly naming the entry; single-manifest refs and
   `@digest` declarations record the manifest uniformly as index and sole
   platform entry. Resolution is a pure function of config — the writing
   machine's arch is never consulted.
4. **Selection centralizes in one function** (an `entry_for(lock, name,
   platform)` shape): the compose/bake edges, `install-zed`, and the markers
   all call it; no consumer constructs keys or picks digests itself.
5. **Env synthesis at the consumption edges**: `up`/`down` materialize a
   per-run env file (the selected platform entries) attached as the second
   `--env-file` — same temp lifecycle as the materialized root compose file,
   printed and kept on failure like the bake file. Bake receives the same
   synthesis in-process. A `lock-env` verb emits the synthesized env on
   stdout for the raw-`docker compose` pressure valve. Part 4's
   producer-lock attachment consumes the same synthesis instead of attaching
   the lock file raw.
6. **Platform selection at the up edge is declaration-driven**: a config
   `platforms` singleton selects itself; a multi-platform repo's `.env`
   names `PLATFORM=` (committed per deploy target). Never host introspection.
7. **Markers resolve by substrate**: `lock:`/`lock-image:` under k8s resolve
   the **index** (kubelet resolves per node — architecturally correct);
   compose-side resolution selects the platform dish.
8. **The mirror verb probes every recorded digest** (index plus dishes) —
   presence by exact bytes. The copy itself is unchanged: whole index, dishes
   ride along; `crane copy` byte-identity keeps upstream and mirror digests
   equal. The retention fix sketch's digest-aliases
   (`plan-mirror-digest-retention.md`) compose with this rule — adopted, they
   would keep dishes GC-safe the same way they keep indexes.
9. **The writer is deterministic**: sorted images/charts and platform keys,
   byte-stable across machines — the `--lock-only --check` gate stays
   hermetic on any-arch runner.

### Migration

No dual-read: the typed loader swaps wholesale, and the devkit version bump
turns every consumer's `--check` gate red until each repo relocks once — the
YAML rewrite re-resolves every entry, so the declaration-freeze/`--upgrade`
migration question dissolves in the cutover. capture-tool relocks to arm64
dishes and deploys the ZED box; placeframe adds `platforms: [linux/amd64]`
and Make-it-Sing declares its platform at the same relock (the config
refusal forces the one line). Ships inside the cluster-P1 release (see
Sequencing) so Part 1's hasher and every later consumer build against the
final lock shape — one format migration, one relock wave, not two.

### Verification owed

- ghcr serves child manifests by digest — a pull and a `crane digest`
  against the mirror by a recorded dish digest. Children's existence inside
  the copied index is verified; per-digest retrieval is not.
- Spot-check a relocked entry: `buildx imagetools inspect --raw <digest>`
  answers a manifest, not an index.

## Part 1 — docker-devkit: precise input closure for `tree-` tags

### The defect being fixed

`compute_service_shas` (`documents.py`) hashes per-service synthetic git trees
over the `.dockerignore`-allowlisted committed tree. The partition rule drops
leftover manifest files and `.dockerignore` from every hash, excludes
`additional_contexts` dependency dirs from dependents' hashes, and nothing
forbids a cross-service `COPY`. Concrete harms: a base-image bump produces a
numerically different localizer with the same `pipeline_version` (silent
calibration-corpus contamination); movable `tree-` pins; a tag that presents
as content-addressed but omits real inputs.

### The design: attribution, not buckets

The hash input set is computed per service by attributing the Dockerfile's
actual consumption — the Dockerfile is the declaration; no input declarations
in `docker-devkit.yaml` (a config-declared input list is a second source of
truth that drifts, and verifying it against the Dockerfile requires the
parser anyway).

Per-service hash inputs:

1. The service's Dockerfile itself. Any recipe change re-keys, which keeps the
   closure self-consistent: a new `COPY` line moves the hash at the same
   moment the newly copied files enter the resolved set.
2. The resolved `COPY`/`ADD` source set — every context path or glob the
   Dockerfile actually copies, resolved against the allowlisted tree. Shared
   roots enter a service's hash only when copied (`packages/python/common/**`
   for the api; nothing for the gateway).
3. The ARG-consumed image-lock lines — intersect the Dockerfile's `ARG`
   declarations with lock names; hash only those lines. Conservative in the
   safe direction: declared-but-unused args still hash their lines. Lock lines
   consumed only at runtime by compose (never declared as build args) drop
   out of build hashes entirely.
4. The service's own `build` mapping from the bake manifest, serialized from
   the parsed document (YAML anchors already resolved) — not the whole file,
   so another service's edit cannot re-key. Tag lists do not affect bits and
   stop re-keying anything.
5. Dependency inputs as embedded tree hashes — instead of re-listing a
   dependency's files inside dependents' hashes, embed the dependency's own
   computed tree hash as a synthetic entry (hash-of-hash; the
   `additional_contexts` graph is a DAG, no circularity). The union-find walk
   exists as `dependency_groups` (`matrix.py`); lift it to the shared
   documents layer and consume it here.

`.dockerignore` drops out of the hash entirely: an edit that changes what a
`COPY` glob matches changes the resolved set and re-keys exactly the affected
services; an edit that changes nothing matched changed no bits, so no re-key
is correct. The resolved set absorbs it.

### The fail-closed construct guard

Precision is safe only if the construct inventory is complete, so the parser
fails closed: any Dockerfile or manifest construct that could read the build
context and cannot be fully attributed is a loud build failure, never a silent
gap. The context-reading constructs to enumerate: `COPY` from context, `ADD`
with a local path (URL-form `ADD` is network, out of scope like apt), `RUN
--mount=type=bind,source=…` (reject), path-form `additional_contexts`
(reject; only `target:` form is attributed). Unknown syntax, heredoc `COPY`,
and unresolvable globs fail loudly. The same scan rejects cross-service `COPY`
sources outside {own dir} ∪ {copied shared roots} ∪ {declared dependency
outputs} — the G5 guard, absorbed into the attribution pass.

### Dirty-tree guard

The hash is over the committed tree while builds compile the working tree.
Post-fix this is the only remaining way the tag can lie, so content-addressed
build paths refuse a dirty tree: `build` and `up --build` run `git status
--porcelain` filtered to the allowlisted set (modified and untracked
allowlisted files both count) and fail loudly listing the offending paths.
`build --dev` is exempt: `:dev` tags are not content-addressed, so the
lie-risk the guard kills does not apply, and the bind-mount dev workflow
inherently runs against a dirty tree. Bare `up` (no build) stays permitted —
it pulls images by committed-tree tag and is self-consistent.
`generate-score` and `publish-compose` run in CI on clean checkouts; the
guard there is a no-op.

### Over-inclusion fates

- Lock bump: re-keys only services declaring that base's arg. A torch bump no
  longer patch-bumps the gateway.
- Manifest edit: re-keys only the edited service. Global non-bit keys
  (`x-cross-compile-targets`, `x-registry-cache`, tag lists) re-key nothing.
- Shared roots: re-key exactly the copying services.
- Allowlist extras (placeframe's `packages/generated/csharp/api-client/` is
  copied by zero of 47 COPY lines): re-key nothing; cost is context upload
  size only.
- Docs inside service dirs: not over-inclusion — most Dockerfiles copy the
  service dir wholesale, so the doc is in the bits; the fix is selective
  COPYs, not hash machinery.

### Soundness argument

Context files reach image bits only through `COPY`/`ADD` (both resolved),
declared args (hashed conservatively), `FROM` bases (lock lines,
digest-pinned so upstream drift cannot change bits without a lock edit, which
re-keys), and `additional_contexts` outputs (embedded dep hashes). The guard
converts any unattributable construct into a loud failure. Environment
channels are deliberately uncovered: buildx/docker engine version, runner
quirks, network fetches during build (apt mirror drift). Consequence: a
same-tree rebuild can yield a different digest, and ghcr tags are mutable, so
a rebuild can overwrite a `tree-` tag onto fresh bytes — accepted residual;
the mitigation that matters is Part 2's release-time semver alias, which
points at the certified digest and never moves.

Variant subtlety: `reconstructor-cuda` and `reconstructor-rocm` share one
Dockerfile dir and therefore one tree tag — the variant difference is a
manifest arg, covered by the manifest-mapping input, so both variants hash
identically. Sound because tree tags are per-repository unique (the full ref
disambiguates), a variant-arg edit re-keys both, and Part 2 gives variants
one shared version line, so advancement stays variant-synced.

### One-time re-key choreography

- Every image gets a fresh `tree-` tag at its next build; old tags freeze in
  place (CI never re-pushes them) — existing pins convert from movable to
  actually-immutable.
- placeframe's committed `stack/generated/**` embed old tags: one deliberate
  regeneration cycle after relocking docker-devkit (commit the devkit bump,
  regenerate, commit artifacts; the score staleness gate is disabled there,
  so this is operator discipline).
- infra's three `tree-` pins keep resolving to frozen old digests; re-point
  at leisure (or adopt Part 2 version tags).
- Calibration corpus rows keep their old `pipeline_version` values; the
  mismatch hard-fail on redeploy is existing behavior for any re-key.
- Refresh the stale comment in `matrix.py` claiming `neural-networks-base-*`
  are tagless.

### Delivery

One docker-devkit patch release (pre-1.0, breaking rides the patch flow),
then relock in placeframe, placeframe-capture-tool, Make-it-Sing. Precision
must land as Part 1 directly — coarse-then-precise would mean two input-set
changes, two re-key events, two regeneration cycles. Estimated 3–5 days
including the test battery.

### Tests

The over-inclusion list becomes the negative-test list: lock line for an
undeclared base edited → unaffected services unchanged; other service's
manifest arg edited → this service unchanged; shared-root file not copied by
a service edited → that service unchanged; `.dockerignore` edit that matches
nothing → no re-key; own-dir edit → re-key; dependency dir edit → dependent
re-keys; declared-arg lock edit → re-key; cross-service COPY → loud failure;
`RUN --mount=type=bind` → loud failure; path-form `additional_contexts` →
loud failure; dirty tree on `build`/`up --build` → loud failure listing
paths; dirty tree on bare `up` and on `build --dev` → permitted.

## Part 2 — github-actions-toolkit: per-unit semver image and bundle lines

### Version space: per-unit semver

Repo-level CalVer was rejected in favor of per-image semver lines: the
consumable-image ecosystem is semver-dominant (this org's own declarations
consume grafana, caddy, keycloak — all semver); automated-consumption
machinery (Renovate's docker manager, Flux `semver` image policies,
dependabot) is semver-native; and the expected repo splits make org images
cross-repo consumables that the lock resolves by tag exactly like third-party
images. Semver's compatibility contract maps to image surfaces (env holes,
ports, service names, volume layout): major = breaking contract, minor =
additive, patch = fix, human-asserted via `major_minor` — the same trust
model packages use.

Tag-everything semantics: every release aliases the full image set — changed
images at their new version, unchanged at their existing one (a metadata
no-op) — so complete sets exist at every release and "newest tag for image X"
is always its highest semver. The release event keeps its CalVer git tag as
the grouping label; apps stay on their existing per-app semver (a CalVer
conversion for apps was considered and retired).

### Declaration surface: `images:` in publish.yml

One entry per version line, keyed by the unit name: the bake service name
with the `-cuda`/`-rocm` variant suffix removed. `major_minor` per unit,
required, same meaning as packages. All variant repositories of a unit
receive the same version tag (`reconstructor-cuda:0.1.3` and
`reconstructor-rocm:0.1.3` share the `reconstructor` line and the
`reconstructor-v0.1.3` git tag). placeframe has 15 services / 13 units.

### Change detection via the shelf — no docker-devkit dependency

The release verb does not import docker-devkit. Per unit: resolve the highest
`{unit}-v*` git tag → its commit → pull that SHA's digest manifest from the
shelf → compare tree tags (recorded in each entry's `tags[]` by the build
push) against the certified head's. Differing tree tag in any variant ⇒ ship
and bump patch (or the declared `major_minor` sits above the line, forcing
the bump — the packages' forced-publish rule). No tag yet ⇒ first release
ships. All primitives exist: `pull_digest_manifest` takes an arbitrary SHA
and is a build-artifact-registry dependency the toolkit already has. The dev
channel compares against the highest stable tag's commit, exactly like
package path-diff.

Nothing-to-ship gates: stable ships when any package, app, image unit, or
bundle changed — an images-only repo cuts releases per promotion that changes
image inputs (promotion cadence is operator-controlled; this matches the
package discipline). Dev tightens from manifest-presence to the same
change-based gate; a docs-only landing in an images-only repo cuts nothing.
Digest-based detection was rejected: rebuild churn re-digests unchanged trees
and would bump versions on noise; tree tags are source-precise post-Part-1.

### Channels and spellings

- Stable: alias each unit's variant digests onto `{version}` and `latest`;
      push the `{unit}-v{version}` git tag.
- Dev: alias changed units onto `{base}-dev.{count}` (npm spelling; valid tag
      grammar). Unchanged units need nothing — their stable tag already
      resolves. Dev bodies show the dev spelling when changed, the current
      stable tag otherwise, mirroring the packages table. No git tags on dev.
- PR: no image version tags, permanently. PR digests become dev and stable
      digests after landing; ghcr deletes tags per digest-version, so any
      `pr-*` image tag sweep would endanger stable tags on shared digests —
      "additive later" would be hazardous later. PR bodies keep `tree-` tag
      links.

### `:latest` repoint

Today `latest` is pushed by every PR build and points at uncertified heads —
the one cell violating the PR-column principle. PR builds stop pushing the
derived `latest`; the stable aliasing includes it alongside the version.
`latest` becomes honestly "newest released version of this image." Dev never
aliases it.

### Aliasing mechanism (verified)

New primitive in build-artifact-registry (org law: the toolkit owns no
registry CLI code), beside `promote_scope`: resolve `ref@digest` from the
shelf manifest, `oras tag` the new tags. Verified live 2026-10-22 against
`registry:2` with the pinned oras 1.2.2: OCI image index aliases preserve
digest and media type; single manifests work; multiple tags per call work;
missing references fail loudly. ghcr itself was not push-tested from the
sandbox (no `packages:write` token) — risk negligible; `docker buildx
imagetools create -t` is the documented one-line fallback. Stable delivery
jobs already carry `packages: write`; push-once discipline holds under the
release mutex; the shelf is SHA-keyed so re-runs alias identical digests.

### ghcr semantics to respect

ghcr's model is version-level (a version = a digest); tags are attributes.
Tags on one digest coexist; deleting a version kills all its tags. A stable
release's certified digest already carries its dev spelling — dev image tags
can never be swept the way stable sweeps `dev-*` git releases. Therefore no
tag GC, and retention policies are a hazard: no retention policies on image
repos carrying release tags; accept accumulation; any future sweeper must be
version-level and release-protecting.

### Compose bundles: `bundles:` in publish.yml

The bundle is the stack-as-a-unit and the artifact consumers actually include.
It gets its own semver line — the Helm chart analog (a chart is a bundle of
pinned images with contracts; Helm mandates strict semver) — versioned by the
toolkit, not placeframe-locally.

- Declaration: `bundles:` section — name, ref(s), `major_minor`, and
  `sources` (the compose source paths; unlike the rejected Dockerfile input
  declarations, compose sources are stated nowhere else, so declaring them is
  new information, not duplicated state). Variants (`placeframe-cuda`,
  `placeframe-rocm`) share one line.
- Change detection: any image unit advanced OR path-diff over `sources`
  (which includes `docker-devkit.lock` — a runtime third-party bump like a
  loki upgrade changes bundle bits without any local tree moving).
- Mechanism: release-time aliasing with the same verified primitive. Every
  certified head's bundle is already published under an immutable `{sha}` tag
  by its PR run (`publish_compose.py` publishes on PR heads and landings), so
  dev and stable alias `placeframe-{variant}:{sha}` onto `{base}-dev.{count}`
  / `{version}` with no new publish job and no ordering problem. Stable
  pushes the `{bundle}-v{version}` git tag.
- Branch tags retired: PR runs currently stamp the PR branch name and
  landings stamp `dev`/`main` — movable pointers accumulating per PR, the
  same disease as PR-moved `latest`. `publish-compose` publishes `{sha}`
  only; the `--branch` flag dies; release-verb aliases carry all channel
  meaning.
- Consumers no longer hand-copy digests at all (Part 3 replaces that
  discipline); the release body gains a Bundle row (version linked to the
  ghcr version page) as the human and Renovate discovery surface.

### Release-note surfaces

The images table becomes `Image | Version` — the changed/unchanged pattern of
the packages table, strictly more informative than a uniform stamp. The
`tree-` tag remains as content identity one click away on the version page.
The three per-run surfaces (pr/dev/stable) render from the same spine.

### Tests

Unit-tag derivation (variant suffix stripping); version math reuse (highest
`{unit}-v*` + 1, forced publish on declared-line-above-tags); shelf-based
tree-tag comparison (changed, unchanged, first-release, missing-shard);
nothing-to-ship including images and bundles; aliasing calls against a stub
registry; bundle gate (image-advanced, source-diff, lock-diff); `latest`
aliased on stable only.

## Part 3 — docker-devkit: consumers pin bundles by version through the lock

### The defect being fixed

Make-it-Sing consumes placeframe's published compose bundle by hand-copying a
digest into `docker-devkit.yaml`:

```yaml
placeframe:
  options:
    cuda:
      ref: oci://ghcr.io/outernet-foundation/placeframe/cuda@sha256:d5a0ae…
```

Every producer change forces push → CI publish → hand-copy digest → commit →
reup. Meanwhile `base_images` and `k8s.charts` already have the right shape:
yaml declares a human-meaningful tag, the lock resolves it to a digest, and
`build --lock-only` is the only writer. Bundles become the third instance of
that law. This un-defers the item the absorbed plan deferred ("docker-devkit
lock support for `include: oci://` refs: future, not now") — the future
arrived with the first real consumer.

### Qualified selection names

The group is the namespace — that is what a group means (a set of mutually
exclusive alternatives: `placeframe` = {cuda | rocm}). The selection grammar
becomes qualified names, uniformly:

```
BUNDLES=placeframe-cuda,tunnel-rathole
```

- Group-dash-option, always, for every option — no unqualified short forms
  even when unambiguous: one grammar rule beats a conditional rule; a group
  gaining a second option would break every bare spelling (rename-by-addition
  hazard); one spelling per bundle greps.
- Bare entries are their own root (`livekit`) and ride unconditionally, as
  today.
- The flat-vocabulary collision law is repealed: qualified names are
  structurally unique (group names unique as dict keys, option names unique
  within group), so two groups may each carry a `cuda` option —
  `placeframe-cuda` and `otherplace-cuda` cannot collide, and no hand
  uniquing (`pf-cuda`) is forced on authors.
- At-most-one-option-per-group refusal is unchanged (naming two qualified
  options of one group refuses naming the group); unknown names refuse loudly
  listing the qualified vocabulary.
- This is a breaking change to the selection grammar and rides the Part 3
  devkit release, so consumers migrate `.env` once (see Adoption).

### Ref grammar and lock entries

- The `ref:` field carries a tag, never a digest:
  `ref: oci://ghcr.io/outernet-foundation/placeframe/cuda:0.3.1`. A digest
  spelling refuses at config load — the hand-copy workflow this part kills
  becomes unrepresentable, not merely discouraged.
- Any published channel spelling is a legal pin — stable semver
  (`0.3.1`), dev (`0.3.1-dev.42`), sha (`{40-hex}`) — matching how packages
  accept dev pins; channel-certification policing is out of scope. Exactly
  one denylist entry: `latest` is refused, the sole mutable spelling
  (repointed at every stable release); this is the mutability law the org
  already holds for images ("pin everything"), not channel discipline —
  PyPI has no `latest`, so this diverges from the packages model nowhere it
  exists.
- Exact tags only — no range specifiers (`>=`, `.x`). The lock resolves
  exact tags to digests; Renovate's docker manager bumps exact pins, exactly
  as it already does for `base_images` and charts. Dev/prerelease pins get no
  automated bump (Renovate skips prereleases by default) — the same behavior
  package dev pins see; accepted.
- Lock entries: `{QUALIFIED_NAME}_BUNDLE={reference}@sha256:…` — the
  qualified selection name upper-cased plus `_BUNDLE`
  (`PLACEFRAME_CUDA_BUNDLE`, `TUNNEL_RATHOLE_BUNDLE`, bare `LIVEKIT_BUNDLE`)
  — written by `build --lock-only`, declaration-incremental like every lock
  line (an unchanged tag copies its frozen digest forward; a tag bump
  re-resolves; `--upgrade` re-resolves everything), collision-refused
  against `base_images` keys at load.
- A version-tagged ref without its lock entry refuses loudly naming
  `build --lock-only` — the charts law, verbatim.
- First-party artifacts resolve direct — org ghcr refs, un-prefixed. The
  mirror is for upstreams that can hurt us; ours can't, so ours don't get
  copied. The `mirror` verb never enumerates bundle declarations.
- `up`'s fail-closed probe stays one law over every lock entry; bundle
  entries probe via `crane digest` (compose-bundle media types are not image
  indexes; `buildx imagetools inspect` may refuse them).
- `up` includes the lock's digest ref; the yaml tag is never used directly at
  bring-up.

### What the loop becomes

Producer release mints `{version}` on the certified bundle digest; the
consumer's upgrade is a yaml tag bump (Renovate-native) plus a relock, with
the digest movement visible as a one-line lock diff in review — no registry
pages, no hand-copying, no drift.

## Part 4 — docker-devkit: local bundle overrides

The other half of the loop: iterating on a producer checkout against the
consumer stack without publishing anything. The exact artifact
`publish-compose` publishes already exists on disk in the producer at
`stack/generated/compose/compose.{option}.yaml`; the OCI bundle differs only
in publish's two finishing passes (lock-hole substitution, own-repo digest
pinning) — both exactly what a local consumer does not want.

### Stage 1 — the `.env` override

Per-machine, never committed (`.env` is already that surface — gitignored,
carries `BUNDLES=`, ports, tokens):

```
BUNDLE_OVERRIDE_PLACEFRAME_CUDA=../placeframe/stack/generated/compose/compose.cuda.yaml
```

- Spelling: `BUNDLE_OVERRIDE_<QUALIFIED_NAME>` — ALL_CAPS throughout, one
  variable per override, commentable per-line; the same qualified identity as
  `BUNDLES=` and the lock key.
- Value grammar, two forms: a filesystem path (relative resolves against the
  consumer repo root, so `../placeframe/…` works anywhere the repos are
  siblings; absolute legal) or an `oci://` ref of any channel — the ref form
  tests a colleague's landed-but-unreleased bundle from the registry without
  local builds and without dirtying the tree.
- Unknown names refuse loudly listing the vocabulary; a known-but-unselected
  name is inert — the override rides with selection, exactly like
  option-level routes/ports.
- An overridden option skips its lock-entry fail-closed probe and its
  lock-entry requirement — testing a bundle that may not be published at any
  digest yet is the point.
- The path form gets the same teeth as a local bundle output: must exist and
  parse, refuses a `door` service, service-name collisions with other
  selected sources refuse. Route/port validation runs unchanged (service
  names are identical, so the door resolves as before).
- Producer-lock attachment (path form only): the bundle's third-party holes
  (`${LOKI_IMAGE:?err}`, …) have no declaration source in the consumer, and
  lock lines are never hand-authored — so the producer's `docker-devkit.lock`
  attaches as an extra `--env-file`, derived deterministically from the
  override path's fixed `stack/generated/compose/` layout (three parents up),
  ordered so the consumer's own declarations win any shared key. The ref form
  needs no producer lock — registry bundles had their holes filled at
  publish.

The iteration loop this buys: commit in placeframe → `generate-score` +
`build` (tree-sha-tagged images land in the local daemon) → `uv run up` in
make-it-sing. `up`'s `--pull missing` already serves local-only tree tags —
pull only what is absent. Tree shas hash `git ls-tree HEAD`, so the unit of
iteration is commit → build, not save → reup; the expensive part of the old
loop (push, CI, digest-copy) is what disappears.

### Stage 2 — cross-repo dev overlay (staged, trigger-gated)

The faster inner loop that exists for single-repo stacks (`up --dev`:
dev-stage images without source, repo bind-mounted read-only into `/app`,
dev-toggle env holes) extended cross-repo: when a path-form override is
active, `up --dev` generates overlay entries from the producer's declaration
— the producer's dev-stage services, the producer's repo root as the bind
source, the producer's workload `dev-toggles` as `${NAME:-default}` holes —
overlaid onto the consumer's assembly. `:dev` images come from the
producer's `build --dev` (one-time plus dependency changes). The producer
repo root is the same derivation the producer-lock attachment already uses.

Trigger, not schedule: build when the commit→rebuild loop proves too slow in
daily consumer-driven development. The design is recorded here because it is
cheapest to capture while the producer-root derivation is fresh; the
implementation-plan session descopes it if the loop turns out fast enough.

## Consumer adoption

- placeframe: add `images:` (13 units) and `bundles:` (`placeframe`, variants
  cuda/rocm, compose sources) to `publish.yml`; stop pushing derived
  `:latest` on PR builds; `publish-compose` drops branch tags and the
  `--branch` flag; `.env` `BUNDLES=` flips to qualified spelling
  (`core-cuda,auth-keycloak,tunnel-ngrok`); one-time `stack/generated/**`
  regeneration after the Part 1 relock.
- Make-it-Sing: `images:` for `livekit-token`; `:latest` removal; `.env`
  flips (`BUNDLES=placeframe-cuda,tunnel-rathole`); yaml digest refs flip to
  version refs with lock entries in the one adoption wave after Part 2;
  overrides adopted as needed.
- placeframe-capture-tool: `images:` for its workloads; `:latest` removal;
  `.env` flip.
- infra: switch the three `tree-` pins to version tags at leisure —
  post-Part-1 tree pins freeze and remain valid.

## Sequencing

Optimization function: simplest and least error-prone — minimize rework,
dead-code windows, and consumer migration waves. Pain-relief timing is
explicitly a non-goal.

```
1. P0 + P1  platform-resolved lock + tree-tag precision as ONE
      docker-devkit release → relock + regeneration cycle;
      capture-tool's relock unblocks the ZED-box AOA deploy
2. P2  semver lines + {sha}-only publishes (github-actions-toolkit
      + build-artifact-registry) → versions exist
3. P3 + P4a as ONE docker-devkit consumer-surface release
      (qualified grammar, version pinning, lock writer, probe, override)
      → consumers adopt in one wave (.env spellings + yaml versions + locks)
4. P4b dev overlay — trigger-gated, anytime after 3
```

The three derivations behind it:

1. No rework: an override shipped before Part 3 would be built, tested, and
   documented against digest-ref yaml — a spelling Part 3 deletes — then
   partially reworked. Everything ships once, against final shapes. Part 0
   rides the step-1 release for the same reason: a separate platform-lock
   release would force two lock-format migrations and two relock waves, and
   Part 1's hasher would build against a format that dies weeks later.
2. Entangled changes ship together, independent ones separately: qualified
   names → lock keys → override names → `BUNDLES=` parsing are one cluster in
   the selection machinery; Part 1 is the hash/build side and touches none of
   it; Part 2 is a different repo. Each release is independently testable,
   none carries machinery it cannot exercise (the lock writer ships only when
   versions exist to resolve).
3. One consumer migration wave: every consumer repo is touched once — `.env`
   spellings, yaml versions, lock entries, override adoption as one reviewable
   change — instead of once per feature.

The order and the release boundaries are ruled here;
`plan-artifact-identity-implementation.md` owns the phase-level work breakdown
that implementing sessions execute.

## Closed decisions

1. Tree-tag identity: precision attribution (the Dockerfile is the
   declaration), fail-closed construct guard, dirty-tree refusal on
   content-addressed build paths; `build --dev` exempt (dev tags are not
   content-addressed; the bind-mount workflow inherently runs dirty); bare
   `up` permitted.
2. Version space: per-unit semver lines with tag-everything; CalVer git tag
   stays the release-event grouping label. Flip-away conditions: none known —
   contract-coupling consumers would strengthen, not weaken, it.
3. Images count as shippable; the release gate is tree-tag change
   (post-Part-1), not digest change (rebuild churn) or presence (release
   spam); the dev gate tightens to match.
4. Origin axis, uniform: third-party upstreams ride the mirror; org-internal
   artifacts (bundles now, org images if ever declared) resolve direct,
   digest-pinned through the lock. This reverses the absorbed plan's standing
   default ("org-internal rides the mirror"), which never shipped; two
   resolutions for the same artifacts at two levels of one pull — bundle
   mirrored, its embedded images direct — is incoherence, and mirroring
   ourselves buys insurance against our own discipline at doubled storage.
5. Channel axis, uniform: every channel spelling (semver, dev, sha) is
   pinnable by consumers everywhere, matching packages; `latest` refused as a
   pin (the one mutable spelling — the standing pin-everything law, not new
   channel policing); exact tags only, no range specifiers. If channel
   restrictions ever come, they come everywhere at once (packages, images,
   bundles); flip condition: a first consumer policy that wants
   certified-only pins.
6. Consumer ref grammar: tag-only in yaml; digests live exclusively in the
   lock; a version-tagged ref without its lock entry refuses naming
   `build --lock-only` (the charts law).
7. Selection names: qualified `group-dash-option`, uniform, no short forms;
   the flat-vocabulary law repealed (qualified names structurally unique);
   at-most-one-per-group unchanged.
8. Lock keys: derived from the qualified selection name, upper-cased, plus
   `_BUNDLE`; collision-refused against `base_images` keys at load.
9. Override surface: `BUNDLE_OVERRIDE_<QUALIFIED_NAME>` in `.env`; value a
   path (relative to consumer root) or an `oci://` ref; unknown refuses
   listing the vocabulary, known-but-unselected is inert; overridden options
   skip the lock-entry probe and requirement; path-form files carry full
   local-output teeth; the producer's lock attaches beneath the consumer's
   own (path form only).
10. Stage 2 (cross-repo dev overlay) is staged, trigger-gated, design
    recorded in Part 4; the dirty-tree `--dev` carve-out rides Part 1
    regardless.
11. Bundle channel spellings: branch tags retire, `--branch` dies, publishes
    are `{sha}`-only, release-verb aliases carry all channel meaning;
    PR-channel image tags ruled out permanently (ghcr version-deletion
    hazard); `:latest` repointed to stable aliasing and exists for browsing,
    never as a pin.
12. The `up` fail-closed probe stays one law over every lock entry; bundle
    entries probe via `crane digest` (artifact media types).
13. Sequencing optimizes for least error-proneness (ruling out a
    pain-first ordering); three releases plus a trigger-gated stage 2;
    consumers adopt in one wave.
14. Lock records platform resolution (Part 0): `platforms` required
    wherever `base_images` is declared; per image the lock stores the index
    digest plus one concrete manifest digest per declared platform — exact
    os/arch selection, attestations skipped, ambiguity refused; machine arch
    is never consulted during resolution.
15. Lock format is structured YAML (images carry index plus a platforms
    map; charts and bundles stay flat); env consumption is synthesized
    per-run at the compose/bake edges, with a `lock-env` verb preserving the
    raw-compose valve; markers resolve the index under k8s and the platform
    dish under compose; selection centralizes in one devkit function.
16. Part 0 ships inside the cluster-P1 docker-devkit release — one
    lock-format migration and one consumer relock wave, with Parts 1-4
    building against the final lock shape.

Course adjustment (2026-10-22, absorbed): precision attribution replaced the
original coarse-bucket Part 1 because per-unit semver made precision
load-bearing (every over-inclusion is a spurious version bump) and the G5
parser — already required — makes it cheap.

## Dismissed and deferred

- Image-attached contract metadata / lock-as-DAG-resolver: ruled out as
  beyond state of practice and redundant with the compose layer, which
  already expresses the dependency graph. Semver tags are the substrate; the
  lock resolves tags to digests for org artifacts exactly as for third
  parties.
- App CalVer conversion: retired (consistent with images staying semver).
- Per-image CalVer aliases: available later as free additional tags if ever
  wanted; not the primary space.
- Range specifiers for bundle refs: rejected — exact tags plus Renovate is
  the docker/chart-native pattern; ranges drag resolver semantics into the
  devkit.
- Channel-restriction policy: deferred with its flip condition recorded in
  decision 5.
- Stage 2 build: trigger-gated (decision 10), not scheduled.

## Hygiene

- Make-it-Sing carries stale git tags from the release machinery's own
  debugging era: `pr-4-237`, `pr-4-238` (superseded same-PR builds, pre-dating
  the ordered sweep), `pr-6-250` (pre-dating working landed-PR resolution;
  `dev-250` is its landing). Delete the stale releases and tags once; check
  whether `pr-5-248` / `pr-7-254` belong to still-open PRs first.
- placeframe's `.dockerignore` allows `packages/generated/csharp/api-client/`
  though nothing copies it: harmless under precision, but it bloats every
  build context; remove at leisure.
