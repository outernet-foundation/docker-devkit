from pathlib import Path

import pytest
import yaml
from bashrun.bash import CalledProcessError
from typer.testing import CliRunner

from docker_devkit import compose

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64

LOCKED_ERROR = (
    f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {FAKE_DIGEST}) is not mirrored yet; "
    "the CI mirror job populates it when the declaring change runs"
)

COMPOSE_PREFIX = "docker compose -f compose.yml --project-directory . --env-file .env --env-file workloads/images.lock"

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _stub_invocation(root: Path, gpu: str, *, include_dev: bool, require_dev: bool) -> tuple[Path | None, str]:
    return None, COMPOSE_PREFIX


def _defined_profiles(compose_prefix: str) -> list[str]:
    return ["keycloak", "tunnel-ngrok"]


def _no_profiles(compose_prefix: str) -> list[str]:
    return []


def _rathole_profiles(compose_prefix: str) -> list[str]:
    return ["tunnel-rathole"]


def _probe_present(command: str) -> str:
    assert "imagetools inspect" in command
    return ""


def _probe_absent(command: str) -> str:
    assert "imagetools inspect" in command
    raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found\n")


def _probe_unauthorized(command: str) -> str:
    assert "imagetools inspect" in command
    raise CalledProcessError(1, command, stderr="unauthorized: authentication required\n")


class SelectiveProbe:
    def __init__(self, present: str) -> None:
        self.present = present
        self.probed: list[str] = []

    def run(self, command: str) -> str:
        reference = command.rpartition("inspect ")[2]
        self.probed.append(reference)
        if reference == self.present:
            return ""
        raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found")


class HandoffRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)


class TestProfileForwarding:
    def _invoke_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> HandoffRecorder:
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "_compose_invocation", _stub_invocation)
        monkeypatch.setattr(compose, "enumerate_profiles", _defined_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_forwards_profiles_declared_in_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} --profile tunnel-ngrok up --pull missing -d"]

    def test_shell_environment_overrides_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        monkeypatch.setenv("COMPOSE_PROFILES", "tunnel-rathole")
        recorder = self._invoke_up(monkeypatch, tmp_path)
        monkeypatch.setattr(compose, "enumerate_profiles", _rathole_profiles)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} --profile tunnel-rathole up --pull missing -d"]

    def test_splits_commas_strips_whitespace_and_deduplicates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text('COMPOSE_PROFILES=" keycloak , , tunnel-ngrok ,keycloak"\n', encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} --profile keycloak --profile tunnel-ngrok up --pull missing -d"]

    def test_absent_profiles_forward_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} up --pull missing -d"]

    def test_empty_value_forwards_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} up --pull missing -d"]

    def test_unknown_profile_fails_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=keycloak,ghost\n", encoding="utf-8")
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "ghost" in str(result.exception)


class TestLockRequirement:
    def _write_lock_free_tree(self, tmp_path: Path, sections: dict[str, object] | None = None) -> None:
        _write_config(tmp_path, sections or {"lifecycle": {"files": ["compose.yml"]}})
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        (tmp_path / "compose.yml").write_text("services:\n  api:\n    image: example:1\n", encoding="utf-8")

    def test_manifest_without_lock_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("services: {}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "No image lock" in str(result.exception)

    def test_lock_free_repo_assembles_without_the_lock_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "enumerate_profiles", _no_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.down_app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env down --remove-orphans"
        ]

    def test_present_lock_still_rides_the_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.lock").write_text("ALPINE_IMAGE=alpine:3.20\n", encoding="utf-8")
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "enumerate_profiles", _no_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.down_app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} "
                f"--env-file .env --env-file {tmp_path / 'workloads' / 'images.lock'} down --remove-orphans"
            )
        ]

    def test_mirror_section_without_lock_skips_the_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.up_app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env up --pull missing -d"
        ]


class TestMirroredLockEnforcement:
    def _write_mirrored_tree(self, tmp_path: Path, lock_text: str) -> None:
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.lock").write_text(lock_text, encoding="utf-8")

    def test_missing_entry_fails_closed_with_the_locked_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "_compose_invocation", _stub_invocation)
        monkeypatch.setattr(compose, "bash_output", _probe_absent)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert str(result.exception) == LOCKED_ERROR

    def test_probe_failure_is_not_reported_as_an_unmirrored_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "_compose_invocation", _stub_invocation)
        monkeypatch.setattr(compose, "bash_output", _probe_unauthorized)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "not mirrored yet" not in message
        assert "not evidence the image is missing" in message
        assert "authentication required" in message
        assert "alpine" in message

    def test_present_entries_proceed_to_bring_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "_compose_invocation", _stub_invocation)
        monkeypatch.setattr(compose, "bash_output", _probe_present)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [f"{COMPOSE_PREFIX} up --pull missing -d"]

    def test_reports_the_first_missing_entry_deterministically(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        present = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        absent = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"
        probe = SelectiveProbe(present)
        self._write_mirrored_tree(tmp_path, f"UV_IMAGE={present}\nALPINE_IMAGE={absent}\n")
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "_compose_invocation", _stub_invocation)
        monkeypatch.setattr(compose, "bash_output", probe.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert "alpine" in str(result.exception)
        assert probe.probed == [absent]
