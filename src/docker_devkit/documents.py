from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal, Self

import pathspec
import yaml
from bashrun.bash import bash, bash_output
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .config import FAN_ARG, DockerDevkitConfig, service_sha_env

KEEP_STORAGE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([TGMK]i?B)?$", re.IGNORECASE)


class ImageLock(RootModel[dict[str, str]]):
    # The env-format pair file beside the bake manifest; entries parse from raw text so
    # errors can name the offending line.
    @model_validator(mode="before")
    @classmethod
    def _parse_entries(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str):
            return value
        context: dict[str, Any] = info.context or {}
        source = context.get("path", "the image lock")
        entries: dict[str, str] = {}
        for number, line in enumerate(value.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, separator, reference = stripped.partition("=")
            if not separator or not name.strip() or not reference.strip():
                raise ValueError(f"{source}:{number}: lock lines are NAME=reference entries")
            if name in entries:
                raise ValueError(f"{source}:{number}: duplicate lock entry {name}")
            entries[name] = reference
        return entries

    @model_validator(mode="after")
    def _require_digest_pins(self, info: ValidationInfo) -> Self:
        context: dict[str, Any] = info.context or {}
        for name, reference in self.root.items():
            if not reference.rpartition("@")[2].startswith("sha256:"):
                raise ValueError(
                    f"{context.get('path', 'the image lock')}: entry {name} is not digest-pinned "
                    f"({reference!r}); build writes @sha256:… pins"
                )
        return self


class BindVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["bind"]
    source: str
    target: str

    @field_validator("source")
    @classmethod
    def _require_relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError(f"absolute bind source {value} cannot be inlined hermetically")
        return value


class OtherVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str

    # Reject malformed binds so they fail validation instead of passing through as non-binds.
    @model_validator(mode="after")
    def _reject_bind(self) -> Self:
        if self.type == "bind":
            raise ValueError("a bind volume is missing its source or target")
        return self


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None


class ComposeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str | None = None


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] | None = None
    configs: dict[str, ComposeConfig] | None = None

    @model_validator(mode="after")
    def _reject_colliding_bind_basenames(self) -> Self:
        # Config names are bind basenames, so two binds sharing one collide however each
        # resolves; the model sees the raw sources, and their basenames never change.
        seen: dict[str, str] = {}
        for service in (self.services or {}).values():
            for volume in service.volumes or []:
                if not isinstance(volume, BindVolume):
                    continue
                source = volume.source
                known = seen.setdefault(Path(source).name, source)
                if known != source:
                    raise ValueError(f"bind sources {known} and {source} collide on config name {Path(source).name}")
        return self


class BakedImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_name: str = Field(default="", alias="image.name")
    digest: str = Field(default="", alias="containerimage.digest")
    ref: str = ""
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _extract_references(self) -> Self:
        for reference in self.image_name.split(","):
            reference = reference.strip()
            if not reference:
                continue
            repository, separator, tag = reference.split("@", 1)[0].rpartition(":")
            if separator:
                self.ref = repository
                self.tags.append(tag)
        return self


class BakeMetadata(RootModel[dict[str, BakedImage]]):
    @model_validator(mode="after")
    def _require_tagged_references(self) -> Self:
        for target, image in self.root.items():
            if not image.tags:
                raise ValueError(f"target '{target}' produced no tagged image references from '{image.image_name}'")
        return self


class DaemonGc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_keep_storage: int | str | None = Field(default=None, alias="defaultKeepStorage")

    @field_validator("default_keep_storage")
    @classmethod
    def _require_parseable_size(cls, value: int | str | None) -> int | str | None:
        if value is None:
            return value
        if KEEP_STORAGE_PATTERN.match(str(value)) is None:
            raise ValueError(f"Could not parse 'defaultKeepStorage' value: {value}")
        return value


class DaemonBuilder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gc: DaemonGc = Field(default_factory=DaemonGc)


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builder: DaemonBuilder = Field(default_factory=DaemonBuilder)

    @model_validator(mode="after")
    def _require_keep_storage(self, info: ValidationInfo) -> Self:
        if self.builder.gc.default_keep_storage:
            return self
        context: dict[str, Any] = info.context or {}
        sample = json.dumps({"builder": {"gc": {"defaultKeepStorage": f"{context.get('min_gb', 60)}GB"}}}, indent=2)
        raise ValueError(
            f"builder.gc.defaultKeepStorage missing in {context.get('path', 'daemon.json')}; "
            f"need >= {context.get('min_gb', 60)}GB:\n\n{sample}"
        )


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""
    name: str = ""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    metadata: ManifestMetadata | None = None


class WorkloadState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instance_suffix: str = ""


class ScoreState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workloads: dict[str, WorkloadState] = {}


def render_bake(config: DockerDevkitConfig, *, push: bool, host: str, dev: bool = False) -> str:
    # The per-run temp bake file: bake validates its own input by failing on it, so no
    # model sits behind the render. Identical configs render byte-identical files.
    services: dict[str, dict[str, Any]] = {}
    for service in config.service_builds.values():
        for target, variant in service.target_variants:
            args: dict[str, str] = {key: f"${{{key}:-}}" for key in config.base_images}
            if variant is not None:
                args[FAN_ARG] = variant
            args.update({sha: f"${{{sha}:?err}}" for sha in service.sha_args})
            build: dict[str, Any] = {"dockerfile": service.dockerfile}
            if dev:
                build["target"] = "dev"
            if config.platforms:
                # A --load run writes a single arch, so a multi-arch declaration collapses
                # to the host; a single declared platform rides through in both modes.
                build["platforms"] = [host] if not push and len(config.platforms) > 1 else list(config.platforms)
            build["args"] = args
            if dev:
                # The dev tag is per service, not per target: whichever gpu variant a host
                # builds, the overlay references one {repository}/{service}:dev image.
                build["tags"] = [f"{config.repository}/{service.name}:dev"]
            else:
                build["tags"] = [
                    f"{config.repository}/{target}:latest",
                    f"{config.repository}/{target}:${{{service_sha_env(service.name)}}}",
                ]
            if service.contexts:
                build["additional_contexts"] = {
                    context: f"target:{context}-{variant}" if variant is not None else f"target:{context}"
                    for context in service.contexts
                }
            if push:
                cache = f"{config.repository}/build-cache"
                build["cache_to"] = [
                    f"type=registry,ref={cache}:{target},mode=max,image-manifest=true,oci-mediatypes=true"
                ]
                build["cache_from"] = [f"type=registry,ref={cache}:{target}"]
            services[target] = {"build": build}
    return yaml.safe_dump({"services": services}, sort_keys=False)


def parse_image_lock(path: Path) -> ImageLock:
    return ImageLock.model_validate(path.read_text(encoding="utf-8"), context={"path": path.as_posix()})


def compute_service_shas(repo_root: Path, config: DockerDevkitConfig) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs = {str(PurePosixPath(service.dockerfile).parent) for service in config.service_builds.values()}

    if not service_dirs:
        # A repo with no Dockerfile services (mirror-only, or bundles built elsewhere)
        # has nothing to hash; skip the git and .dockerignore reads entirely.
        return {}

    # Derive the workload root as the single top-level directory holding every service;
    # files under it that belong to no service are excluded from every SHA.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs if PurePosixPath(service_dir).parts}
    if len(tops) > 1:
        raise RuntimeError(f"services span multiple top-level directories: {sorted(tops)}")
    workload_root = PurePosixPath(next(iter(tops))) if tops else None

    tree_entries = bash_output("git ls-tree -r HEAD", cwd=repo_root).splitlines()

    dockerignore = repo_root / ".dockerignore"
    spec = pathspec.PathSpec.from_lines("gitignore", dockerignore.read_text().splitlines())

    allowed_entries: list[tuple[str, str, str]] = []
    for entry in tree_entries:
        meta, path = entry.split("\t", 1)
        if spec.match_file(path):
            continue
        mode, _type, obj_hash = meta.split()
        allowed_entries.append((mode, obj_hash, path))

    workload_prefix = workload_root.as_posix() + "/" if workload_root is not None else None
    shared_entries: list[tuple[str, str, str]] = []
    per_service: dict[str, list[tuple[str, str, str]]] = {directory: [] for directory in service_dirs}

    for mode, obj_hash, path in allowed_entries:
        if workload_prefix is not None and path.startswith(workload_prefix):
            for service_dir in service_dirs:
                if path.startswith(service_dir + "/"):
                    per_service[service_dir].append((mode, obj_hash, path))
                    break
        else:
            shared_entries.append((mode, obj_hash, path))

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        entries = shared_entries + per_service[service_dir]
        index_input = "\n".join(f"{mode} {obj_hash}\t{path}" for mode, obj_hash, path in entries) + "\n"

        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            bash("git update-index --index-info", cwd=repo_root, env=index_env, stdin_text=index_input)
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[service_sha_env(PurePosixPath(service_dir).name)] = f"tree-{tree_hash}"

    return result
