from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path

import yaml
from bashrun import bash_output
from pydantic import BaseModel, ConfigDict, Field

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_ref: str | None = Field(default=None, alias="x-image-ref")


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    services: dict[str, ComposeService] = Field(default_factory=dict)


class BakeDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_images: dict[str, str] = Field(default_factory=dict, alias="x-base-images")


@dataclass(frozen=True)
class ImageReference:
    name: str
    reference: str


def collect_repo_references(
    root: Path,
    compose_glob: str = "compose*.yml",
    bake_glob: str = "compose*.bake.yml",
    dockerfile_glob: str = "docker/*/Dockerfile*",
    image_glob: str = "score/*.yaml",
) -> list[ImageReference]:
    return (
        [
            reference
            for path in sorted(root.glob(compose_glob))
            for reference in compose_service_refs(_load_yaml_document(path))
        ]
        + [
            reference
            for path in sorted(root.glob(bake_glob))
            for reference in bake_base_image_refs(_load_yaml_document(path))
        ]
        + [
            ImageReference("", match.group(1))
            for path in sorted(root.glob(dockerfile_glob))
            for match in FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
        + [
            ImageReference("", match.group(1))
            for path in sorted(root.glob(image_glob))
            for match in IMAGE_LINE_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
    )


def compose_service_refs(document: object) -> list[ImageReference]:
    return [
        ImageReference(service_name, service.image_ref)
        for service_name, service in ComposeDocument.model_validate(document).services.items()
        if service.image_ref is not None
    ]


def bake_base_image_refs(document: object) -> list[ImageReference]:
    return list(starmap(ImageReference, BakeDocument.model_validate(document).base_images.items()))


def _load_yaml_document(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def strip_build_args(reference: str) -> str:
    return BUILD_ARG_PATTERN.sub("", reference).strip()


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Could not parse digest for image reference: {reference}")
    return match.group(1)
