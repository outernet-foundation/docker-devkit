from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Literal, get_args

from bashrun.bash import bash_check

from .config import GpuFamily

# A flat Literal: typer cannot annotate unions and pyright cannot type a star-unpacked
# subscript, so the family membership pins by test instead of by derivation.
Gpu = Literal["auto", "none", "cuda", "rocm"]
GPU_TYPES = get_args(GpuFamily)


def detect_gpu() -> Gpu:
    if shutil.which("nvidia-smi") and bash_check("nvidia-smi"):
        return "cuda"
    if shutil.which("rocminfo") and bash_check("rocminfo"):
        return "rocm"
    raise RuntimeError("Could not detect GPU type.")


def host_platform() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    raise RuntimeError(f"Unsupported host architecture: {machine}")


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result
