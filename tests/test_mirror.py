from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner, Result

from docker_devkit import mirror
from docker_devkit.mirror import MirrorConfig

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64

MANIFEST = (
    "x-base-images:\n"
    "  UV_IMAGE: ghcr.io/astral-sh/uv:0.12.15\n"
    "  API_IMAGE: ghcr.io/outernet-foundation/placeframe/api:1.2.3\n"
    "services:\n"
    "  api:\n"
    "    build:\n"
    "      dockerfile: workloads/api/Dockerfile\n"
    "      tags:\n"
    "        - localhost:5000/api:${API_SHA}\n"
)

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


class CommandRecorder:
    def __init__(self, present_refs: set[str] | None = None) -> None:
        self.commands: list[str] = []
        self.present_refs = present_refs or set()

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("curl"):
            Path("go-containerregistry_Linux_x86_64.tar.gz").write_text("", encoding="utf-8")

    def check(self, command: str) -> bool:
        self.commands.append(command)
        return command.removeprefix("crane digest ") in self.present_refs


class TestLoadMirrorConfig:
    def test_loads_declared_prefix(self, tmp_path: Path):
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})

        assert mirror.load_mirror_config(tmp_path) == MirrorConfig(prefix=MIRROR_PREFIX)

    def test_absent_section_returns_none(self, tmp_path: Path):
        _write_config(tmp_path, {"lifecycle": {"files": ["compose.yml"]}})

        assert mirror.load_mirror_config(tmp_path) is None

    def test_missing_config_returns_none(self, tmp_path: Path):
        assert mirror.load_mirror_config(tmp_path) is None

    def test_rejects_unknown_keys(self, tmp_path: Path):
        _write_config(tmp_path, {"mirror": {"prefix": "ghcr.io/x/mirror", "bogus": "true"}})

        with pytest.raises(ValidationError):
            mirror.load_mirror_config(tmp_path)

    def test_rejects_trailing_slash_prefix(self, tmp_path: Path):
        _write_config(tmp_path, {"mirror": {"prefix": "ghcr.io/x/mirror/"}})

        with pytest.raises(ValidationError):
            mirror.load_mirror_config(tmp_path)


class TestMirroredRef:
    def test_prefixes_upstream_native_references(self):
        assert mirror.mirrored_ref("ghcr.io/astral-sh/uv:0.12.15", MIRROR_PREFIX) == (
            f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        )

    def test_rejects_already_prefixed_references(self):
        with pytest.raises(RuntimeError, match="upstream-native"):
            mirror.mirrored_ref(f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15", MIRROR_PREFIX)


class TestMirrorVerb:
    def _write_tree(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def _invoke(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: CommandRecorder) -> Result:
        monkeypatch.setattr(mirror, "bash", recorder.run)
        monkeypatch.setattr(mirror, "bash_check", recorder.check)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(mirror.app, [])

    def test_copies_every_declared_reference(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = CommandRecorder()
        self._write_tree(tmp_path)

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        upstream_uv = "ghcr.io/astral-sh/uv:0.12.15"
        upstream_api = "ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_uv} {MIRROR_PREFIX}/{upstream_uv}",
            f"crane copy {upstream_api} {MIRROR_PREFIX}/{upstream_api}",
        ]
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_skips_references_the_lock_pins_as_mirrored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        mirrored_uv = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        (tmp_path / "workloads" / "images.lock").write_text(f"UV_IMAGE={mirrored_uv}\n", encoding="utf-8")
        recorder = CommandRecorder(present_refs={mirrored_uv})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        upstream_api = "ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_api} {MIRROR_PREFIX}/{upstream_api}",
        ]
        assert "Mirrored references: 1 copied, 1 already present" in result.stdout

    def test_presence_probe_failure_falls_through_to_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        mirrored_uv = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        (tmp_path / "workloads" / "images.lock").write_text(f"UV_IMAGE={mirrored_uv}\n", encoding="utf-8")
        recorder = CommandRecorder()

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane digest {mirrored_uv}" in recorder.commands
        assert len([command for command in recorder.commands if command.startswith("crane copy")]) == 2
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_copies_declared_charts_beside_images(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upstream_chart = f"ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"
        recorder = CommandRecorder()
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {
                "mirror": {"prefix": MIRROR_PREFIX},
                "generate-score": {
                    "workloads": ["api.yaml"],
                    "project_name": "example",
                    "cloud_storage_class": "cloud-volumes",
                    "local_storage_class": "local-path",
                    "helm_version": "3.22.1",
                    "charts": [{"name": "loki", "ref": f"oci://{upstream_chart}"}],
                },
            },
        )

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane copy {upstream_chart} {MIRROR_PREFIX}/{upstream_chart}" in recorder.commands
        assert "Mirrored references: 1 copied, 0 already present" in result.stdout

    def test_skips_charts_the_mirror_already_holds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upstream_chart = f"ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"
        mirrored_chart = f"{MIRROR_PREFIX}/{upstream_chart}"
        recorder = CommandRecorder(present_refs={mirrored_chart})
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {
                "mirror": {"prefix": MIRROR_PREFIX},
                "generate-score": {
                    "workloads": ["api.yaml"],
                    "project_name": "example",
                    "cloud_storage_class": "cloud-volumes",
                    "local_storage_class": "local-path",
                    "helm_version": "3.22.1",
                    "charts": [{"name": "loki", "ref": f"oci://{upstream_chart}"}],
                },
            },
        )

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane digest {mirrored_chart}" in recorder.commands
        assert not [command for command in recorder.commands if command.startswith("crane copy")]
        assert "Mirrored references: 0 copied, 1 already present" in result.stdout

    def test_errors_without_mirror_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)

    def test_rejects_unpinned_declarations_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("x-base-images:\n  ALPINE_IMAGE: alpine\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValidationError)
