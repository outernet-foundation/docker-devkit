from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit.config import ChartConfig
from docker_devkit.score_generate import (
    BindVolume,
    ComposeService,
    ScoreConfig,
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
