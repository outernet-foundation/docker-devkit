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

from .documents import BakeDocument

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
COPY_FROM_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)
MISSING_VERSION = "<missing>"


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


# Compose files may carry tags like `ports: !reset []` that plain YAML parsing
# rejects as unknown. Tagged values are never consumed here, so they parse as None.
class ComposeDialectLoader(yaml.SafeLoader):
    pass


ComposeDialectLoader.add_constructor("!reset", lambda loader, node: None)
ComposeDialectLoader.add_constructor("!override", lambda loader, node: None)


def compose_image_refs(compose_file: Path) -> list[ImageReference]:
    # Includes are not followed — no repo declares x-image-ref inside an included file.
    document = yaml.load(compose_file.read_text(encoding="utf-8"), Loader=ComposeDialectLoader)
    services = document.get("services", {}) if isinstance(document, dict) else {}
    return [
        ImageReference(name, service["x-image-ref"])
        for name, service in services.items()
        if isinstance(service, dict) and isinstance(service.get("x-image-ref"), str)
    ]


def unpinned_references(root: Path) -> list[str]:
    return [
        occurrence.reference
        for occurrence in collect_repo_references(root)
        if "/" in (reference := strip_build_args(occurrence.reference))
        and not BUILD_ARG_PATTERN.search(occurrence.reference)
        and ":" not in (tail := reference[reference.rfind("/") + 1 :])
        and "@" not in tail
    ]


def collect_repo_references(
    root: Path,
    compose_glob: str = "compose*.yml",
    bake_glob: str = "compose*.bake.yml",
    dockerfile_glob: str | None = "docker/*/Dockerfile*",
    image_glob: str | None = "score/*.yaml",
) -> list[ImageReference]:
    return (
        [reference for path in sorted(root.glob(compose_glob)) for reference in compose_image_refs(path)]
        + [
            reference
            for path in sorted(root.glob(bake_glob))
            for reference in bake_base_image_refs(yaml.safe_load(path.read_text(encoding="utf-8")))
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


def version_coupling_violations(root: Path, couplings: list[VersionCoupling]) -> list[str]:
    return [
        f"{coupling.name}: {site.description} at {path}: expected {expected}, found {found}"
        for coupling in couplings
        for expected in [re.sub(r"^[^0-9]+", "", _pyproject_value(root, coupling.pyproject_key)).split(",")[0].strip()]
        for site in coupling.sites
        for path in sorted(root.glob(site.glob)) or [Path(site.glob)]
        for found in [_site_version(path, site)]
        if found != expected
    ]


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Could not parse digest for image reference: {reference}")
    return match.group(1)


def strip_build_args(reference: str) -> str:
    return BUILD_ARG_PATTERN.sub("", reference).strip()


def _pyproject_value(root: Path, dotted_key: str) -> str:
    value = TomlDocument.model_validate(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))).root
    for component in dotted_key.split("."):
        if not isinstance(value, dict) or component not in value:
            raise RuntimeError(f"pyproject key {dotted_key} is missing or non-table at {component}")
        value = value[component]
    if not isinstance(value, str):
        raise TypeError(f"pyproject key {dotted_key} is not a string")
    return value


def _site_version(path: Path, site: VersionSite) -> str:
    if not path.exists():
        return MISSING_VERSION
    if site.base_image:
        haystack = next(
            (
                reference.reference
                for reference in bake_base_image_refs(yaml.safe_load(path.read_text(encoding="utf-8")))
                if reference.name == site.base_image
            ),
            "",
        )
    else:
        haystack = path.read_text(encoding="utf-8")
    match = re.search(site.pattern, haystack)
    return match.group(1) if match else MISSING_VERSION


def bake_base_image_refs(document: object) -> list[ImageReference]:
    return list(starmap(ImageReference, BakeDocument.model_validate(document).base_images.items()))
