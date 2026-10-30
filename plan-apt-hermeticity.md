# Unpinned apt as the unpoliced build input

## Deficiency

On 2026-10-30, during placeframe-capture-tool re-orientation, the aoa-bridge build
was noticed running `apt-get update` + unpinned `apt-get install` against the live
distro archive. Surveying the estate shows this is not an aoa-bridge one-off — it is
the house pattern: **every Dockerfile in both consumer repos that installs OS packages
does so unpinned, against a mutable archive, at build time**. apt packages are the one
input class in the whole build pipeline that no pin, lock, or police layer covers.

Sites (all verified in-tree):

| Repo / image | Packages (unpinned) | Stage | Blast radius |
|---|---|---|---|
| capture-tool `aoa-bridge` (Dockerfile:18) | autoconf, automake, build-essential, ca-certificates, git, libtool, m4, pkg-config | `libusb-builder` stage only | compiled libusb .so differs across build dates |
| capture-tool `zed-capture` (Dockerfile:40) | ca-certificates | final runtime image | whatever the archive served that day ships in the box |
| placeframe `api` (Dockerfile:26) | git, ca-certificates | single base stage = runtime image | same |
| placeframe `database-manager` (Dockerfile:26) | git, ca-certificates | runtime image | same |
| placeframe `lease-server` (Dockerfile:26) | git, ca-certificates | runtime image | same |
| placeframe `neural-networks-base` (Dockerfile:18) | git, ca-certificates, libgl1, libglib2.0-0, libsm6, libxext6, libxrender1 | runtime image | inherited transitively by `localizer` and `reconstructor` (`FROM neural-networks-base`) |

`aoa-gateway` (stock caddy) and placeframe's remaining Dockerfiles (auth-initializer,
database-migrator, postgres, s3-initializer, seaweedfs) install no OS packages.

Note: placeframe's `git` is load-bearing, not vestigial — the api Dockerfile's own
comment says "Git is required to resolve VCS dependencies in the lock file (e.g.
logconf)", so it cannot simply be pruned; it needs VCS deps out of the lock or a
build-stage-only git. capture-tool's `ca-certificates` is plausibly prunable (requests
vendors certifi, uv bundles its own roots) — unverified whether anything in the zed
runtime path reads the system CA bundle.

## What is already pinned, and where apt falls outside it

The rest of the pipeline is closed:

- base images: per-arch arm64 **manifest digests** in `docker-devkit.lock`, resolved by
  `build --lock-only` (never index digests, never attestations)
- hand-written image references: policed by `image_refs.unpinned_references` — consumer
  CIs fail on an unpinned ref
- first-party image identity: tree-SHA tags from `compute_service_shas`
- Python deps: `pylock.toml` per workload, installed by uv
- libusb source: `LIBUSB_COMMIT` git SHA (content-addressed — the clone-then-checkout
  shape is fine content-wise)
- k8s charts: digest-pinned through the lock's `{NAME}_CHART` entries
- score-compose / score-k8s / helm: version-pinned constants in `config.py`
- devkits/toolkit: `uv.lock` + full-SHA `uses:` refs

Nothing in docker-devkit parses `RUN apt-get …` lines. `image_refs.py` scans FROM,
`COPY --from=`, and `image:` — apt is invisible to every existing gate. The lock has no
vocabulary for "which archive state the apt layer resolved against".

## Where it bites vs. where it's masked

Masked day-to-day: ghcr tags are push-once-immutable, so published `{tree-sha}` tags
hide the non-reproducibility.

Bites for real:

- **The tree-SHA identity claim is false.** `compute_service_shas` keys image identity
  on the source tree SHA; the whole release/install system's claim is "tree SHA →
  image". Unpinned apt means the same tree SHA baked on two days is two different
  images. Rebuilding an old commit (bisect, revert-deploy) does not reproduce the
  original bits, and "what image produced this capture data" is unanswerable.
- **`install-zed --build` local bakes are non-reproducible** — the iteration path for
  box logic, i.e. the path most likely to be asked to reproduce something later.
- **Archive transitions break old commits' builds** — a suite going EOL or a package
  leaving the archive makes a commit that built fine last week unbuildable, with no
  lock to point at.
- placeframe PR rebuilds of the same head differ silently.

## Adjacent and worse: unpinned downloads in Dockerfiles

`zed-capture/Dockerfile:34-38` runs Stereolabs' `get_python_api.py`, which downloads
**whatever pyzed wheel the vendor CDN is serving today** into the runtime image. The
SDK version is implied by the digest-pinned base image, but the wheel bytes are
unpinned. This is a larger hole than any apt line and belongs in the same pass.

## Fix sketch (not decided)

1. **Prune before pinning.** Audit each apt line for load-bearing-ness (see notes
   above); every removed package shrinks the problem. The runtime half may reduce to
   just placeframe's git-for-VCS-locks and neural-networks-base's graphics libs.
2. **Snapshot-pin the remainder through the lock.** Rewrite `sources.list` at the top
   of each apt-using Dockerfile to a dated snapshot, with the snapshot date as a
   **no-default ARG** — a missing key is a hard bake error, exactly the base-image ARG
   law. `build --lock-only` records the dates as lock entries; a declaration surface in
   `docker-devkit.yaml` (archive → suite) keeps it explicit rather than derived from
   base images. Snapshot services: `snapshot.debian.org` for the Debian bases
   (`python:3.13-slim`, the trixie uv image), `snapshot.ubuntu.com` for the jammy-based
   ZED images. Two archives cover the whole estate.
3. **Pin the pyzed wheel** — exact URL + sha256, in the lock or the repo, replacing the
   `get_python_api.py` live fetch.
4. **aoa-bridge's toolchain stage is the one place a snapshot pin is mandatory**, not
   merely nice: it is a compiler stack producing the shipped libusb binary from
   otherwise-pinned source; today the output bytes are a function of the archive du
   jour.

Rejected alternatives: hand version-pinning (`pkg=1.2.3-…`) — hand-maintained, rot,
distro-string pain across suites, and it still fetches from the live archive, buying
loudness but not hermeticity. A prebuilt digest-pinned toolchain/libusb wrapper image —
just moves the question (who built the wrapper, with what archive?), i.e. recursion
unless that build is itself snapshot-pinned.

## Status

Deferred by operator choice on 2026-10-30 — recorded here so it is not forgotten. No
mitigation exists anywhere in the estate; the deficiency is live on every build.
