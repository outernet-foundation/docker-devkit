# Mirror digest retention incident

## Incident

On 2026-10-29, during the compose-consumer-model delivery, placeframe's committed
`docker-devkit.lock` entry for `KEYCLOAK_IMAGE` named a digest the org mirror no longer
held, with no path to regain it:

- Old lock entry: `ghcr.io/outernet-foundation/mirror/quay.io/keycloak/keycloak:26.8.1@sha256:79c623a2…`
- Digest-only probe of the mirror: `79c623a2` **absent** (registry 404 / manifest-unknown) —
  genuinely deleted from ghcr, not a tag mismatch.
- A local docker image carried RepoDigests for both
  `mirror/…keycloak@sha256:79c623a2…` and `quay.io/keycloak@sha256:79c623a2…` — proof the
  digest was pullable from both registries at some earlier time.
- Upstream `quay.io/keycloak/keycloak:26.8.1` (a manifest **list**) currently resolves to
  `sha256:a78d6b79…`; the mirror's tag points at the same list; `a78d6b79` is present.
  (The `3e1ac6…` value seen via `docker manifest inspect` is the amd64 **child** manifest,
  not a tag digest — lock digests are list digests.)

Every `up` on the frozen lock failed closed at the mirror probe ("not mirrored"), and the
documented remedy — let CI's mirror job run — cannot fix it: the mirror job copies the
upstream **tag**, which now resolves to `a78d6b79`, never restoring `79c623a2`. The only
recovery was `build --lock-only --upgrade` (re-resolve everything against upstream),
landed as placeframe commit `e896998c`.

## Mechanism

1. Upstream re-pushed the **same tag** (`26.8.1`) with rebuilt content: new list digest
   `a78d6b79` replaced `79c623a2` at the tag. A tag that semantically should be immutable
   wasn't.
2. Some re-resolution in the org (a lock-freshness `--upgrade` or a new declaration in
   *any* repo — `mirror/<upstream-host>/<path>` is a **shared org-wide namespace**, so any
   repo's CI mirror job writes the same mirror tag) demanded the current upstream digest.
   Its presence probe found `a78d6b79` missing, so the job ran
   `crane copy quay.io/keycloak/keycloak:26.8.1 mirror/…/keycloak:26.8.1` — which **moves
   the mirror tag**, orphaning `79c623a2` as an untagged manifest in ghcr.
3. ghcr garbage-collects untagged manifests. `79c623a2` was deleted from the mirror.
4. Placeframe's lock still froze `79c623a2` — by design: declaration-incremental relocks
   copy frozen digests forward precisely so same-tag upstream re-pushes don't churn green
   branches. Nothing ever re-copies the frozen digest, and nothing re-pins it.

The three preconditions: a mutable upstream tag, a re-resolution anywhere in the org
after the tag moved, and ghcr's untagged-manifest GC. All three are routine.

## Why the design didn't prevent it

The mirror's durability promise — once the mirror holds a digest, upstream retention is
irrelevant — defends against **upstream** GC/yank, and that defense held. The mirror was
never made durable against **its own rewrites**:

- The `mirror` verb only ever writes **tag** refs (`crane copy {ref} {prefix}/{ref}`).
  No digest-named ref is ever created, so every re-copy of a moved tag orphans the
  previous manifest in a registry that GCs untagged manifests — the exact failure mode
  the mirror exists to mitigate, operating on the mirror's own namespace.
- The stranding is org-global: the first consumer to re-resolve after an upstream
  same-tag re-push moves the shared mirror tag and strands every other consumer's frozen
  digests. Lock freezing guarantees nobody re-copies the old content.

## Recurrence

Unmitigated. The `--upgrade` relock (`a78d6b79`) has the same exposure: if upstream
re-pushes `26.8.1` again and any repo freshness-relocks before a dependent's `up` runs,
the digest strands identically and that dependent's next `up` fails closed with an
unfixable-by-mirror error. The window is real — placeframe's branch CI has not run since
the relock.

## Fix sketch (not decided)

The structural fix is in the `mirror` verb: pin every mirrored digest under an immutable,
digest-derived ref **in addition to** the tag, so a tag move can never orphan content.
Sketch: after each `crane copy`, also write a digest-alias ref — e.g.
`crane copy {prefix}/{ref}@sha256:… {prefix}/{ref}:sha256-<digest16>` (or an oras-style
by-digest push) — and probe presence by digest, which already works because the alias
keeps the manifest tagged and therefore GC-safe. Old digests stay pullable forever; a
moved upstream tag costs one more alias per re-push.

Two secondary candidates, likely complements rather than alternatives:

- **Loud same-tag movement at relock**: when a declaration's tag resolves to a digest
  different from the frozen one *without* the declaration changing, print the movement —
  today it is silent by design, which is why the stranding surfaced as a runtime probe
  failure instead of at the lock layer.
- **Freeze acknowledgement at `up`**: the not-mirrored error could name the recovery
  path explicitly (`build --lock-only --upgrade`) since "let CI's mirror job run" is
  provably insufficient for this failure class.
