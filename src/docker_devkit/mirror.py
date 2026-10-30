from __future__ import annotations

from pathlib import Path

import typer
from bashrun.bash import bash, bash_check

from .config import DEFAULT_MIRROR_PREFIX, LOCK_PATH, MirrorConfig, chart_lock_key, load_config
from .crane import install_crane
from .documents import parse_image_lock

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def mirror() -> None:
    root = Path.cwd()
    config = load_config(root)
    if config is None:
        raise RuntimeError("a docker-devkit.yaml with base_images (or k8s.charts) is required to mirror")
    prefix = config.mirror.prefix if config.mirror is not None else DEFAULT_MIRROR_PREFIX

    lock_entries = parse_image_lock(root / LOCK_PATH).root if (root / LOCK_PATH).exists() else {}
    mirror_targets = [
        (reference, lock_entries.get(name))
        for name, reference in sorted(config.base_images.items(), key=lambda item: item[1])
    ]
    for chart_name, chart_ref in sorted((config.k8s.charts if config.k8s is not None else {}).items()):
        entry = lock_entries.get(chart_lock_key(chart_name))
        if entry is None:
            raise RuntimeError(
                f"chart {chart_name!r}: {LOCK_PATH.as_posix()} lacks the {chart_lock_key(chart_name)} entry "
                "consumers render (build --lock-only writes it)"
            )
        mirror_targets.append((chart_ref.removeprefix("oci://"), entry))

    if not mirror_targets:
        raise RuntimeError("nothing is declared to mirror (base_images and k8s.charts are empty)")

    install_crane()
    copied = 0
    already_present = 0
    for reference, presence in mirror_targets:
        if presence is not None and bash_check(f"crane digest {presence}"):
            print(f"Already mirrored {presence}")
            already_present += 1
            continue
        destination = mirrored_ref(reference, prefix)
        print(f"Mirror {reference} -> {destination}")
        bash(f"crane copy {reference} {destination}")
        copied += 1
    print(f"Mirrored references: {copied} copied, {already_present} already present")


def load_mirror_config(root: Path) -> MirrorConfig | None:
    config = load_config(root)
    return config.mirror if config is not None else None


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(f"declarations are upstream-native; {reference} already carries the mirror prefix {prefix}")
    return f"{prefix}/{reference}"
