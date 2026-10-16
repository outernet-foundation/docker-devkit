from __future__ import annotations

import shutil
from os import environ
from pathlib import Path
from typing import Literal, get_args

from bashrun.bash import bash_check, bash_output

from .config import LifecycleConfig, load_config

DEFAULT_COMPOSE_FILE = Path("compose.yml")
GPU_PLACEHOLDER = "{gpu}"
Gpu = Literal["auto", "cuda", "rocm", "none"]
GPU_TYPES = tuple(gpu for gpu in get_args(Gpu) if gpu not in ("auto", "none"))

# Treat the image manifest and its lock as a named pair in the same directory.
MANIFEST_PATH = Path("workloads/images.yml")
LOCK_PATH = Path("workloads/images.lock")


def load_lifecycle_config(root: Path) -> LifecycleConfig | None:
    config = load_config(root)
    return config.lifecycle if config is not None else None


def require_manifest(root: Path) -> Path:
    manifest = resolve_manifest(root)
    if manifest is None:
        raise RuntimeError(f"No image manifest at {MANIFEST_PATH}")
    return manifest


def resolve_manifest(root: Path) -> Path | None:
    manifest = root / MANIFEST_PATH
    return manifest if manifest.exists() else None


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


def detect_gpu() -> Gpu:
    if shutil.which("nvidia-smi") and bash_check("nvidia-smi"):
        return "cuda"
    if shutil.which("rocminfo") and bash_check("rocminfo"):
        return "rocm"
    raise RuntimeError("Could not detect GPU type.")


def resolve_compose_profiles(env_file: Path) -> list[str]:
    file_values = parse_env_file(env_file)
    raw_profiles = environ.get("COMPOSE_PROFILES") or file_values.get("COMPOSE_PROFILES", "")
    profiles: list[str] = []
    for entry in raw_profiles.split(","):
        profile = entry.strip()
        if profile and profile not in profiles:
            profiles.append(profile)
    return profiles


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result
