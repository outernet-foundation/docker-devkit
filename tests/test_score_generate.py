from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit import score_generate
from docker_devkit.config import ChartConfig, SecretsConfig, SeamConfig
from docker_devkit.score_generate import (
    BindVolume,
    ComposeService,
    ScoreConfig,
    merge_override,
    compose_seams,
    generate_k8s,
    tool_requirements,
    write_generated_file,
)

CONFIG_BASE = {
    "workloads": ["api.yaml"],
    "project_name": "example",
    "cloud_storage_class": "cloud-volumes",
    "local_storage_class": "local-path",
}
DIGEST = "sha256:" + "a" * 64

SEAM_BASE = {
    "name": "core",
    "output": "stack/generated/compose/compose.{gpu}.yaml",
    "workloads": ["api.yaml"],
}


def test_score_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ScoreConfig.model_validate({
            "workloads": ["api.yaml"],
            "project_name": "example",
            "cloud_storage_class": "cloud-volumes",
            "local_storage_class": "local-path",
            "publication": ["8000:api:8000"],
        })


def test_chart_config_accepts_digest_pinned_oci_ref() -> None:
    chart = ChartConfig.model_validate({
        "name": "loki",
        "ref": "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST,
    })

    assert chart.values == []
    assert chart.namespace is None


def test_chart_config_rejects_non_oci_ref() -> None:
    with pytest.raises(ValidationError, match="upstream-native OCI"):
        ChartConfig.model_validate({"name": "loki", "ref": "ghcr.io/grafana/helm-charts/loki@" + DIGEST})


def test_chart_config_rejects_tagged_ref_without_digest() -> None:
    with pytest.raises(ValidationError, match="digest-pinned"):
        ChartConfig.model_validate({"name": "loki", "ref": "oci://ghcr.io/grafana/helm-charts/loki:6.0.0"})


def test_score_config_requires_helm_version_with_charts() -> None:
    with pytest.raises(ValidationError, match="helm_version"):
        ScoreConfig.model_validate({**CONFIG_BASE, "charts": [{"name": "loki", "ref": "oci://r/chart@" + DIGEST}]})

    config = ScoreConfig.model_validate({
        **CONFIG_BASE,
        "charts": [{"name": "loki", "ref": "oci://r/chart@" + DIGEST}],
        "helm_version": "3.22.1",
    })

    assert config.helm_version == "3.22.1"


def test_score_config_rejects_duplicate_chart_names() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ScoreConfig.model_validate({
            **CONFIG_BASE,
            "helm_version": "3.22.1",
            "charts": [
                {"name": "loki", "ref": "oci://r/one@" + DIGEST},
                {"name": "loki", "ref": "oci://r/two@" + DIGEST},
            ],
        })


def test_score_config_accepts_a_secrets_contract() -> None:
    config = ScoreConfig.model_validate({
        **CONFIG_BASE,
        "secrets": {"file": "stack/score/secrets.yaml", "k8s_secret": "placeframe-secrets"},
    })

    assert config.secrets is not None
    assert config.secrets.model_dump() == {
        "file": Path("stack/score/secrets.yaml"),
        "k8s_secret": "placeframe-secrets",
        "output": Path("stack/generated/secrets.txt"),
    }


def test_secrets_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        SecretsConfig.model_validate({
            "file": "stack/score/secrets.yaml",
            "k8s_secret": "placeframe-secrets",
            "rotation": "weekly",
        })


def test_secrets_config_requires_file_and_secret_name() -> None:
    with pytest.raises(ValidationError):
        SecretsConfig.model_validate({"file": "stack/score/secrets.yaml"})

    with pytest.raises(ValidationError):
        SecretsConfig.model_validate({"k8s_secret": "placeframe-secrets"})


def testtool_requirements_gate_helm_on_charts_and_target() -> None:
    with_charts = ScoreConfig.model_validate({
        **CONFIG_BASE,
        "helm_version": "3.22.1",
        "charts": [{"name": "loki", "ref": "oci://r/chart@" + DIGEST}],
    })

    assert [item.binary for item in tool_requirements(with_charts, "both")] == ["score-compose", "score-k8s", "helm"]
    assert [item.binary for item in tool_requirements(with_charts, "compose")] == ["score-compose"]

    without_charts = ScoreConfig.model_validate(CONFIG_BASE)

    assert [item.binary for item in tool_requirements(without_charts, "both")] == ["score-compose", "score-k8s"]


def testtool_requirements_carry_fetch_facts_for_every_tool() -> None:
    with_charts = ScoreConfig.model_validate({
        **CONFIG_BASE,
        "helm_version": "3.22.1",
        "charts": [{"name": "loki", "ref": "oci://r/chart@" + DIGEST}],
    })

    for item in tool_requirements(with_charts, "both"):
        assert item.version
        assert item.release.startswith("https://")
        assert item.archive.startswith("https://")
        assert item.extract

    helm = tool_requirements(with_charts, "both")[-1]

    assert helm.extract == "--strip-components=1 linux-amd64/helm"


def test_bind_volume_carries_required_source_and_target() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "bind", "source": "a", "target": "/b"}]})

    assert volume.volumes is not None
    assert isinstance(volume.volumes[0], BindVolume)
    assert volume.volumes[0].source == "a"
    assert volume.volumes[0].target == "/b"


def test_malformed_bind_fails_validation_instead_of_passing_through() -> None:
    with pytest.raises(ValidationError):
        ComposeService.model_validate({"volumes": [{"type": "bind", "target": "/b"}]})


def test_non_bind_volume_kind_passes_through_untyped() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "tmpfs", "target": "/cache"}]})

    assert volume.volumes is not None
    assert not isinstance(volume.volumes[0], BindVolume)


def test_write_normalises_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)

    write_generated_file(path, "content\n")

    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text(encoding="utf-8") == "content\n"


WORKLOAD_MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: workload\n"
HELM_RENDERED = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: chart\n"
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
CHART_REF = "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST
LOCK_IMAGES = {"GRAFANA_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/grafana/grafana:11.6.0@" + DIGEST}


class K8sBashRecorder:
    def __init__(self, output_path: Path, state_path: Path) -> None:
        self.output_path = output_path
        self.state_path = state_path
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: Path | None = None) -> None:
        self.commands.append(command)
        if command.startswith("score-k8s generate"):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(WORKLOAD_MANIFEST, encoding="utf-8")
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text("workloads: {}\n", encoding="utf-8")


class HelmRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.values_contents: list[str] = []

    def run(self, command: str) -> str:
        self.commands.append(command)
        for argument in command.split("--values ")[1:]:
            self.values_contents.append(Path(argument.split()[0]).read_text(encoding="utf-8"))
        return HELM_RENDERED


def _charts_config(values: list[str]) -> ScoreConfig:
    return ScoreConfig.model_validate({
        **CONFIG_BASE,
        "workloads": [],
        "helm_version": "3.22.1",
        "charts": [{"name": "loki", "ref": CHART_REF, "values": values}],
    })


def _generate_with_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ScoreConfig, images: dict[str, str]
) -> HelmRecorder:
    output = Path("stack/generated/k8s/manifests.yaml")
    k8s = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    helm = HelmRecorder()
    monkeypatch.setattr(score_generate, "bash", k8s.run)
    monkeypatch.setattr(score_generate, "bash_output", helm.run)
    generate_k8s(tmp_path, config, output, MIRROR_PREFIX, images)
    return helm


def test_values_without_markers_pass_through_as_the_committed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "values").mkdir()
    (tmp_path / "values" / "grafana.yaml").write_text("service:\n  port: 3000\n", encoding="utf-8")
    config = _charts_config(["values/grafana.yaml"])

    helm = _generate_with_chart(monkeypatch, tmp_path, config, {})

    assert helm.commands == [
        (
            f"helm template loki oci://{MIRROR_PREFIX}/ghcr.io/grafana/helm-charts/loki@{DIGEST} "
            f"--values {(tmp_path / 'values' / 'grafana.yaml').as_posix()}"
        )
    ]
    assert helm.values_contents == ["service:\n  port: 3000\n"]


def test_values_with_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources").mkdir(parents=True)
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").write_text(
        "apiVersion: 1\ndatasources:\n  - name: Loki\n    url: http://loki:3100\n",
        encoding="utf-8",
    )
    (tmp_path / "values").mkdir()
    (tmp_path / "values" / "grafana.yaml").write_text(
        "datasources:\n  datasources.yaml:\n    include-yaml: workloads/grafana/provisioning/datasources/datasources.yaml\n",
        encoding="utf-8",
    )
    config = _charts_config(["values/grafana.yaml"])

    helm = _generate_with_chart(monkeypatch, tmp_path, config, {})

    argument = helm.commands[0].split("--values ")[1].split()[0]
    assert argument != (tmp_path / "values" / "grafana.yaml").as_posix()
    assert argument.endswith("values/grafana.yaml")
    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["datasources"]["datasources.yaml"] == {
        "apiVersion": 1,
        "datasources": [{"name": "Loki", "url": "http://loki:3100"}],
    }


def test_text_markers_in_values_dump_as_literal_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "loki").mkdir(parents=True)
    (tmp_path / "workloads" / "loki" / "config.yaml").write_text(
        "storage:\n  s3:\n    access_key_id: ${S3_ACCESS_KEY}\n",
        encoding="utf-8",
    )
    (tmp_path / "values").mkdir()
    (tmp_path / "values" / "loki.yaml").write_text(
        "loki:\n  config:\n    include: workloads/loki/config.yaml\n",
        encoding="utf-8",
    )
    config = _charts_config(["values/loki.yaml"])

    helm = _generate_with_chart(monkeypatch, tmp_path, config, {})

    assert "config: |" in helm.values_contents[0]
    assert "${S3_ACCESS_KEY}" in helm.values_contents[0]


def test_values_with_lock_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "values").mkdir()
    (tmp_path / "values" / "grafana.yaml").write_text(
        "image:\n  lock-image:\n    key: GRAFANA_IMAGE\n    digest_field: sha\n",
        encoding="utf-8",
    )
    config = _charts_config(["values/grafana.yaml"])

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["registry"] == "ghcr.io/outernet-foundation/mirror/docker.io/grafana"
    assert resolved["image"]["repository"] == "grafana"
    assert resolved["image"]["tag"] == "11.6.0"
    assert resolved["image"]["sha"] == DIGEST.removeprefix("sha256:")


def test_values_lock_markers_resolve_after_includes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads").mkdir()
    (tmp_path / "workloads" / "images.yaml").write_text(
        "image:\n  lock-image: GRAFANA_IMAGE\n",
        encoding="utf-8",
    )
    (tmp_path / "values").mkdir()
    (tmp_path / "values" / "grafana.yaml").write_text(
        "include-yaml: workloads/images.yaml\n",
        encoding="utf-8",
    )
    config = _charts_config(["values/grafana.yaml"])

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["digest"] == DIGEST


def test_seam_requires_gpu_token_when_variants_declared() -> None:
    with pytest.raises(ValidationError, match=r"\{gpu\} token"):
        SeamConfig.model_validate({**SEAM_BASE, "gpu_variants": ["cuda", "rocm"], "output": "stack/compose.yaml"})


def test_seam_rejects_gpu_token_without_variants() -> None:
    with pytest.raises(ValidationError, match="nothing to substitute"):
        SeamConfig.model_validate(SEAM_BASE)

    with pytest.raises(ValidationError, match="nothing to substitute"):
        SeamConfig.model_validate({
            "name": "tunnel",
            "output": "stack/compose.yaml",
            "workloads": ["tunnel.yaml"],
            "patch_templates": ["compose/patches/gpu-{gpu}.tpl"],
        })


def test_seam_rejects_empty_and_duplicate_variant_names() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SeamConfig.model_validate({**SEAM_BASE, "gpu_variants": ["cuda", ""]})

    with pytest.raises(ValidationError, match="unique"):
        SeamConfig.model_validate({**SEAM_BASE, "gpu_variants": ["cuda", "cuda"]})


def test_seam_rejects_empty_workloads_and_unknown_override_targets() -> None:
    with pytest.raises(ValidationError, match="no workloads"):
        SeamConfig.model_validate({"name": "empty", "output": "stack/compose.yaml", "workloads": []})

    with pytest.raises(ValidationError, match="does not carry"):
        SeamConfig.model_validate({
            "name": "gateway-open",
            "output": "stack/compose.yaml",
            "workloads": ["gateway.yaml"],
            "overrides": {"api.yaml": "compose/overrides/open.yaml"},
        })


def test_score_config_rejects_duplicate_seam_names_and_colliding_outputs() -> None:
    with pytest.raises(ValidationError, match="seam names must be unique"):
        ScoreConfig.model_validate({
            **CONFIG_BASE,
            "seams": [
                {"name": "core", "output": "stack/a.yaml", "workloads": ["api.yaml"]},
                {"name": "core", "output": "stack/b.yaml", "workloads": ["api.yaml"]},
            ],
        })

    with pytest.raises(ValidationError, match="collide"):
        ScoreConfig.model_validate({
            **CONFIG_BASE,
            "seams": [
                {"name": "one", "output": "stack/same.{gpu}.yaml", "workloads": ["a.yaml"], "gpu_variants": ["cuda"]},
                {"name": "two", "output": "stack/same.{gpu}.yaml", "workloads": ["b.yaml"], "gpu_variants": ["cuda"]},
            ],
        })


def test_compose_seams_flatten_per_output_file() -> None:
    config = ScoreConfig.model_validate({
        **CONFIG_BASE,
        "k8s_gpu_variant": "cuda",
        "seams": [
            {
                "name": "core",
                "output": "stack/generated/compose/core.{gpu}.yaml",
                "workloads": ["api.yaml", "localizer.yaml"],
                "gpu_variants": ["cuda", "rocm"],
                "provisioners": ["compose/config.yaml"],
                "patch_templates": ["compose/patches/gpu-{gpu}.tpl"],
                "publishes": ["8000:api:8000"],
            },
            {
                "name": "gateway-open",
                "output": "stack/generated/compose/gateway-open.yaml",
                "workloads": ["gateway.yaml"],
                "overrides": {"gateway.yaml": "compose/overrides/gateway-open.yaml"},
            },
        ],
    })

    [core_cuda, core_rocm, gateway_open] = compose_seams(config)

    assert core_cuda.name == "core-cuda"
    assert core_cuda.variant == "cuda"
    assert core_cuda.output == Path("stack/generated/compose/core.cuda.yaml")
    assert core_cuda.patch_templates == [Path("compose/patches/gpu-cuda.tpl")]
    assert core_rocm.name == "core-rocm"
    assert core_rocm.output == Path("stack/generated/compose/core.rocm.yaml")
    assert core_rocm.patch_templates == [Path("compose/patches/gpu-rocm.tpl")]
    assert gateway_open.name == "gateway-open"
    assert gateway_open.variant is None
    assert gateway_open.output == Path("stack/generated/compose/gateway-open.yaml")
    assert gateway_open.overrides == {"gateway.yaml": "compose/overrides/gateway-open.yaml"}
    assert gateway_open.publishes == []


def testmerge_override_deletes_nulls_and_merges_mappings() -> None:
    workload = {
        "containers": {"gateway": {"image": "reg/gateway:1", "files": {"/a.conf": {"content": "x"}}}},
        "resources": {"auth": {"type": "oidc-issuer"}, "config": {"type": "placeframe-config"}},
    }

    merged = merge_override(
        workload, {"containers": {"gateway": {"files": None}}, "resources": {"auth": None}}, "gateway.yaml"
    )

    assert "files" not in merged["containers"]["gateway"]
    assert merged["containers"]["gateway"]["image"] == "reg/gateway:1"
    assert "auth" not in merged["resources"]
    assert merged["resources"]["config"] == {"type": "placeframe-config"}


def testmerge_override_replaces_scalars_and_lists() -> None:
    workload = {"containers": {"gateway": {"image": "reg/gateway:1", "command": ["a"]}}}

    merged = merge_override(
        workload, {"containers": {"gateway": {"image": "reg/gateway:2", "command": ["b", "c"]}}}, "gateway.yaml"
    )

    assert merged["containers"]["gateway"]["image"] == "reg/gateway:2"
    assert merged["containers"]["gateway"]["command"] == ["b", "c"]


def testmerge_override_rejects_non_mapping_shapes() -> None:
    with pytest.raises(SystemExit, match="mapping"):
        merge_override({"containers": {}}, ["not-a-map"], "gateway.yaml")

    with pytest.raises(SystemExit, match="mapping"):
        merge_override(None, {"containers": None}, "gateway.yaml")
