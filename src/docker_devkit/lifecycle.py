from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .detect_gpu import Gpu

BAKE_FILE = Path("compose.bake.yml")
DEFAULT_COMPOSE_FILE = Path("compose.yml")
GPU_PLACEHOLDER = "{gpu}"


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


def enforce_bake_declaration(root: Path, config: LifecycleConfig | None) -> None:
    if config is None and (root / BAKE_FILE).exists():
        raise RuntimeError(
            f"{BAKE_FILE} present without [tool.docker-devkit.lifecycle]; declare the table or rename the file"
        )


def expand_compose_files(config: LifecycleConfig | None, gpu: Gpu, *, include_dev: bool = False) -> list[Path]:
    if config is None:
        return [DEFAULT_COMPOSE_FILE]
    files = [Path(entry.format(gpu=gpu)) for entry in config.files if gpu != "none" or GPU_PLACEHOLDER not in entry]
    if include_dev and config.dev_file is not None:
        files.append(Path(config.dev_file))
    return files
