from __future__ import annotations

import os
from pathlib import Path

import typer
from bashrun.bash import bash

from .config import MirrorConfig, load_config
from .documents import parse_bake
from .image_refs import bake_base_image_refs
from .lifecycle import MANIFEST_PATH

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

CRANE_VERSION = "v0.22.1"


@app.command()
def mirror() -> None:
    root = Path.cwd()
    devkit = load_config(root)
    if devkit is None or devkit.mirror is None:
        raise RuntimeError("a mirror section with 'prefix' is required in docker-devkit.yaml to mirror")
    config = devkit.mirror

    manifest = root / MANIFEST_PATH
    declared = bake_base_image_refs(parse_bake(manifest)) if manifest.exists() else []
    targets = sorted(declared, key=lambda occurrence: occurrence.reference)
    charts = (
        sorted(devkit.generate_score.charts, key=lambda chart: chart.ref) if devkit.generate_score is not None else []
    )
    print(f"Install crane {CRANE_VERSION}")
    sudo = "sudo " if os.geteuid() != 0 else ""
    archive = "go-containerregistry_Linux_x86_64.tar.gz"
    bash(f"curl -fsSLO https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive}")
    bash(f"{sudo}tar -xzf {archive} -C /usr/local/bin crane")
    Path(archive).unlink()
    references = [occurrence.reference for occurrence in targets] + [
        chart.ref.removeprefix("oci://") for chart in charts
    ]
    for reference in references:
        destination = mirrored_ref(reference, config.prefix)
        print(f"Mirror {reference} -> {destination}")
        bash(f"crane copy {reference} {destination}")
    print(f"Mirrored references: {len(references)}")


def load_mirror_config(root: Path) -> MirrorConfig | None:
    config = load_config(root)
    return config.mirror if config is not None else None


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(
            f"x-base-images values are upstream-native; {reference} already carries the mirror prefix {prefix}"
        )
    return f"{prefix}/{reference}"
