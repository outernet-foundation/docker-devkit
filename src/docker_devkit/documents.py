from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pathspec
import yaml
from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, Field, field_validator

DECLARED_REFERENCE_PATTERN = re.compile(
    r"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)


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


def parse_bake(path: Path) -> BakeDocument:
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
    try:
        return BakeDocument.model_validate(loader.get_single_data())
    finally:
        loader.dispose()


def compute_service_shas(repo_root: Path, bake: BakeDocument) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs: set[str] = set()
    for config in bake.services.values():
        if not config.build.tags:
            continue
        service_dirs.add(str(PurePosixPath(config.build.dockerfile).parent))

    # Derive the workload root as the single top-level directory holding every tagged service;
    # files under it that belong to no tagged service are excluded from every SHA.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs if PurePosixPath(service_dir).parts}
    if len(tops) > 1:
        raise RuntimeError(f"Bake services span multiple top-level directories: {sorted(tops)}")
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

        result[PurePosixPath(service_dir).name.upper().replace("-", "_") + "_SHA"] = f"tree-{tree_hash}"

    return result
