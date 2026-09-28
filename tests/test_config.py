import importlib.metadata
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit.config import DEV_SENTINEL, load_config


def write_config(tmp_path: Path, payload: dict[str, object]) -> None:
    (tmp_path / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def test_load_config_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert load_config(tmp_path) is None


def test_requires_omitted_is_required(tmp_path: Path) -> None:
    write_config(tmp_path, {"lifecycle": {"files": ["compose.yml"]}})

    with pytest.raises(ValidationError, match="requires"):
        load_config(tmp_path)


def test_requires_empty_string_rejected(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": ""})

    with pytest.raises(ValidationError, match="requires"):
        load_config(tmp_path)


def test_requires_invalid_specifier_rejected(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": "not-a-specifier"})

    with pytest.raises(ValidationError, match="requires"):
        load_config(tmp_path)


def _version_dev_sentinel(_name: str) -> str:
    return DEV_SENTINEL


def _version_0_1_18(_name: str) -> str:
    return "0.1.18"


def _version_0_1_19_dev5(_name: str) -> str:
    return "0.1.19.dev5"


def _version_0_1_17(_name: str) -> str:
    return "0.1.17"


def test_requires_dev_sentinel_skips_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _version_dev_sentinel)
    write_config(tmp_path, {"requires": ">=0.1.18"})

    config = load_config(tmp_path)
    assert config is not None

    assert config.requires == ">=0.1.18"


def test_requires_in_range_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _version_0_1_18)
    write_config(tmp_path, {"requires": ">=0.1.18"})

    config = load_config(tmp_path)
    assert config is not None

    assert config.requires == ">=0.1.18"


def test_requires_dev_channel_prerelease_satisfies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _version_0_1_19_dev5)
    write_config(tmp_path, {"requires": ">=0.1.18"})

    config = load_config(tmp_path)
    assert config is not None

    assert config.requires == ">=0.1.18"


def test_requires_out_of_range_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _version_0_1_17)
    write_config(tmp_path, {"requires": ">=0.1.18"})

    with pytest.raises(SystemExit, match="does not satisfy requires"):
        load_config(tmp_path)


def test_load_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    (tmp_path / "docker-devkit.yaml").write_text('requires: ">=0.1"\nrequires: ">=0.2"\n', encoding="utf-8")

    with pytest.raises(Exception, match=r"Duplicate key"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": ">=0.1", "lifecycle_prefix": "ghcr.io/x/mirror"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_config(tmp_path)


def test_load_config_loads_lifecycle_and_mirror(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "requires": ">=0.1",
            "lifecycle": {"files": ["compose.yml", "compose.{gpu}.yml"], "dev_file": "compose.dev.yml"},
            "mirror": {"prefix": "ghcr.io/outernet-foundation/mirror"},
        },
    )

    config = load_config(tmp_path)
    assert config is not None

    assert config.lifecycle is not None
    assert config.lifecycle.files == ["compose.yml", "compose.{gpu}.yml"]
    assert config.lifecycle.dev_file == "compose.dev.yml"
    assert config.mirror is not None
    assert config.mirror.prefix == "ghcr.io/outernet-foundation/mirror"
    assert config.generate_score is None


def test_load_config_loads_generate_score_section(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "requires": ">=0.1",
            "generate-score": {
                "workloads": ["api.yaml"],
                "project_name": "placeframe",
                "cloud_storage_class": "hcloud-volumes",
                "local_storage_class": "local-path",
            },
        },
    )

    config = load_config(tmp_path)
    assert config is not None

    assert config.generate_score is not None
    assert config.generate_score.workloads == ["api.yaml"]
    assert config.generate_score.project_name == "placeframe"
    assert config.lifecycle is None
