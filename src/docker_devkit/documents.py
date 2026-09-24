from __future__ import annotations

import re
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ModelT = TypeVar("ModelT", bound=BaseModel)

DECLARED_REFERENCE_PATTERN = re.compile(
    r"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)
DIGEST_TAIL_PATTERN = re.compile(r"@sha256:[a-f0-9]{64}$")


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

    @field_validator("base_images")
    @classmethod
    def _require_tag_or_digest(cls, base_images: dict[str, str]) -> dict[str, str]:
        for name, reference in base_images.items():
            match = DECLARED_REFERENCE_PATTERN.fullmatch(reference)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"x-base-images.{name}: {reference!r} must be 'path:tag' or 'path@digest'")
        return base_images


def digest_pinned(reference: str) -> bool:
    return DIGEST_TAIL_PATTERN.search(reference) is not None


def parse_bake(path: Path) -> BakeDocument:
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
    try:
        return BakeDocument.model_validate(loader.get_single_data())
    finally:
        loader.dispose()
