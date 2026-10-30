# Image identity and versioning

Pre-implementation plan, two linked initiatives: give `tree-` tags a precise,
fail-closed input closure in this package (Part 1), then add per-service semver
version lines to images and compose bundles in github-actions-toolkit (Part 2),
sequenced after Part 1. Captured from investigation sessions 2026-10-15 and
2026-10-22; all open questions are closed and the decisions are recorded at the
end. Code anchors are as of the 2026-10-22 session and may drift.

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
                  (content identity,      (post-Part-2)           (post-Part-2)
                  all channels)
shelf (ORAS)      sha-{full}[-{key8}]     read back for stapling  read back
packages          —                       {base}.dev{count} /     {base}
                                          -dev.{count}
compose bundle    {sha} only              {base}-dev.{count}      semver
                  (post-Part-2; branch tags retired)
```

Standing principles: one ordinal (commit count at the certified head) keys
everything per-event, so `pr-{N}-{count}` and the landing's `dev-{count}` share
the number because they are the same bits at two lifecycle stages. The PR
channel publishes nothing permanent anywhere — only content-keyed ephemera
(`tree-`, `sha-`). Registry-side channel markers exist only where the registry
forces one (npm dist-tag; image and bundle dev spellings). Post-Part-2 every
consumable artifact type carries a per-unit semver line; the CalVer git tag
stays the release-event grouping label.

## Part 1 — docker-devkit: precise input closure for `tree-` tags

### The defect being fixed

`compute_service_shas` (`documents.py:62`) hashes per-service synthetic git
trees over the `.dockerignore`-allowlisted committed tree. The partition rule
drops `workloads/images.yml`, `workloads/images.lock`, and `.dockerignore`
from every hash, excludes `additional_contexts` dependency dirs from
dependents' hashes, and nothing forbids a cross-service `COPY`. Concrete
harms: a base-image bump produces a numerically different localizer with the
same `pipeline_version` (silent calibration-corpus contamination); movable
`tree-` pins; a tag that presents as content-addressed but omits real inputs.

### The design: attribution, not buckets

The original fix sketch (coarse shared buckets) is superseded. The hash input
set is computed per service by attributing the Dockerfile's actual
consumption — the Dockerfile is the declaration; no input declarations in
`docker-devkit.yaml` or the manifest (a config-declared input list is a second
source of truth that drifts, and verifying it against the Dockerfile requires
the parser anyway).

Per-service hash inputs:

1. The service's Dockerfile itself. Any recipe change re-keys, which keeps the
   closure self-consistent: a new `COPY` line moves the hash at the same
   moment the newly copied files enter the resolved set.
2. The resolved `COPY`/`ADD` source set — every context path or glob the
   Dockerfile actually copies, resolved against the allowlisted tree. Shared
   roots enter a service's hash only when copied (`packages/python/common/**`
   for the api; nothing for the gateway).
3. The ARG-consumed `workloads/images.lock` lines — intersect the Dockerfile's
   `ARG` declarations with lock names; hash only those lines. Conservative in
   the safe direction: declared-but-unused args still hash their lines.
   Lock lines consumed only at runtime by compose (never declared as build
   args) drop out of build hashes entirely.
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
gap. The context-reading constructs to enumerate: `COPY` from context,
`ADD` with a local path (URL-form `ADD` is network, out of scope like apt),
`RUN --mount=type=bind,source=…` (reject), path-form `additional_contexts`
(reject; only `target:` form is attributed). Unknown syntax, heredoc `COPY`,
and unresolvable globs fail loudly. The same scan rejects cross-service
`COPY` sources outside {own dir} ∪ {copied shared roots} ∪ {declared
dependency outputs} — the G5 guard, absorbed into the attribution pass.

### Dirty-tree guard

The hash is over the committed tree while builds compile the working tree.
Post-fix this is the only remaining way the tag can lie, so build paths
refuse a dirty tree: `build` and `up --build` run `git status --porcelain`
filtered to the allowlisted set (modified and untracked allowlisted files
both count) and fail loudly listing the offending paths. `up` without
`--build` stays permitted — it pulls images by committed-tree tag and is
self-consistent. `generate-score` and `publish-compose` run in CI on clean
checkouts; the guard there is a no-op.

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
declared args (hashed conservatively), `FROM` bases (lock lines, digest-pinned
so upstream drift cannot change bits without a lock edit, which re-keys), and
`additional_contexts` outputs (embedded dep hashes). The guard converts any
unattributable construct into a loud failure. Environment channels are
deliberately uncovered: buildx/docker engine version, runner quirks, network
fetches during build (apt mirror drift). Consequence: a same-tree rebuild can
yield a different digest, and ghcr tags are mutable, so a rebuild can
overwrite a `tree-` tag onto fresh bytes — accepted residual; the mitigation
that matters is Part 2's release-time semver alias, which points at the
certified digest and never moves.

Variant subtlety: `reconstructor-cuda` and `reconstructor-rocm` share one
Dockerfile dir and therefore one tree tag — the variant difference is a
manifest arg, covered by the manifest-mapping input, so both variants hash
identically. Sound because tree tags are per-repository unique (the full ref
disambiguates), a variant-arg edit re-keys both, and Part 2 gives variants one
shared version line, so advancement stays variant-synced.

### One-time re-key choreography

- Every image gets a fresh `tree-` tag at its next build; old tags freeze in
  place (CI never re-pushes them) — existing pins convert from movable to
  actually-immutable.
- placeframe's committed `stack/generated/**` embed old tags: one deliberate
  regeneration cycle after relocking docker-devkit (commit the devkit bump,
  regenerate, commit artifacts; the score staleness gate is disabled there, so
  this is operator discipline).
- infra's three `tree-` pins keep resolving to frozen old digests; re-point at
  leisure (or adopt Part 2 version tags).
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
paths; dirty tree on bare `up` → permitted.

## Part 2 — github-actions-toolkit: per-service semver image and bundle lines

### Version space: per-service semver

Repo-level CalVer was rejected in favor of per-image semver lines: the
consumable-image ecosystem is semver-dominant (this org's own `x-base-images`
consumes grafana, caddy, keycloak — all semver); automated-consumption
machinery (Renovate's docker manager, Flux `semver` image policies,
dependabot) is semver-native; and the expected repo splits make org images
cross-repo consumables that the lock resolves by tag exactly like third-party
images. Semver's compatibility contract maps to image surfaces (env holes,
ports, service names, volume layout): major = breaking contract, minor =
additive, patch = fix, human-asserted via `major_minor` — the same trust model
packages use.

Tag-everything semantics: every release aliases the full image set — changed
images at their new version, unchanged at their existing one (a metadata
no-op) — so complete sets exist at every release and "newest tag for image X"
is always its highest semver. The release event keeps its CalVer git tag as
the grouping label; apps stay on their existing per-app semver (a CalVer
conversion for apps was considered and retired).

### Declaration surface: `images:` in publish.yml

One entry per version line, keyed by the unit name: the bake service name
with the `-cuda`/`-rocm` variant suffix removed. `major_minor` per unit,
required, same meaning as packages. All variant repositories of a unit receive
the same version tag (`reconstructor-cuda:0.1.3` and `reconstructor-rocm:0.1.3`
share the `reconstructor` line and the `reconstructor-v0.1.3` git tag).
placeframe has 15 services / 13 units.

### Change detection via the shelf — no docker-devkit dependency

The release verb does not import docker-devkit. Per unit: resolve the highest
`{unit}-v*` git tag → its commit → pull that SHA's digest manifest from the
shelf → compare tree tags (recorded in each entry's `tags[]` by the build
push) against the certified head's. Differing tree tag in any variant ⇒ ship
and bump patch (or the declared `major_minor` sits above the line, forcing the
bump — the packages' forced-publish rule). No tag yet ⇒ first release ships.
All primitives exist: `pull_digest_manifest` takes an arbitrary SHA and is a
build-artifact-registry dependency the toolkit already has. The dev channel
compares against the highest stable tag's commit, exactly like package
path-diff.

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
the one cell violating the PR-column principle. Consumers remove `latest`
from `workloads/images.yml` build tags; the stable aliasing includes it
alongside the version. `latest` becomes honestly "newest released version of
this image." Dev never aliases it.

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

The bundle is the stack-as-a-unit and the artifact consumers actually include
(`Make-it-Sing/compose.yml:6`). It gets its own semver line — the Helm chart
analog (a chart is a bundle of pinned images with contracts; Helm mandates
strict semver) — versioned by the toolkit, not placeframe-locally.

- Declaration: `bundles:` section — name, ref(s), `major_minor`, and
  `sources` (the compose source paths; unlike the rejected Dockerfile input
  declarations, compose sources are stated nowhere else, so declaring them is
  new information, not duplicated state). Variants (`placeframe-cuda`,
  `placeframe-rocm`) share one line.
- Change detection: any image unit advanced OR path-diff over `sources`
  (which includes `workloads/images.lock` — a runtime third-party bump like a
  loki upgrade changes bundle bits without any local tree moving).
- Mechanism: release-time aliasing with the same verified primitive. Every
  certified head's bundle is already published under an immutable `{sha}` tag
  by its PR run (`publish_compose.py` publishes on PR heads and landings), so
  dev and stable alias `placeframe-{variant}:{sha}` onto `{base}-dev.{count}`
  / `{version}` with no new publish job and no ordering problem. Stable pushes
  the `{bundle}-v{version}` git tag.
- Branch tags retired: PR runs currently stamp the PR branch name and
  landings stamp `dev`/`main` — movable pointers accumulating per PR,
  the same disease as PR-moved `latest`. `publish-compose` publishes `{sha}`
  only; release-verb aliases carry all channel meaning.
- Make-it-Sing: no change required — the digest include remains the correct
  discipline. What improves is finding the digest: the release body gains a
  Bundle row (version linked to the ghcr version page).

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

## Consumer adoption

- placeframe: add `images:` (13 units) and `bundles:` (`placeframe`, variants
  cuda/rocm, compose sources) to `publish.yml`; drop `latest` from
  `workloads/images.yml` tags; `publish-compose` drops branch tags; one-time
  `stack/generated/**` regeneration after the docker-devkit relock.
- Make-it-Sing: same `images:` for `livekit-token`; `latest` removal; one-time
  stale-tag cleanup (below).
- placeframe-capture-tool: `images:` for its workloads; `latest` removal.
- infra: switch the three `tree-` pins (`reconstructor-probe.yaml:13`,
  `EnableLocalNode.py:7`, `program/reconstructor.py:9`) to version tags at
  leisure — post-Part-1 tree pins freeze and remain valid.

## Sequencing

1. Part 1 in docker-devkit (precision closure + fail-closed guard + dirty-tree
   guard) → patch release → relock the three consumers → placeframe's
   regeneration cycle.
2. Part 2 in github-actions-toolkit (+ the build-artifact-registry aliasing
   primitive). Requires Part 1 (tree tags are the change key).
3. Adoption per consumer.

## Closed decisions

1. Dirty-tree identity: guard on build paths (`build`, `up --build`); bare
   `up` permitted; committed-tree identity otherwise.
2. COPY-scan guard (G5): batched into the Part 1 release, enforced at hash
   time inside the attribution pass.
3. Version space: per-service semver lines with tag-everything (flipped from
   CalVer after research; rationale above). Flip-away conditions recorded:
   none known — contract-coupling consumers would strengthen, not weaken, it.
4. Images count as shippable; gate is tree-tag change (post-Part-1), not
   digest change (rebuild churn) or presence (release spam); dev gate
   tightened to match.
5. Mirror routing for org-internal refs: closed by standing default — they
   ride the mirror like every declaration; no exemption planned. Re-opens
   only when a first org-internal `x-base-images` declaration makes the
   redundancy real. Deferral is safe: nothing depends on it, the default is
   benign (byte-identical copies), and adopting an exemption later carries no
   migration cost.
6. `oras tag` on manifest lists: verified working (registry:2 live test,
   pinned oras 1.2.2); primitive lives in build-artifact-registry.
7. PR-channel image tags: ruled out permanently (ghcr version-deletion
   hazard; zero value); `latest` repointed to stable aliasing.
8. Compose bundles: own semver line, toolkit-generic via `bundles:`,
   release-time aliasing, variants share the line, branch tags retired.

Course adjustment (2026-10-22, supersedes the original coarse-bucket Part 1):
precision attribution replaces bucket partitioning, because per-service semver
made precision load-bearing (every over-inclusion is a spurious version bump)
and the G5 parser — already required — makes it cheap. Config-declared input
lists were considered and rejected: the Dockerfile is the declaration.

## Dismissed and deferred

- Image-attached contract metadata / lock-as-DAG-resolver: ruled out as
  beyond state of practice and redundant with the compose layer, which already
  expresses the dependency graph. Semver tags are the substrate; the lock
  resolves tags to digests for org images exactly as for third parties.
- App CalVer conversion: retired (consistent with images staying semver).
- docker-devkit lock support for `include: oci://` refs (declare bundle by
  version tag, lock resolves digest): future, not now — same org-internal
  consumption future as decision 5.
- Per-image CalVer aliases: available later as free additional tags if ever
  wanted; not the primary space.

## Hygiene

- Make-it-Sing carries stale git tags from the release machinery's own
  debugging era: `pr-4-237`, `pr-4-238` (superseded same-PR builds, pre-dating
  the ordered sweep), `pr-6-250` (pre-dating working landed-PR resolution;
  `dev-250` is its landing). Delete the stale releases and tags once;
  check whether `pr-5-248` / `pr-7-254` belong to still-open PRs first.
- placeframe's `.dockerignore` allows `packages/generated/csharp/api-client/`
  though nothing copies it: harmless under precision, but it bloats every
  build context; remove at leisure.
