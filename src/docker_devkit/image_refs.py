from __future__ import annotations

import datetime
import re
import shlex
import tomllib
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path

import yaml
from bashrun.bash import bash_output
from pydantic import BaseModel, ConfigDict, Field, RootModel

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
COPY_FROM_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)
MISSING_VERSION = "<missing>"


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_ref: str | None = Field(default=None, alias="x-image-ref")


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    services: dict[str, ComposeService] = Field(default_factory=dict)


class BakeDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_images: dict[str, str] = Field(default_factory=dict, alias="x-base-images")


type TomlValue = (
    str
    | int
    | float
    | bool
    | datetime.datetime
    | datetime.date
    | datetime.time
    | list[TomlValue]
    | dict[str, TomlValue]
)


class TomlDocument(RootModel[TomlValue]):
    pass


@dataclass(frozen=True)
class ImageReference:
    name: str
    reference: str


@dataclass(frozen=True)
class VersionSite:
    description: str
    glob: str
    pattern: str
    base_image: str | None = None


@dataclass(frozen=True)
class VersionCoupling:
    name: str
    pyproject_key: str
    sites: tuple[VersionSite, ...]


def collect_repo_references(
    root: Path,
    compose_glob: str = "compose*.yml",
    bake_glob: str = "compose*.bake.yml",
    dockerfile_glob: str | None = "docker/*/Dockerfile*",
    image_glob: str | None = "score/*.yaml",
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
        + (
            [
                ImageReference("", match.group(1))
                for path in sorted(root.glob(dockerfile_glob))
                for match in FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
            ]
            + [
                ImageReference("", match.group(1))
                for path in sorted(root.glob(dockerfile_glob))
                for match in COPY_FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
                if "/" in match.group(1)
            ]
            if dockerfile_glob
            else []
        )
        + (
            [
                ImageReference("", match.group(1))
                for path in sorted(root.glob(image_glob))
                for match in IMAGE_LINE_PATTERN.finditer(path.read_text(encoding="utf-8"))
            ]
            if image_glob
            else []
        )
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


def unpinned_references(root: Path) -> list[str]:
    return [
        occurrence.reference
        for occurrence in collect_repo_references(root)
        if "/" in (reference := strip_build_args(occurrence.reference))
        and not BUILD_ARG_PATTERN.search(occurrence.reference)
        and ":" not in (tail := reference[reference.rfind("/") + 1 :])
        and "@" not in tail
    ]


def version_coupling_violations(root: Path, couplings: list[VersionCoupling]) -> list[str]:
    return [
        f"{coupling.name}: {site.description} at {path}: expected {expected}, found {found}"
        for coupling in couplings
        for expected in [_declared_version(_pyproject_value(root, coupling.pyproject_key))]
        for site in coupling.sites
        for path in _site_paths(root, site)
        for found in [_site_version(path, site)]
        if found != expected
    ]


def _pyproject_value(root: Path, dotted_key: str) -> str:
    value = TomlDocument.model_validate(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))).root
    for component in dotted_key.split("."):
        if not isinstance(value, dict) or component not in value:
            raise RuntimeError(f"pyproject key {dotted_key} is missing or non-table at {component}")
        value = value[component]
    if not isinstance(value, str):
        raise TypeError(f"pyproject key {dotted_key} is not a string")
    return value


def _declared_version(specifier: str) -> str:
    return re.sub(r"^[^0-9]+", "", specifier).split(",")[0].strip()


def _site_paths(root: Path, site: VersionSite) -> list[Path]:
    return sorted(root.glob(site.glob)) or [Path(site.glob)]


def _site_version(path: Path, site: VersionSite) -> str:
    if not path.exists():
        return MISSING_VERSION
    haystack = _bake_base_image(path, site.base_image) if site.base_image else path.read_text(encoding="utf-8")
    match = re.search(site.pattern, haystack)
    return match.group(1) if match else MISSING_VERSION


def _bake_base_image(path: Path, key: str) -> str:
    return next(
        (reference.reference for reference in bake_base_image_refs(_load_yaml_document(path)) if reference.name == key),
        "",
    )


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Could not parse digest for image reference: {reference}")
    return match.group(1)
