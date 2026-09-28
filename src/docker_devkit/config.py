from __future__ import annotations

import importlib.metadata
from pathlib import Path

from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, field_validator
from strictyaml import load as load_strict_yaml

DEFAULT_CONFIG_PATH = Path("docker-devkit.yaml")
DEV_SENTINEL = "0.0.0.dev0"


class LifecycleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(default_factory=list)
    dev_file: str | None = None


class MirrorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str

    @field_validator("prefix")
    @classmethod
    def _require_bare_prefix(cls, prefix: str) -> str:
        if not prefix or prefix.endswith("/"):
            raise ValueError("mirror prefix must be non-empty and carry no trailing slash")
        return prefix


class ScoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str]
    project_name: str
    cloud_storage_class: str
    local_storage_class: str
    score_dir: Path = Path("stack/score")
    bake_file: Path = Path("workloads/images.yml")
    compose_output: Path = Path("stack/generated/compose/compose.yaml")
    k8s_output: Path = Path("stack/generated/k8s/manifests.yaml")
    # --local writes here instead of the committed k8s output; consumers gitignore this path so a
    # local-only storage class cannot reach the artifact their cluster deploys.
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    k8s_state: Path = Path("stack/generated/k8s/.score-k8s")
    storage_class_var: str = "SCORE_STORAGE_CLASS"
    compose_provisioners: list[str] = Field(default_factory=list)
    compose_patch_templates: list[str] = Field(default_factory=list)
    k8s_provisioners: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    score_k8s_version: str = "0.15.0"
    score_compose_version: str = "0.42.0"


class DockerDevkitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    requires: str
    lifecycle: LifecycleConfig | None = None
    mirror: MirrorConfig | None = None
    generate_score: ScoreConfig | None = Field(default=None, alias="generate-score")

    @field_validator("requires")
    @classmethod
    def _validate_requires(cls, value: str) -> str:
        if not value:
            raise ValueError("requires must be a non-empty PEP 440 specifier; use '>=0.0' for no constraint")
        SpecifierSet(value)
        return value


def load_config(root: Path) -> DockerDevkitConfig | None:
    path = root / DEFAULT_CONFIG_PATH
    if not path.exists():
        return None
    config = DockerDevkitConfig.model_validate(load_strict_yaml(path.read_text(encoding="utf-8")).data)
    installed = importlib.metadata.version("docker-devkit")
    if installed == DEV_SENTINEL:
        return config
    if not SpecifierSet(config.requires).contains(installed, prereleases=True):
        raise SystemExit(f"docker-devkit {installed} does not satisfy requires={config.requires!r}")
    return config
