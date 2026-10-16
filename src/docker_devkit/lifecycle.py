from __future__ import annotations

from pathlib import Path

from bashrun.bash import bash_output

from .config import LifecycleConfig, load_config
from .detect_gpu import Gpu

DEFAULT_COMPOSE_FILE = Path("compose.yml")
GPU_PLACEHOLDER = "{gpu}"

# Treat the image manifest and its lock as a named pair in the same directory.
MANIFEST_PATH = Path("workloads/images.yml")
LOCK_PATH = Path("workloads/images.lock")


def load_lifecycle_config(root: Path) -> LifecycleConfig | None:
    config = load_config(root)
    return config.lifecycle if config is not None else None


def resolve_manifest(root: Path) -> Path | None:
    manifest = root / MANIFEST_PATH
    return manifest if manifest.exists() else None


def require_manifest(root: Path) -> Path:
    manifest = resolve_manifest(root)
    if manifest is None:
        raise RuntimeError(f"No image manifest at {MANIFEST_PATH}")
    return manifest


def enforce_bake_declaration(root: Path, config: LifecycleConfig | None) -> None:
    if config is None and (root / MANIFEST_PATH).exists():
        raise RuntimeError(
            "image manifest present without a lifecycle section in docker-devkit.yaml; declare the section"
        )


def expand_compose_files(config: LifecycleConfig | None, gpu: Gpu, *, include_dev: bool = False) -> list[Path]:
    if config is None:
        return [DEFAULT_COMPOSE_FILE]
    files = [Path(entry.format(gpu=gpu)) for entry in config.files if gpu != "none" or GPU_PLACEHOLDER not in entry]
    if include_dev and config.dev_file is not None:
        files.append(Path(config.dev_file))
    return files


def enumerate_profiles(compose_prefix: str) -> list[str]:
    output = bash_output(f"{compose_prefix} config --profiles")
    return [line.strip() for line in output.splitlines() if line.strip()]
