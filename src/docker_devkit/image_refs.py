from __future__ import annotations

import datetime as dt
import re
import shlex
import tomllib
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import Any

import yaml
from bashrun.bash import bash_output

from .config import DockerDevkitConfig

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
COPY_FROM_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)
MISSING_VERSION = "<missing>"

type TomlValue = str | int | float | bool | dt.datetime | dt.date | dt.time | list[TomlValue] | dict[str, TomlValue]


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


def unpinned_references(root: Path) -> list[str]:
    dockerfile_globs = ("workloads/*/Dockerfile*", "docker/*/Dockerfile*")
    image_globs = ("stack/score/**/*.yaml", "score/**/*.yaml")
    dockerfile_paths = [path for dockerfile_glob in dockerfile_globs for path in sorted(root.glob(dockerfile_glob))]
    image_paths = [path for image_glob in image_globs for path in sorted(root.glob(image_glob))]
    strays = (
        [
            match.group(1)
            for path in dockerfile_paths
            for match in FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
        + [
            match.group(1)
            for path in dockerfile_paths
            for match in COPY_FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
            if "/" in match.group(1)
        ]
        + [
            match.group(1)
            for path in image_paths
            for match in IMAGE_LINE_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
    )
    return [
        reference
        for reference in strays
        if not BUILD_ARG_PATTERN.search(reference)
        if "/" in (stripped := BUILD_ARG_PATTERN.sub("", reference).strip())
        if ":" not in (tail := stripped[stripped.rfind("/") + 1 :])
        if "@" not in tail
    ]


def version_coupling_violations(root: Path, couplings: list[VersionCoupling]) -> list[str]:
    violations: list[str] = []
    for coupling in couplings:
        value: TomlValue = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        for component in coupling.pyproject_key.split("."):
            if not isinstance(value, dict) or component not in value:
                raise RuntimeError(f"pyproject key {coupling.pyproject_key} is missing or non-table at {component}")
            value = value[component]
        if not isinstance(value, str):
            raise TypeError(f"pyproject key {coupling.pyproject_key} is not a string")
        expected = re.sub(r"^[^0-9]+", "", value).split(",")[0].strip()
        for site in coupling.sites:
            for path in sorted(root.glob(site.glob)) or [Path(site.glob)]:
                if not path.exists():
                    found = MISSING_VERSION
                else:
                    if site.base_image:
                        # A presence probe over the config file's base_images mapping, not
                        # schema consumption — the coupling lint reads one declared value.
                        document = yaml.safe_load(path.read_text(encoding="utf-8"))
                        base_images: dict[str, Any] = document.get("base_images") or {}
                        haystack = str(base_images.get(site.base_image, ""))
                    else:
                        haystack = path.read_text(encoding="utf-8")
                    match = re.search(site.pattern, haystack)
                    found = match.group(1) if match else MISSING_VERSION
                if found != expected:
                    violations.append(
                        f"{coupling.name}: {site.description} at {path}: expected {expected}, found {found}"
                    )
    return violations


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Could not parse digest for image reference: {reference}")
    return match.group(1)


def bake_base_image_refs(config: DockerDevkitConfig) -> list[ImageReference]:
    return list(starmap(ImageReference, config.base_images.items()))
