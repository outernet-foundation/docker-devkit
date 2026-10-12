from pathlib import Path

import pytest

from docker_devkit.modes import resolve_compose_profiles


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
