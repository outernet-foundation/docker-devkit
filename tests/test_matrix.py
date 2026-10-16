import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from docker_devkit.matrix import app

runner = CliRunner()


def service(
    dockerfile: str,
    *,
    tags: bool = True,
    contexts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    build: dict[str, Any] = {"dockerfile": dockerfile}
    if tags:
        build["tags"] = [f"registry/{dockerfile}:latest"]
    if contexts is not None:
        build["additional_contexts"] = contexts
    if platforms is not None:
        build["platforms"] = platforms
    return {"build": build}


TAGGED_BASES: dict[str, Any] = {
    "services": {
        "api": service("docker/api/Dockerfile"),
        "postgres": service("docker/postgres/Dockerfile"),
        "neural-networks-base-cuda": service("docker/neural-networks-base/Dockerfile"),
        "reconstructor-cuda": service(
            "docker/reconstructor/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
        "localizer-cuda": service(
            "docker/localizer/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
    }
}

TAGLESS_BASES: dict[str, Any] = {
    "services": {
        "api": service("docker/api/Dockerfile"),
        "neural-networks-base-cuda": service("docker/neural-networks-base/Dockerfile", tags=False),
        "reconstructor-cuda": service(
            "docker/reconstructor/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
    }
}


def _invoke_matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict[str, Any]) -> Any:
    (tmp_path / "workloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workloads" / "images.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, [])


def test_tagged_bases_group_with_their_dependents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, TAGGED_BASES)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api postgres", "variant": "common"},
        {"targets": "localizer-cuda neural-networks-base-cuda reconstructor-cuda", "variant": "cuda"},
    ]


def test_tagless_bases_ride_as_dependencies_not_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, TAGLESS_BASES)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api", "variant": "common"},
        {"targets": "reconstructor-cuda", "variant": "cuda"},
    ]


def test_single_service_repo_emits_matrix_of_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {"services": {"livekit-token": service("workloads/livekit-token/Dockerfile")}}

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "livekit-token", "variant": "common"}]


def test_cross_compile_targets_get_their_own_legs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile"),
            "postgres": service("docker/postgres/Dockerfile"),
            "zed-capture": service("docker/zed-capture/Dockerfile"),
        },
        "x-cross-compile-targets": ["zed-capture"],
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api postgres", "variant": "common"},
        {"targets": "zed-capture", "variant": "common"},
    ]


def test_platform_pinned_services_join_the_cross_compile_cohort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile"),
            "zed-capture": service("docker/zed-capture/Dockerfile", platforms=["linux/arm64"]),
            "aoa-bridge": service("docker/aoa-bridge/Dockerfile", platforms=["linux/arm64"]),
        }
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api", "variant": "common"},
        {"targets": "aoa-bridge zed-capture", "variant": "common"},
    ]


def test_unknown_cross_compile_target_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {"api": service("docker/api/Dockerfile")},
        "x-cross-compile-targets": ["missing"],
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "unknown services" in str(result.exception)


def test_group_spanning_cohort_membership_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile"),
            "zed-capture": service(
                "docker/zed-capture/Dockerfile", contexts={"api": "target:api"}, platforms=["linux/arm64"]
            ),
        }
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "spans cross-compile cohort" in str(result.exception)


def test_non_target_additional_contexts_do_not_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile", contexts={"shared": "docker/shared"}),
            "postgres": service("docker/postgres/Dockerfile"),
        }
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api postgres", "variant": "common"}]


def test_group_spanning_variants_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile"),
            "reconstructor-cuda": service("docker/reconstructor/Dockerfile", contexts={"api": "target:api"}),
        }
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "spans gpu variants" in str(result.exception)


def test_unknown_target_reference_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = {
        "services": {
            "api": service("docker/api/Dockerfile", contexts={"base": "target:missing-base"}),
        }
    }

    result = _invoke_matrix(monkeypatch, tmp_path, data)

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "unknown build target" in str(result.exception)
