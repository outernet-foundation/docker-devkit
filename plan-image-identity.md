# Image identity and versioning

Pre-implementation plan, two linked initiatives: close the tree-tag input-closure
defect in this package (Part 1), then add release version tags to images in
github-actions-toolkit (Part 2), sequenced after Part 1. Captured from an
investigation session on 2026-10-15; code anchors are as of that session and may
drift. The open questions at the end must reach convergence before any of this
is implemented.

## Part 1 — `tree-` tags do not cover their input set (defect, lands first)

A lock-file bump does not change any service's tree tag. That is a bug, not a
design choice: the partition rule in `context_sha.py` is deliberate, but its
consequence — the base-image lock and the bake manifest are invisible to every
hash — was never weighed. This section is the full audit; the lock is one of
several gaps, not the only one.

### How the tag is computed today

`compute_service_shas` (`src/docker_devkit/context_sha.py:12`) builds, per
tagged service, a synthetic git index and writes a tree object; the pushed tag
`tree-{hash}` is that tree hash. Input flow:

```
git ls-tree -r HEAD                    committed tree only
  → .dockerignore allowlist filter     the root allowlist defines the input set
  → partition:
      outside workload root        → shared, in every service's hash
      under workloads/<svc>/       → that service only
      under workloads/, no owner   → dropped from every hash   ← the defect lives here
  → git update-index --index-info + git write-tree
  → env {DIR_NAME}_SHA → bake tag :${X_SHA} (and ENV-stamped inside some images)
```

The workload root is the single top-level directory containing every tagged
service's Dockerfile (`workloads/` in all three image repos). Everything directly
under it that belongs to no service dir — `workloads/images.yml`,
`workloads/images.lock`, plus harmless bystanders — is dropped from every hash.

### The gap audit

Every channel that influences image bits, and whether the hash sees it:

| # | Input channel | Seen |
|---|---|---|
| G1 | `workloads/images.lock` — base digests → env → `x-base-args` → `FROM` | no |
| G2 | `workloads/images.yml` — literal args (`TORCH_DEVICE`), args wiring, `additional_contexts` edges, tags | no |
| G3 | `additional_contexts: target:` dependencies — dependents' hashes exclude their bases' dirs (live in placeframe: reconstructor/localizer consume `neural-networks-base` builds) | no |
| G4 | `.dockerignore` itself — defines the input set, sits outside the allowlist, edits never re-key | no |
| G5 | Latent: a Dockerfile `COPY workloads/<other-service>/…` — allowlisted, unowned by the copying service, nothing forbids it | no |

Seen correctly today: own service dir (including its `pylock.toml` and
calibration artifacts) and shared allowlisted roots outside `workloads/`
(placeframe: `database/`, `packages/python/`, `packages/generated/*`).

Related observation: `reconstructor-cuda` and `reconstructor-rocm` share one
Dockerfile dir, hence one tree tag, across two image repositories; the variant
difference is a manifest arg (G2). After the fix this is still fine — different
repos, and manifest edits re-key — but worth remembering when reasoning about
tag uniqueness.

### Why it matters — concrete harms

- **Calibration corpus contamination (sharpest harm).** `LOCALIZER_SHA` is the
  localizer's `pipeline_version` (`workloads/localizer/Dockerfile` stamps it as
  ENV; the runtime loader hard-fails on mismatch; `fit-calibration` keys its
  corpus on it specifically to prevent pooling across different pipelines). A
  torch-base bump produces a numerically different localizer with the *same*
  `pipeline_version`: the corpus silently pools across genuinely different
  pipelines. Same for `RECONSTRUCTOR_SHA`, stamped into every
  `ReconstructionMetrics` row.
- **Movable pins.** Because a rebuild pushes the same tag over a new digest (ghcr
  allows tag overwrite), a base bump silently repoints every existing `tree-`
  pin. Today that bites infra (three hand pins: `infra/placeframe/
  reconstructor-probe.yaml:13`, `EnableLocalNode.py:7`, `program/reconstructor.py:9`)
  and placeframe's committed `stack/generated/**` artifacts, which embed `tree-`
  refs.
- **Identity promise broken in general.** The tag presents as content-addressed
  but the address omits real inputs (bases, args, dependency builds).

### The fix

Make the tag address the full input closure of the image. In
`compute_service_shas`:

1. **Repo-level build inputs become shared entries**: `workloads/images.yml`,
   `workloads/images.lock`, and the root `.dockerignore` join the shared set
   (same standing as `database/` and `packages/**` already have). Coarse — any
   lock bump re-keys every service — and correctly so: CI rebuilds everything on
   any bump, and a per-service arg-graph would be machinery for precision nobody
   consumes.
2. **Dependency closure**: each tagged service's entry set gains the dirs of its
   transitive `additional_contexts: target:` dependencies, tagged or tagless.
   The union-find walk already exists as `dependency_groups`
   (`src/docker_devkit/matrix.py:64`); lift it to a shared home (it is a
   bake-document fact) and consume it here.
3. **Fail-loudly companion for G5**: scan each Dockerfile's `COPY` lines and
   reject any source outside {own dir} ∪ {shared roots} ∪ {declared dependency
   dirs} — the same spirit as the allowlist's "adding a COPY not listed here
   fails the build loudly", closed for cross-service paths. The Dockerfile
   scanning pattern exists in `image_refs.stray_references`.

No tag-grammar change: `tree-` stays, the value is opaque and re-keys anyway.
New tests in `tests/test_context_sha.py`: lock edit re-keys all; manifest arg
edit re-keys all; dependency-dir edit re-keys dependents; `.dockerignore` edit
re-keys; out-of-closure COPY raises.

Stale comment to refresh while in there: `matrix.py`'s comment claims
`neural-networks-base-*` are tagless; they carry tags in placeframe's manifest
today.

### One-time re-key effects (benign, but must be choreographed)

- Every image gets a fresh `tree-` tag at its next build; **old tags freeze in
  place** (CI never re-pushes them) — the fix converts existing pins from
  movable to actually-immutable, so they get safer, not broken.
- placeframe's committed `stack/generated/**` embed old tags: one deliberate
  regeneration cycle after relocking docker-devkit (commit the devkit bump,
  regenerate, commit artifacts — the score staleness gate is disabled there, so
  this is operator discipline).
- infra's pins keep resolving to frozen old digests; re-point at leisure.
- Calibration corpus rows keep their old `pipeline_version` values; the
  mismatch hard-fail on redeploy is existing behavior for any re-key.
- Delivery: docker-devkit patch release (pre-1.0, breaking rides the patch
  flow) + relock in placeframe, placeframe-capture-tool, Make-it-Sing.

### Residual design decision: committed tree vs working tree

The hash is over the **committed** tree while local `build`/`up` build the
**working** tree: uncommitted edits produce images stamped with the committed
tree's identity, silently. Pre-fix this was academic; post-fix it is the
remaining hole. Options: refuse to build when allowlisted paths differ from HEAD
(one `git status --porcelain` check over the allowlisted set), or an explicit
decision that the tag means committed-tree identity and builds must be clean.
Open question 1.

## Part 2 — image version tags (github-actions-toolkit; after Part 1)

### Motivation: the pointer landscape has a hole

- `tree-` (post-fix): content-addressed, immutable, but unordered and unreadable.
- `:latest`: moved by **every PR build** (build-docker runs on PRs via
  integrate), so it points at unmerged, uncertified heads — worse than useless.
- No ordered, certified image pointer exists at all. `dev-{count}` on images
  would be the only thing meaning "latest merged-to-dev image set"; a CalVer tag
  the only thing meaning "the image set that shipped in this release".

Current cross-repo consumers, all hand-maintained: Make-it-Sing pins the compose
bundle by raw digest (`oci://…placeframe/placeframe-cuda@sha256:…`,
`Make-it-Sing/compose.yml:6`); infra pins `tree-` tags by hand in three files;
placeframe's own generated stack embeds `tree-` refs from the committed tree.

### Proposed design (not converged)

- **Release-time aliasing in the toolkit release verb**, not build-time. The
  verb already pulls the digest manifest from the shelf at the certified SHA on
  every channel (`verbs/release.py:212`); alias each entry's `ref@digest` onto
  the release's tag. Build-time tagging would require threading version/count
  facts into the docker matrix legs (they carry none today — `matrix` stamps
  only project-owned legs); release-time needs zero build-path changes.
- **Version space: CalVer, repo-level** (`{ref}:{YYYY.MM.N}`), riding the
  release the images ship in — versus per-image semver (a new `images:` section
  in `publish.yml`, per-image tag namespaces, plan computation: the full package
  machinery duplicated). CalVer is honest and cheap; the only argument that
  flips it is per-image semver coupling to package versions (open question 3).
- **Tag-everything semantics.** Every release aliases the full image set at the
  certified head, including images whose bits did not change since the previous
  release. The CalVer tag is a *release-event alias* ("the stack that shipped in
  2026.10.4"), not a content identity (that is `tree-`'s job). Complete release
  sets beat partial ones: partial tagging forces every consumer to know "which
  tag is the newest one that exists for this image". Same philosophy as the
  per-run release bodies ("snapshots, not changelogs").
- **Channels: stable and dev.** Dev cuts `dev-{count}` image tags with the same
  mechanism when cutting the `dev-{count}` prerelease — symmetric with package
  dev versions, fills the ordered-certified-pointer gap. PR channel deferred
  (open question 7): weakest value (PR notes already link the exact `tree-`
  tags), guaranteed garbage (closed PRs; every push increments the count), and
  it would push cleanup duties into merge-gate and the closed-PR sweep, which
  today touch only git releases. Purely additive later.

### ghcr semantics the design must respect

ghcr's model is version-level (a version = a digest), tags are attributes:

- **Tags on one digest coexist; deletion kills all of them.** A stable release's
  certified head is a digest that already carries `dev-{count}` (it landed on
  dev first). CalVer aliasing lands on the same version — dev image tags can
  never be swept the way `release --channel stable` sweeps `dev-*` git releases.
- **Therefore no tag GC, and retention policies are a hazard.** A "keep last N
  versions" package setting would eventually delete digests a CalVer tag points
  at, silently rotting released versions. Safe posture: no retention policies on
  image repos that carry release tags; accept accumulation; any future sweeper
  must be version-level and CalVer-protecting.

### Mechanism

New aliasing primitive in build-artifact-registry (org law: the toolkit owns no
registry CLI code), reusing the neutral credential chain. `oras tag` is proven
on cache artifacts (`cache.py:101` `promote_scope`) but not on docker image
manifest lists — verify live first (open question 6); `docker buildx imagetools
create` is the fallback but drags buildx into the release job. Stable delivery
jobs already carry `packages: write`. Push-once discipline holds: CalVer
count-and-cut and dev counts are serialized under the release mutex; the shelf
is SHA-keyed so re-runs alias identical digests (metadata no-op).

### Change detection (optional; only feeds the nothing-to-ship gate)

Stable's nothing-to-ship early return (`release.py:104`) ignores images today:
a repo with images but no changed packages/apps cuts no release — so an
images-only repo can never cut one, and whether images should count as shippable
is open question 4.

- Cheap detector (toolkit-only, one helper): previous CalVer git tag → its
  merge commit's parent2 (the previous certified head) → pull that SHA's digest
  manifest from the shelf → diff against the current one. All primitives exist
  (`pull_digest_manifest` takes an arbitrary SHA). Key choice: **digest**
  (catches everything including base bumps, over-reports on rebuild churn —
  timestamps likely re-digest unchanged trees; safe direction) versus **tree
  tag** (precise on source content, but inherits Part 1's blind spots until the
  fix lands; after Part 1 it becomes the sane key). If this is ever wanted for
  partial tagging or per-image version lines rather than just the gate, it
  requires the post-fix tree key — another reason Part 1 lands first.

### Consumer adoption

- **`x-base-images` + `workloads/images.lock`**: works with zero docker-devkit
  code today — `update_lock_entries` resolves any tagged ref via
  `imagetools inspect`, so `placeframe/api:2026.10.1` locks to a digest exactly
  like a third-party tag; `unpinned_references` passes a version tag. This is
  the "lock resolves the readable tag, digest gives immutability" story.
- **Mirror routing for org-internal refs** (open question 5): as-is, a declared
  org-built ref rides the mirror (`mirrored_ref` prefixes it; the `mirror` verb
  crane-copies it; `up`'s `enforce_mirrored` probes it). Uniform, zero code —
  but the mirror's stated rationale (upstream GC/yank) does not apply to
  org-owned ghcr. Alternative: an org-namespace pass-through exemption, touching
  `mirrored_ref`, the lock writer, the `mirror` verb, and `enforce_mirrored` —
  contained. Session recommendation was the exemption.
- **infra**: switch the three `tree-` pins to version tags. Post-Part-1 tree
  pins freeze, but version tags never move at all; strictly better.
- **Make-it-Sing's compose bundle is NOT covered by any of this.** The bundle
  (`placeframe-cuda`) is published by placeframe-local `publish-compose` on
  integrate pushes with `{sha}` + branch tags only (`scripts/src/scripts/
  publish_compose.py:104-110`); `include: oci://…` is not an `x-base-images`
  declaration, is not scanned by `stray_references`, has no lock step, and never
  sees a CalVer. Versioning the bundle is a separate, placeframe-scoped follow-up
  (open question 8).
- **Version coupling**: `version_coupling_violations` composes with image
  versions only if they share a version space with packages — with CalVer they
  do not. If any consumer wants "image tag must match `placeframe-core` version"
  coupling, that is the argument that flips open question 3 to per-image semver.

## Sequencing

1. Part 1 in docker-devkit → patch release → consumer relocks → placeframe's
   one-time `stack/generated/**` regeneration cycle.
2. Part 2 in github-actions-toolkit (+ build-artifact-registry aliasing
   primitive).
3. Adoption per consumer: infra pins, `x-base-images` declarations, mirror
   routing policy.

## Open questions (close before implementation)

1. Committed-tree vs working-tree identity: dirty-tree guard, or documented
   committed-tree identity with clean-build requirement?
2. COPY-scan guard (G5) in the same change, or a follow-up?
3. Version space for image tags: CalVer repo-level vs per-image semver (flips
   only if package-version coupling is wanted).
4. Nothing-to-ship: do images count as shippable (images-only repos cut a
   release on every main push), and is that cadence accepted? If not, which
   gate — presence, digest-change, or post-Part-1 tree-change?
5. Mirror routing for org-internal refs: ride the mirror (zero code) or
   org-namespace pass-through exemption (recommended in session)?
6. Verify `oras tag` works against ghcr image manifest lists; fallback is
   `docker buildx imagetools create` in the release job.
7. PR-channel image tags: deferred (session position) or ruled out?
8. Compose-bundle versioning (Make-it-Sing's include): scope a placeframe-local
   follow-up or drop.
