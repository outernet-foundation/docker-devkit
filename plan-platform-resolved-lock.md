# Platform-resolved lock

## The defect

`build`'s lock writer resolves a declared tag to its top-level digest via
`docker buildx imagetools inspect` — for a multi-platform tag that is the
**index** digest (the manifest list), not a per-platform manifest. A consumer
that pulls the pinned ref without a platform expression gets whichever
platform the *resolving machine* runs: capture-tool's save/load ship path
resolves on the amd64 build host and delivers amd64 loki/alloy to the arm64
ZED box (`exec format error` on every boot since first deploy). Pull-on-target
consumers are unaffected — the receiving daemon resolves the index for its own
arch — so the defect stays invisible until a middleman ships across arches.

## The law

Same inputs (config + lock) produce the same bytes on any machine, any arch,
any day. A machine's own architecture participates only in the final act of
executing bytes. The defect is the one place resolution consults the resolving
machine implicitly; this design removes the last implicit arch decision.

## Design

1. **`platforms` is required whenever `base_images` is declared** — refuse at
   `load_config`. It stops being an optional build hint and becomes the
   explicit deploy-target declaration. Charts are exempt (arch-neutral OCI
   artifacts).

2. **The lock becomes structured YAML** — per image: the index digest plus one
   concrete manifest digest per declared platform; charts stay flat refs:

   ```yaml
   images:
     LOKI_IMAGE:
       index: ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@sha256:…
       platforms:
         linux/arm64: ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@sha256:…
   charts:
     KEYCLOAK_CHART: oci://ghcr.io/…@sha256:…
   ```

   Rationale: the platform axis cannot live in env-var grammar. Flat compound
   keys (`LOKI_IMAGE_LINUX_ARM64`) encode schema as string discipline, scatter
   selection into every consumer, and cannot express index-vs-dish
   distinctions. The env-line format was adequate at one axis; its two
   documented virtues — native `--env-file` consumption at the `up` handoff
   and the raw-`docker compose` valve — reduce to one attach line and a debug
   habit, both preserved by synthesis (below). The committed lock becomes the
   complete truth: every exact byte-stream the system can run, and for which
   machines, readable without touching a registry.

3. **The resolver records the complete resolution** against the upstream
   reference: index digest + one concrete manifest digest per declared
   platform. Selection rules: exact `os`/`architecture` match; `unknown/unknown`
   and attestation-annotated entries skipped; multiple matches refuse loudly
   naming the entry; single-manifest refs and `@digest` declarations record
   the manifest uniformly as index and sole platform entry. Resolution is a
   pure function of config — the writing machine's arch is never consulted.

4. **Selection centralizes in one function** (an `entry_for(lock, name,
   platform)` shape): the compose/bake edges, `install-zed`, and the markers
   all call it; no consumer constructs keys or picks digests itself.

5. **Env synthesis at the consumption edges**: `up`/`down` materialize a
   per-run env file (the selected platform entries) attached as the second
   `--env-file` — same temp lifecycle as the materialized root compose file,
   printed and kept on failure like the bake file. Bake receives the same
   synthesis in-process. A `lock-env` verb emits the synthesized env on
   stdout for the raw-`docker compose` pressure valve:
   `docker compose --env-file <(uv run lock-env) …`.

6. **Platform selection at the up edge is declaration-driven**: a config
   `platforms` singleton selects itself; a multi-platform repo's `.env` names
   `PLATFORM=` (committed per deploy target). Never host introspection.

7. **Markers resolve by substrate**: `lock:`/`lock-image:` under k8s resolve
   the **index** (kubelet resolves per node — architecturally correct);
   compose-side resolution selects the platform dish. The structure makes
   both explicit; the flat format could not express the distinction.

8. **The mirror verb probes every recorded digest** (index + dishes) —
   presence by exact bytes. The copy itself is unchanged: whole index, dishes
   ride along; `crane copy` byte-identity keeps upstream and mirror digests
   equal.

9. **The writer is deterministic**: sorted images/charts and platform keys,
   byte-stable across machines — the `--lock-only --check` gate stays
   hermetic on any-arch runner.

## Migration

No dual-read: the typed loader swaps wholesale, and the devkit version bump
turns every consumer's `--check` gate red until each repo relocks once — the
YAML rewrite re-resolves every entry, so the declaration-freeze/`--upgrade`
migration question dissolves in the cutover. capture-tool relocks to arm64
dishes and deploys; placeframe adds `platforms: [linux/amd64]` at its next
relock (one line, forced loudly by the config refusal).

## Verification owed

- ghcr serves child manifests by digest — a pull and a `crane digest` against
  the mirror by a recorded dish digest. Children's existence inside the
  copied index is verified; per-digest retrieval is not.
- Spot-check a relocked entry: `buildx imagetools inspect --raw <digest>`
  answers a manifest, not an index.

## Release mechanics

Pre-1.0 patch flow. Bump and release the devkit, then capture-tool repins
(`pyproject.toml` exact pin and `docker-devkit.yaml` `requires` floor, same
run) and relocks from the sandbox (registry queries only). Deploy stays
host-side per the capture-tool AOA handoff.
