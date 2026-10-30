import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from docker_devkit.matrix import app

runner = CliRunner()

FANNED = "ARG TORCH_DEVICE\nFROM docker.io/library/python:3.13-slim\n"
DEPENDENT = "FROM neural-networks-base\n"
EXTERNAL = "FROM docker.io/library/python:3.13-slim\n"


def _write_repo(root: Path, dockerfiles: dict[str, str], *, config: dict[str, Any] | None = None) -> None:
    for service, text in dockerfiles.items():
        directory = root / "workloads" / service
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").write_text(text, encoding="utf-8")
    payload: dict[str, Any] = {"requires": ">=0"}
    if dockerfiles:
        payload["repository"] = "ghcr.io/example"
    if config is not None:
        payload.update(config)
    (root / "docker-devkit.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _invoke_matrix(monkeypatch: Any, tmp_path: Path, dockerfiles: dict[str, str]) -> Any:
    _write_repo(tmp_path, dockerfiles)
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, [])


def test_bases_group_with_their_dependents_per_variant(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(
        monkeypatch,
        tmp_path,
        {
            "api": EXTERNAL,
            "postgres": EXTERNAL,
            "neural-networks-base": FANNED,
            "reconstructor": DEPENDENT,
            "localizer": DEPENDENT,
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api postgres", "variant": "common"},
        {"targets": "localizer-cuda neural-networks-base-cuda reconstructor-cuda", "variant": "cuda"},
        {"targets": "localizer-rocm neural-networks-base-rocm reconstructor-rocm", "variant": "rocm"},
    ]


def test_unfanned_dependents_group_with_their_base(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(
        monkeypatch,
        tmp_path,
        {
            "base": EXTERNAL,
            "app": "FROM base\n",
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "app base", "variant": "common"}]


def test_independent_services_share_the_common_leg(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, {"api": EXTERNAL, "postgres": EXTERNAL})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api postgres", "variant": "common"}]


def test_single_service_repo_emits_matrix_of_one(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, {"livekit-token": EXTERNAL})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "livekit-token", "variant": "common"}]


def test_repo_uniform_platforms_leave_the_legs_unchanged(monkeypatch: Any, tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {"api": EXTERNAL, "zed-capture": EXTERNAL},
        config={"platforms": ["linux/arm64"]},
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api zed-capture", "variant": "common"}]


def test_repo_without_config_emits_empty(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_config_without_services_emits_empty(monkeypatch: Any, tmp_path: Path) -> None:
    _write_repo(tmp_path, {}, config={"requires": ">=0.1"})
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
