from __future__ import annotations

import importlib.metadata
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


class SeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    output: Path
    workloads: list[str]
    gpu_variants: list[str] = Field(default_factory=list)
    provisioners: list[str] = Field(default_factory=list)
    patch_templates: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _gpu_token_pairs_with_variants(self) -> Self:
        templated = ["{gpu}" in self.output.as_posix()] + ["{gpu}" in template for template in self.patch_templates]
        if self.gpu_variants and not templated[0]:
            raise ValueError(
                f"seam {self.name!r}: gpu_variants declared — output must carry a {{gpu}} token "
                "or every variant overwrites the same file"
            )
        if not self.gpu_variants and any(templated):
            raise ValueError(
                f"seam {self.name!r}: output or patch_templates carry {{gpu}} but no gpu_variants "
                "are declared; there is nothing to substitute"
            )
        if any(not variant for variant in self.gpu_variants):
            raise ValueError(f"seam {self.name!r}: gpu variant names must be non-empty")
        if len(self.gpu_variants) != len(set(self.gpu_variants)):
            raise ValueError(
                f"seam {self.name!r}: gpu variant names must be unique; duplicates overwrite the same output"
            )
        if not self.name:
            raise ValueError("seam names must be non-empty")
        if not self.workloads:
            raise ValueError(f"seam {self.name!r} carries no workloads; a seam publishes at least one")
        unknown = sorted(set(self.overrides) - set(self.workloads))
        if unknown:
            raise ValueError(f"seam {self.name!r}: overrides name workloads the seam does not carry: {unknown}")
        return self


class ScoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str]
    project_name: str
    cloud_storage_class: str
    local_storage_class: str
    score_dir: Path = Path("stack/score")
    bake_file: Path = Path("workloads/images.yml")
    k8s_output: Path = Path("stack/generated/k8s/manifests.yaml")
    # Write --local output here; consumers gitignore it so a local storage class cannot reach
    # the artifact their cluster deploys.
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    k8s_state: Path = Path("stack/generated/k8s/.score-k8s")
    storage_class_var: str = "SCORE_STORAGE_CLASS"
    k8s_provisioners: list[str] = Field(default_factory=list)
    k8s_patch_templates: list[str] = Field(default_factory=list)
    seams: list[SeamConfig] = Field(default_factory=list)
    k8s_gpu_variant: str = ""
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

    @model_validator(mode="after")
    def _seam_outputs_are_unique(self) -> Self:
        names = [seam.name for seam in self.seams]
        if len(names) != len(set(names)):
            raise ValueError("seam names must be unique; a duplicate collides on artifact naming")
        outputs: set[str] = set()
        for seam in self.seams:
            for variant in seam.gpu_variants or [None]:
                output = seam.output.as_posix().format(gpu=variant)
                if output in outputs:
                    raise ValueError(
                        f"seam outputs collide on {output}; seam x variant pairs must write distinct files"
                    )
                outputs.add(output)
        return self


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
