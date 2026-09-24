from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit.score_generate import (
    ScoreConfig,
    expand_placeholders,
    normalise_state_paths,
    sort_documents,
    write_generated_file,
    load_score_config,
)


def make_config(**overrides: str | list[str]) -> ScoreConfig:
    values: dict[str, str | list[str]] = {
        "workloads": ["api.yaml", "gateway.yaml"],
        "project_name": "example",
        "cloud_storage_class": "cloud-volumes",
        "local_storage_class": "local-path",
    }
    values.update(overrides)
    return ScoreConfig.model_validate(values)


def test_expand_substitutes_uppercase_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_SHA", "tree-abc")
    expanded = expand_placeholders("image: registry/app:${API_SHA}", "api.yaml", Path("compose.bake.yml"))
    assert expanded == "image: registry/app:tree-abc"


def test_expand_leaves_score_resource_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_SHA", "tree-abc")
    text = "host: ${resources.db.host}"
    assert expand_placeholders(text, "api.yaml", Path("compose.bake.yml")) == text


def test_expand_exits_on_missing_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_SHA", raising=False)
    with pytest.raises(SystemExit, match="API_SHA"):
        expand_placeholders("image: registry/app:${API_SHA}", "api.yaml", Path("compose.bake.yml"))


def test_sort_documents_orders_by_kind_then_namespace(tmp_path: Path) -> None:
    path = tmp_path / "manifests.yaml"
    service_zeta = "apiVersion: v1\nkind: Service\nmetadata:\n  name: b\n  namespace: zeta\n"
    config_map = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: zeta\n"
    service_alpha = "apiVersion: v1\nkind: Service\nmetadata:\n  name: z\n  namespace: alpha\n"
    deployment = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: c\n  namespace: alpha\n"
    path.write_text(f"{service_zeta}---\n{config_map}---\n{service_alpha}---\n{deployment}", encoding="utf-8")

    sort_documents(path)

    assert path.read_text(encoding="utf-8") == (
        f"---\n{config_map}---\n{deployment}---\n{service_alpha}---\n{service_zeta}"
    )


def test_sort_documents_ignores_empty_blocks(tmp_path: Path) -> None:
    path = tmp_path / "manifests.yaml"
    document = "apiVersion: v1\nkind: Namespace\n"
    path.write_text(f"{document}---\n\n---\n", encoding="utf-8")

    sort_documents(path)

    assert path.read_text(encoding="utf-8") == f"---\n{document}"


def test_normalise_state_paths_reduces_rendered_paths_to_filenames(tmp_path: Path) -> None:
    state = tmp_path / "state.yaml"
    state.write_text(
        "workloads:\n- name: api\n  file: /tmp/tmp1234/api.yaml\n- name: gateway\n  file: /tmp/tmp1234/gateway.yaml\n",
        encoding="utf-8",
    )

    normalise_state_paths(make_config(), state)

    text = state.read_text(encoding="utf-8")
    assert "file: api.yaml" in text
    assert "file: gateway.yaml" in text
    assert "/tmp/tmp1234" not in text


def test_write_normalises_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)

    write_generated_file(path, "content\n")

    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text(encoding="utf-8") == "content\n"


def test_load_score_config_reads_consumer_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.docker-devkit.generate-score]\n"
        'workloads = ["api.yaml"]\n'
        'project_name = "placeframe"\n'
        'cloud_storage_class = "hcloud-volumes"\n'
        'local_storage_class = "local-path"\n'
        'publishes = ["8000:api:8000"]\n',
        encoding="utf-8",
    )

    config = load_score_config(tmp_path)

    assert config.workloads == ["api.yaml"]
    assert config.project_name == "placeframe"
    assert config.publishes == ["8000:api:8000"]
    assert config.score_dir == Path("stack/score")
    assert config.bake_file == Path("workloads/images.yml")
    assert config.compose_output == Path("stack/generated/compose/compose.yaml")
    assert config.k8s_output == Path("stack/generated/k8s/manifests.yaml")
    assert config.k8s_state == Path("stack/generated/k8s/.score-k8s")
    assert config.score_k8s_version == "0.15.0"
    assert config.score_compose_version == "0.42.0"


def test_load_score_config_missing_table_raises(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "consumer"\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="generate-score"):
        load_score_config(tmp_path)


def test_score_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ScoreConfig.model_validate({
            "workloads": ["api.yaml"],
            "project_name": "example",
            "cloud_storage_class": "cloud-volumes",
            "local_storage_class": "local-path",
            "publication": ["8000:api:8000"],
        })
