from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .detect_gpu import Gpu

DEFAULT_COMPOSE_FILE = Path("compose.yml")
GPU_PLACEHOLDER = "{gpu}"

# The image manifest and its lock are a named pair living in the same directory. The
# legacy names keep resolving so a repo can upgrade the devkit before moving its files.
MANIFEST_CANDIDATES = (Path("workloads/images.yml"), Path("compose.bake.yml"))
MANIFEST_LOCK_PAIR = {"images.yml": "images.lock", "compose.bake.yml": ".env.lock"}
LOCK_CANDIDATES = (Path("workloads/images.lock"), Path(".env.lock"))


class LifecycleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(default_factory=list)
    dev_file: str | None = None


def load_lifecycle_config(root: Path) -> LifecycleConfig | None:
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get("docker-devkit", {}).get("lifecycle")
    if section is None:
        return None
    return LifecycleConfig.model_validate(section)


def resolve_manifest(root: Path) -> Path | None:
    return next((root / candidate for candidate in MANIFEST_CANDIDATES if (root / candidate).exists()), None)


def require_manifest(root: Path) -> Path:
    manifest = resolve_manifest(root)
    if manifest is None:
        raise RuntimeError(f"No image manifest found; {MANIFEST_CANDIDATES[0]} is the canonical name")
    return manifest


def resolve_lock(root: Path, manifest: Path | None) -> Path:
    if manifest is not None:
        return manifest.parent / MANIFEST_LOCK_PAIR[manifest.name]
    existing = next((root / candidate for candidate in LOCK_CANDIDATES if (root / candidate).exists()), None)
    return existing if existing is not None else root / LOCK_CANDIDATES[0]


def enforce_bake_declaration(root: Path, config: LifecycleConfig | None) -> None:
    if config is None and any((root / candidate).exists() for candidate in MANIFEST_CANDIDATES):
        raise RuntimeError(
            "image manifest present without [tool.docker-devkit.lifecycle]; declare the table or rename the file"
        )


def expand_compose_files(config: LifecycleConfig | None, gpu: Gpu, *, include_dev: bool = False) -> list[Path]:
    if config is None:
        return [DEFAULT_COMPOSE_FILE]
    files = [Path(entry.format(gpu=gpu)) for entry in config.files if gpu != "none" or GPU_PLACEHOLDER not in entry]
    if include_dev and config.dev_file is not None:
        files.append(Path(config.dev_file))
    return files
