from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from docker_devkit.lifecycle import (
    LifecycleConfig,
    enforce_bake_declaration,
    expand_compose_files,
    load_lifecycle_config,
    resolve_compose_profiles,
    resolve_manifest,
)


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


class TestLoadLifecycleConfig:
    def test_loads_declared_files_and_dev_file(self, tmp_path: Path):
        _write_config(
            tmp_path,
            {
                "lifecycle": {
                    "files": ["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"],
                    "dev_file": "compose.dev.yml",
                }
            },
        )

        config = load_lifecycle_config(tmp_path)

        assert config == LifecycleConfig(
            files=["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"],
            dev_file="compose.dev.yml",
        )

    def test_dev_file_defaults_to_none_when_only_files_declared(self, tmp_path: Path):
        _write_config(tmp_path, {"lifecycle": {"files": ["compose.yml"]}})

        config = load_lifecycle_config(tmp_path)

        assert config == LifecycleConfig(files=["compose.yml"])

    def test_files_defaults_to_empty_when_only_dev_file_declared(self, tmp_path: Path):
        _write_config(tmp_path, {"lifecycle": {"dev_file": "compose.dev.yml"}})

        config = load_lifecycle_config(tmp_path)

        assert config == LifecycleConfig(dev_file="compose.dev.yml")

    def test_absent_section_returns_none(self, tmp_path: Path):
        _write_config(tmp_path, {"mirror": {"prefix": "ghcr.io/x/mirror"}})

        assert load_lifecycle_config(tmp_path) is None

    def test_missing_config_returns_none(self, tmp_path: Path):
        assert load_lifecycle_config(tmp_path) is None

    def test_rejects_unknown_keys(self, tmp_path: Path):
        _write_config(tmp_path, {"lifecycle": {"files": ["compose.yml"], "bogus": "true"}})

        with pytest.raises(ValidationError):
            load_lifecycle_config(tmp_path)

    def test_rejects_scalar_files(self, tmp_path: Path):
        _write_config(tmp_path, {"lifecycle": {"files": "compose.yml"}})

        with pytest.raises(ValidationError):
            load_lifecycle_config(tmp_path)


class TestExpandComposeFiles:
    def test_expands_gpu_placeholder(self):
        config = LifecycleConfig(files=["compose.yml", "compose.{gpu}.yml"])

        assert expand_compose_files(config, "cuda") == [Path("compose.yml"), Path("compose.cuda.yml")]

    def test_drops_gpu_entry_when_none(self):
        config = LifecycleConfig(files=["compose.yml", "compose.{gpu}.yml"])

        assert expand_compose_files(config, "none") == [Path("compose.yml")]

    def test_keeps_concrete_files_when_none(self):
        config = LifecycleConfig(files=["compose.yml", "compose.cpu.yml"])

        assert expand_compose_files(config, "none") == [Path("compose.yml"), Path("compose.cpu.yml")]

    def test_appends_dev_file_when_include_dev(self):
        config = LifecycleConfig(files=["compose.yml"], dev_file="compose.dev.yml")

        assert expand_compose_files(config, "cuda", include_dev=True) == [
            Path("compose.yml"),
            Path("compose.dev.yml"),
        ]

    def test_omits_dev_file_by_default(self):
        config = LifecycleConfig(files=["compose.yml"], dev_file="compose.dev.yml")

        assert expand_compose_files(config, "cuda") == [Path("compose.yml")]

    def test_empty_files_stay_empty_without_dev(self):
        assert expand_compose_files(LifecycleConfig(), "cuda") == []

    def test_no_table_defaults_to_single_compose_file(self):
        assert expand_compose_files(None, "cuda") == [Path("compose.yml")]
        assert expand_compose_files(None, "cuda", include_dev=True) == [Path("compose.yml")]


class TestResolveManifest:
    def test_resolves_images_manifest(self, tmp_path: Path):
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("", encoding="utf-8")

        assert resolve_manifest(tmp_path) == tmp_path / "workloads" / "images.yml"

    def test_returns_none_without_manifest(self, tmp_path: Path):
        assert resolve_manifest(tmp_path) is None


class TestEnforceBakeDeclaration:
    def test_accepts_images_manifest_with_declaration(self, tmp_path: Path):
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("", encoding="utf-8")

        enforce_bake_declaration(tmp_path, LifecycleConfig())

    def test_accepts_missing_manifest_without_declaration(self, tmp_path: Path):
        enforce_bake_declaration(tmp_path, None)

    def test_rejects_images_manifest_without_declaration(self, tmp_path: Path):
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError, match="lifecycle"):
            enforce_bake_declaration(tmp_path, None)


class TestResolveComposeProfiles:
    def test_reads_from_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")

        assert resolve_compose_profiles(env_file) == ["tunnel-ngrok"]

    def test_shell_environment_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env"
        env_file.write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        monkeypatch.setenv("COMPOSE_PROFILES", "tunnel-rathole")

        assert resolve_compose_profiles(env_file) == ["tunnel-rathole"]

    def test_splits_commas_strips_whitespace_and_drops_empties(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('COMPOSE_PROFILES=" keycloak , , tunnel-ngrok ,"\n', encoding="utf-8")

        assert resolve_compose_profiles(env_file) == ["keycloak", "tunnel-ngrok"]

    def test_deduplicates_preserving_first_occurrence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("COMPOSE_PROFILES=keycloak,tunnel-ngrok,keycloak\n", encoding="utf-8")

        assert resolve_compose_profiles(env_file) == ["keycloak", "tunnel-ngrok"]

    def test_absent_yields_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")

        assert resolve_compose_profiles(env_file) == []

    def test_empty_value_yields_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("COMPOSE_PROFILES=\n", encoding="utf-8")

        assert resolve_compose_profiles(env_file) == []
