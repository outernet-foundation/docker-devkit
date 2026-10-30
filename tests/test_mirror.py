from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner, Result

from docker_devkit import crane, mirror
from docker_devkit.config import LOCK_PATH
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
        _write_config(tmp_path, {"requires": ">=0.1"})

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
        _write_config(
            tmp_path,
            {
                "mirror": {"prefix": MIRROR_PREFIX},
                "base_images": {
                    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
                    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                },
            },
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}\n",
            encoding="utf-8",
        )

    def _invoke(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: CommandRecorder) -> Result:
        monkeypatch.setattr(mirror, "bash", recorder.run)
        monkeypatch.setattr(crane, "bash", recorder.run)
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
        (tmp_path / LOCK_PATH).write_text(f"UV_IMAGE={mirrored_uv}\n", encoding="utf-8")
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
        (tmp_path / LOCK_PATH).write_text(f"UV_IMAGE={mirrored_uv}\n", encoding="utf-8")
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
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
                "k8s": {"charts": {"loki": f"oci://{upstream_chart}"}},
            },
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}\n"
            f"LOKI_CHART={MIRROR_PREFIX}/{upstream_chart}\n",
            encoding="utf-8",
        )
        values = tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("loki: {}\n", encoding="utf-8")

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane copy ghcr.io/astral-sh/uv:0.12.15 {MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15" in (
            recorder.commands
        )
        assert f"crane copy {upstream_chart} {MIRROR_PREFIX}/{upstream_chart}" in recorder.commands
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_a_chart_without_its_lock_entry_refuses_naming_the_chart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {"k8s": {"charts": {"loki": f"oci://ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"}}},
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(
            f"OTHER_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.21.0@{FAKE_DIGEST}\n", encoding="utf-8"
        )
        values = tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("loki: {}\n", encoding="utf-8")

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "LOKI_CHART" in str(result.exception)

    def test_errors_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)

    def test_zero_declared_targets_is_a_loud_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"requires": ">=0.1"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "nothing is declared to mirror" in str(result.exception)

    def test_rejects_unpinned_declarations_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"base_images": {"ALPINE_IMAGE": "alpine"}})
        (tmp_path / "workloads").mkdir()
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValidationError)
