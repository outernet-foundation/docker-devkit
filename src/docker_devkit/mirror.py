from __future__ import annotations

import os
import shlex
from pathlib import Path

import typer
from bashrun.bash import bash, bash_check

from .config import MirrorConfig, load_config
from .image_refs import declared_references

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

CRANE_VERSION = "v0.22.1"


@app.command()
def mirror() -> None:
    root = Path.cwd()
    config = load_mirror_config(root)
    if config is None:
        raise RuntimeError("a mirror section with 'prefix' is required in docker-devkit.yaml to mirror")

    targets = sorted(declared_references(root), key=lambda occurrence: occurrence.reference)
    print(f"Install crane {CRANE_VERSION}")
    sudo = "sudo " if os.geteuid() != 0 else ""
    archive = "go-containerregistry_Linux_x86_64.tar.gz"
    bash(f"curl -fsSLO https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive}")
    bash(f"{sudo}tar -xzf {archive} -C /usr/local/bin crane")
    Path(archive).unlink()
    for target in targets:
        destination = mirrored_ref(target.reference, config.prefix)
        print(f"Mirror {target.reference} -> {destination}")
        bash(f"crane copy {target.reference} {destination}")
    print(f"Mirrored images: {len(targets)}")


def load_mirror_config(root: Path) -> MirrorConfig | None:
    config = load_config(root)
    return config.mirror if config is not None else None


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(
            f"x-base-images values are upstream-native; {reference} already carries the mirror prefix {prefix}"
        )
    return f"{prefix}/{reference}"


def reference_exists(reference: str) -> bool:
    return bash_check(f"docker buildx imagetools inspect {shlex.quote(reference)}")


def enforce_mirrored(lock_data: dict[str, str]) -> None:
    for value in sorted(lock_data.values()):
        if reference_exists(value):
            continue
        reference, _, digest = value.rpartition("@")
        raise RuntimeError(
            f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored yet; "
            "the CI mirror job populates it when the declaring change runs"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
