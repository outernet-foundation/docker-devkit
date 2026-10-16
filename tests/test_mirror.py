import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from docker_devkit import build_docker, mirror
from docker_devkit.documents import BakeDocument
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

LOCKED_ERROR = (
    f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {FAKE_DIGEST}) is not mirrored yet; "
    "the CI mirror job populates it when the declaring change runs"
)

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


class SelectiveExistence:
    def __init__(self, present: str) -> None:
        self.present = present
        self.probed: list[str] = []

    def exists(self, reference: str) -> bool:
        self.probed.append(reference)
        return reference == self.present


class BashCheckRecorder:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.commands: list[str] = []

    def check(self, command: str) -> bool:
        self.commands.append(command)
        return self.exists


class CommandRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("curl"):
            Path("go-containerregistry_Linux_x86_64.tar.gz").write_text("", encoding="utf-8")


class DigestRecorder:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def resolve(self, reference: str) -> str:
        self.inspected.append(reference)
        return FAKE_DIGEST


class BakeEnvProbe:
    def __init__(self) -> None:
        self.uv_image: str | None = None

    def run(self, command: str) -> None:
        self.uv_image = os.environ.get("UV_IMAGE")
        Path("metadata.json").write_text('{"api": {}}', encoding="utf-8")


def _always_true(reference: str) -> bool:
    return True


def _bash_check_false(command: str) -> bool:
    return False


def _no_service_shas(root: Path, bake: BakeDocument) -> dict[str, str]:
    return {}


def _no_gc_limits(min_gb: int = 60) -> None:
    return None


def _fixed_digest(reference: str) -> str:
    return FAKE_DIGEST


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


class TestReferenceExists:
    def test_probes_with_imagetools_inspect(self, monkeypatch: pytest.MonkeyPatch):
        recorder = BashCheckRecorder(exists=True)
        monkeypatch.setattr(mirror, "bash_check", recorder.check)

        assert mirror.reference_exists("docker.io/library/alpine:3.20")
        assert recorder.commands == ["docker buildx imagetools inspect docker.io/library/alpine:3.20"]

    def test_returns_false_for_failed_probe(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "bash_check", _bash_check_false)

        assert not mirror.reference_exists("docker.io/library/alpine:3.20")


class TestEnforceMirrored:
    def test_fails_closed_with_the_locked_message(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "reference_exists", _bash_check_false)
        lock_data = {"ALPINE_IMAGE": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"}

        with pytest.raises(RuntimeError) as excinfo:
            mirror.enforce_mirrored(lock_data)

        assert str(excinfo.value) == LOCKED_ERROR

    def test_passes_when_every_entry_exists(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "reference_exists", _always_true)
        lock_data = {"ALPINE_IMAGE": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"}

        mirror.enforce_mirrored(lock_data)

    def test_reports_the_first_missing_entry_deterministically(self, monkeypatch: pytest.MonkeyPatch):
        present = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        absent = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"
        existence = SelectiveExistence(present)
        monkeypatch.setattr(mirror, "reference_exists", existence.exists)
        lock_data = {"UV_IMAGE": present, "ALPINE_IMAGE": absent}

        with pytest.raises(RuntimeError, match="alpine"):
            mirror.enforce_mirrored(lock_data)

        assert existence.probed == [absent]


class TestMirrorVerb:
    def _write_tree(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def test_copies_every_declared_reference(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = CommandRecorder()
        monkeypatch.setattr(mirror, "bash", recorder.run)
        monkeypatch.chdir(tmp_path)
        self._write_tree(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code == 0
        upstream_uv = "ghcr.io/astral-sh/uv:0.12.15"
        upstream_api = "ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_uv} {MIRROR_PREFIX}/{upstream_uv}",
            f"crane copy {upstream_api} {MIRROR_PREFIX}/{upstream_api}",
        ]
        assert "Mirrored references: 2" in result.stdout

    def test_copies_declared_charts_beside_images(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = CommandRecorder()
        monkeypatch.setattr(mirror, "bash", recorder.run)
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
                    "charts": [{"name": "loki", "ref": f"oci://ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"}],
                },
            },
        )

        result = runner.invoke(mirror.app, [])

        assert result.exit_code == 0
        upstream_chart = f"ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"
        assert f"crane copy {upstream_chart} {MIRROR_PREFIX}/{upstream_chart}" in recorder.commands
        assert "Mirrored references: 1" in result.stdout

    def test_errors_without_mirror_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)


class TestUpstreamAwareLockResolution:
    def _write_tree(self, tmp_path: Path, *, mirror: bool, manifest: str = MANIFEST) -> None:
        if mirror:
            _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(manifest, encoding="utf-8")

    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=True)
        monkeypatch.chdir(tmp_path)
        recorder = DigestRecorder()
        monkeypatch.setattr(build_docker, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)

        build_docker.run_build(lock_only=True)

        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]
        lock = (tmp_path / "workloads" / "images.lock").read_text(encoding="utf-8")
        assert f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}" in lock
        assert f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}" in lock

    def test_requires_a_mirror_section_for_base_images(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)

        with pytest.raises(RuntimeError, match="mirror section"):
            build_docker.run_build(lock_only=True)

    def test_digest_pinned_declarations_pass_through_prefixed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifest = f"x-base-images:\n  ALPINE_IMAGE: docker.io/library/alpine@{FAKE_DIGEST}\n"
        self._write_tree(tmp_path, mirror=True, manifest=manifest)
        monkeypatch.chdir(tmp_path)
        recorder = DigestRecorder()
        monkeypatch.setattr(build_docker, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)

        build_docker.run_build(lock_only=True)

        assert recorder.inspected == []
        lock = (tmp_path / "workloads" / "images.lock").read_text(encoding="utf-8")
        assert f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine@{FAKE_DIGEST}" in lock

    def test_cached_entries_resolve_again_only_on_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=True)
        (tmp_path / "workloads" / "images.lock").write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}\n"
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        recorder = DigestRecorder()
        monkeypatch.setattr(build_docker, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)

        build_docker.run_build(lock_only=True)

        assert recorder.inspected == []


class TestBakeSeesLockValues:
    def _write_tree(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def _fake_bake(self, monkeypatch: pytest.MonkeyPatch) -> BakeEnvProbe:
        probe = BakeEnvProbe()
        monkeypatch.setattr(build_docker, "bash", probe.run)
        monkeypatch.setattr(build_docker, "_check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_docker, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        return probe

    def test_bake_sees_mirror_prefixed_lock_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        probe = self._fake_bake(monkeypatch)

        build_docker.run_build(gpu="none")

        assert probe.uv_image == f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
