from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field

ModelT = TypeVar("ModelT", bound=BaseModel)


class BakeBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = "."
    dockerfile: str
    target: str | None = None
    platforms: list[str] = Field(default_factory=list)
    args: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    additional_contexts: dict[str, str] = Field(default_factory=dict)


class BakeService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build: BakeBuild


class BakeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_images: dict[str, str] = Field(default_factory=dict, alias="x-base-images")
    base_args: dict[str, str] = Field(default_factory=dict, alias="x-base-args")
    registry_cache: str | None = Field(default=None, alias="x-registry-cache")
    cross_compile_targets: list[str] = Field(default_factory=list, alias="x-cross-compile-targets")
    services: dict[str, BakeService] = Field(default_factory=dict)


def parse_bake(path: Path) -> BakeDocument:
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
    try:
        return BakeDocument.model_validate(loader.get_single_data())
    finally:
        loader.dispose()
