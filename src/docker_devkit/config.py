from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Self

from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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


class PublishComposeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    artifact: str
    files: list[str] = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)

    @field_validator("repository")
    @classmethod
    def _require_registry_repository(cls, repository: str) -> str:
        host, separator, path = repository.partition("/")
        if not separator or not host or not path or repository.endswith("/"):
            raise ValueError(
                "repository must be registry-host/repository-path with no trailing slash (ghcr.io/org/repo)"
            )
        return repository

    @field_validator("artifact")
    @classmethod
    def _require_oci_artifact_name(cls, artifact: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", artifact):
            raise ValueError("artifact must be lowercase alphanumerics and dashes (an OCI repository name)")
        return artifact

    @field_validator("variants")
    @classmethod
    def _require_unique_variants(cls, variants: list[str]) -> list[str]:
        if len(variants) != len(set(variants)):
            raise ValueError("variants must be unique")
        return variants


class ChartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ref: str
    values: list[Path] = Field(default_factory=list)
    namespace: str | None = None

    @field_validator("ref")
    @classmethod
    def _require_digest_pinned_oci(cls, ref: str) -> str:
        if not ref.startswith("oci://"):
            raise ValueError("chart refs are upstream-native OCI references (oci://registry/path@sha256:…)")
        if "@sha256:" not in ref:
            raise ValueError("chart refs are digest-pinned; a tag would move under the same name")
        return ref


class SecretsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: Path
    k8s_secret: str
    output: Path = Path("stack/generated/secrets.txt")


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
    # Write --local output here; consumers gitignore it so a local storage class cannot reach
    # the artifact their cluster deploys.
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    k8s_state: Path = Path("stack/generated/k8s/.score-k8s")
    storage_class_var: str = "SCORE_STORAGE_CLASS"
    compose_provisioners: list[str] = Field(default_factory=list)
    compose_patch_templates: list[str] = Field(default_factory=list)
    k8s_provisioners: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    secrets: SecretsConfig | None = None
    charts: list[ChartConfig] = Field(default_factory=list)
    helm_version: str = ""
    score_k8s_version: str = "0.15.0"
    score_compose_version: str = "0.42.0"

    @model_validator(mode="after")
    def _require_helm_for_charts(self) -> Self:
        if self.charts and not self.helm_version:
            raise ValueError("helm_version is required when charts are declared")
        names = [chart.name for chart in self.charts]
        if len(names) != len(set(names)):
            raise ValueError("chart names must be unique; a duplicate collides on rendered resource names")
        return self


class DockerDevkitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    requires: str
    lifecycle: LifecycleConfig | None = None
    mirror: MirrorConfig | None = None
    publish_compose: PublishComposeConfig | None = Field(default=None, alias="publish-compose")
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
