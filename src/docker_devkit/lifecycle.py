from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, get_args

from bashrun.bash import bash_check, bash_output

DEFAULT_COMPOSE_FILE = Path("compose.yml")
GPU_PLACEHOLDER = "{gpu}"
Gpu = Literal["auto", "cuda", "rocm", "none"]
GPU_TYPES = tuple(gpu for gpu in get_args(Gpu) if gpu not in ("auto", "none"))

# Treat the image manifest and its lock as a named pair in the same directory.
MANIFEST_PATH = Path("workloads/images.yml")
LOCK_PATH = Path("workloads/images.lock")


def require_manifest(root: Path) -> Path:
    manifest = resolve_manifest(root)
    if manifest is None:
        raise RuntimeError(f"No image manifest at {MANIFEST_PATH}")
    return manifest


def resolve_manifest(root: Path) -> Path | None:
    manifest = root / MANIFEST_PATH
    return manifest if manifest.exists() else None


def enumerate_profiles(compose_prefix: str) -> list[str]:
    output = bash_output(f"{compose_prefix} config --profiles")
    return [line.strip() for line in output.splitlines() if line.strip()]


def detect_gpu() -> Gpu:
    if shutil.which("nvidia-smi") and bash_check("nvidia-smi"):
        return "cuda"
    if shutil.which("rocminfo") and bash_check("rocminfo"):
        return "rocm"
    raise RuntimeError("Could not detect GPU type.")


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result
