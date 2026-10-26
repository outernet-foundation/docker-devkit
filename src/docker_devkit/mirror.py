from __future__ import annotations

import os
from pathlib import Path

import typer
from bashrun.bash import bash, bash_check

from .config import MirrorConfig, load_config
from .documents import parse_bake
from .image_refs import bake_base_image_refs
from .lifecycle import LOCK_PATH, MANIFEST_PATH, parse_env_file

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
    lock_file = root / LOCK_PATH
    lock_entries = parse_env_file(lock_file) if lock_file.exists() else {}
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
    mirror_targets = [(occurrence.reference, lock_entries.get(occurrence.name)) for occurrence in targets]
    for chart in charts:
        chart_reference = chart.ref.removeprefix("oci://")
        mirror_targets.append((chart_reference, mirrored_ref(chart_reference, config.prefix)))
    copied = 0
    already_present = 0
    for reference, presence in mirror_targets:
        destination = mirrored_ref(reference, config.prefix)
        if presence is not None and bash_check(f"crane digest {presence}"):
            print(f"Already mirrored {presence}")
            already_present += 1
            continue
        print(f"Mirror {reference} -> {destination}")
        bash(f"crane copy {reference} {destination}")
        copied += 1
    print(f"Mirrored references: {copied} copied, {already_present} already present")


def load_mirror_config(root: Path) -> MirrorConfig | None:
    config = load_config(root)
    return config.mirror if config is not None else None


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(
            f"x-base-images values are upstream-native; {reference} already carries the mirror prefix {prefix}"
        )
    return f"{prefix}/{reference}"
