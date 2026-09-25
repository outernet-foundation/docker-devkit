import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from docker_devkit import build_docker, mirror
from docker_devkit.documents import BakeDocument
from docker_devkit.image_refs import ImageReference
from docker_devkit.mirror import MirrorConfig

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64

MIRROR_REFERENCES = [
    f"{MIRROR_PREFIX}/docker.io/library/python:3.13-slim@{FAKE_DIGEST}",
    f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15-python3.13-trixie-slim",
    f"{MIRROR_PREFIX}/quay.io/keycloak/keycloak:26.3.5@{FAKE_DIGEST}",
    f"{MIRROR_PREFIX}/docker.io/livekit/livekit-server:v1.12.0@{FAKE_DIGEST}",
]

MANIFEST = (
    "x-base-images:\n"
    f"  UV_IMAGE: {MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15\n"
    "  API_IMAGE: ghcr.io/outernet-foundation/placeframe/api:1.2.3\n"
    "services:\n"
    "  api:\n"
    "    build:\n"
    "      dockerfile: workloads/api/Dockerfile\n"
    "      tags:\n"
    "        - localhost:5000/api:${API_SHA}\n"
)

LOCKED_ERROR = (
    f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {FAKE_DIGEST}) is not mirrored yet. "
    "If this is a new upstream dependency that has not been through CI, pass --allow-upstream-fallback "
    "to pull the same digest from upstream for this run."
)

runner = CliRunner()


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


def _must_not_be_probed(reference: str) -> bool:
    raise AssertionError(f"unexpected existence probe: {reference}")


def _no_service_shas(root: Path, bake: BakeDocument) -> dict[str, str]:
    return {}


def _no_gc_limits(min_gb: int = 60) -> None:
    return None


def _fixed_digest(reference: str) -> str:
    return FAKE_DIGEST


class TestInversion:
    @pytest.mark.parametrize("reference", MIRROR_REFERENCES)
    def test_round_trips_through_the_prefix(self, reference: str):
        assert mirror.is_mirrored(reference, MIRROR_PREFIX)
        assert f"{MIRROR_PREFIX}/{mirror.upstream_ref(reference, MIRROR_PREFIX)}" == reference

    @pytest.mark.parametrize("reference", MIRROR_REFERENCES)
    def test_preserves_tag_and_digest_tail(self, reference: str):
        assert mirror.upstream_ref(reference, MIRROR_PREFIX).rpartition("/")[2] == reference.rpartition("/")[2]

    def test_dockerio_path_maps_to_fully_qualified_upstream(self):
        reference = f"{MIRROR_PREFIX}/docker.io/livekit/livekit-server:v1.12.0@{FAKE_DIGEST}"
        assert (
            mirror.upstream_ref(reference, MIRROR_PREFIX) == f"docker.io/livekit/livekit-server:v1.12.0@{FAKE_DIGEST}"
        )

    def test_prefix_boundary_requires_slash(self):
        assert not mirror.is_mirrored(f"{MIRROR_PREFIX}-evil/docker.io/library/alpine:3.20", MIRROR_PREFIX)

    def test_publish_entries_are_not_mirrored(self):
        assert not mirror.is_mirrored("ghcr.io/outernet-foundation/placeframe/api:1.2.3", MIRROR_PREFIX)


class TestLoadMirrorConfig:
    def test_loads_declared_prefix(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            f"[tool.docker-devkit.mirror]\nprefix = '{MIRROR_PREFIX}'\n", encoding="utf-8"
        )

        assert mirror.load_mirror_config(tmp_path) == MirrorConfig(prefix=MIRROR_PREFIX)

    def test_absent_table_returns_none(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.docker-devkit.lifecycle]\nfiles = []\n", encoding="utf-8")

        assert mirror.load_mirror_config(tmp_path) is None

    def test_missing_pyproject_returns_none(self, tmp_path: Path):
        assert mirror.load_mirror_config(tmp_path) is None

    def test_rejects_unknown_keys(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.docker-devkit.mirror]\nprefix = 'ghcr.io/x/mirror'\nbogus = true\n", encoding="utf-8"
        )

        with pytest.raises(ValidationError):
            mirror.load_mirror_config(tmp_path)

    def test_rejects_trailing_slash_prefix(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.docker-devkit.mirror]\nprefix = 'ghcr.io/x/mirror/'\n", encoding="utf-8"
        )

        with pytest.raises(ValidationError):
            mirror.load_mirror_config(tmp_path)


class TestMirrorTargets:
    def test_filters_declared_references_by_prefix_and_sorts(self, tmp_path: Path):
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

        assert mirror.mirror_targets(tmp_path, MIRROR_PREFIX) == [
            ImageReference("UV_IMAGE", f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15")
        ]


class TestReferenceExists:
    def test_probes_with_imagetools_inspect(self, monkeypatch: pytest.MonkeyPatch):
        recorder = BashCheckRecorder(exists=True)
        monkeypatch.setattr(mirror, "bash_check", recorder.check)

        assert mirror.reference_exists("docker.io/library/alpine:3.20")
        assert recorder.commands == ["docker buildx imagetools inspect docker.io/library/alpine:3.20"]

    def test_returns_false_for_failed_probe(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "bash_check", _bash_check_false)

        assert not mirror.reference_exists("docker.io/library/alpine:3.20")


class TestUpstreamFallback:
    def test_fails_closed_with_the_locked_message(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "reference_exists", _bash_check_false)
        lock_data = {"ALPINE_IMAGE": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"}

        with pytest.raises(RuntimeError) as excinfo:
            mirror.upstream_fallback(lock_data, MIRROR_PREFIX, allow_upstream_fallback=False)

        assert str(excinfo.value) == LOCKED_ERROR

    def test_flag_substitutes_exactly_the_missing_entries(self, monkeypatch: pytest.MonkeyPatch):
        present = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        absent = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"
        existence = SelectiveExistence(present)
        monkeypatch.setattr(mirror, "reference_exists", existence.exists)
        lock_data = {
            "UV_IMAGE": present,
            "ALPINE_IMAGE": absent,
            "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        }

        substitutions = mirror.upstream_fallback(lock_data, MIRROR_PREFIX, allow_upstream_fallback=True)

        assert substitutions == {"ALPINE_IMAGE": f"docker.io/library/alpine:3.20@{FAKE_DIGEST}"}
        assert existence.probed == [present, absent]

    def test_returns_empty_when_everything_is_mirrored(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "reference_exists", _always_true)
        lock_data = {"ALPINE_IMAGE": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"}

        assert mirror.upstream_fallback(lock_data, MIRROR_PREFIX, allow_upstream_fallback=False) == {}

    def test_non_mirror_entries_are_never_probed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mirror, "reference_exists", _must_not_be_probed)
        lock_data = {"API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3"}

        assert mirror.upstream_fallback(lock_data, MIRROR_PREFIX, allow_upstream_fallback=True) == {}


class TestWriteFallbackOverlay:
    def test_writes_sorted_entries_under_a_pid_named_tempfile(self):
        substitutions = {
            "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
            "ALPINE_IMAGE": f"docker.io/library/alpine:3.20@{FAKE_DIGEST}",
        }

        overlay = mirror.write_fallback_overlay(substitutions)

        assert overlay.parent == Path(tempfile.gettempdir())
        assert overlay.name == f"docker-devkit-upstream-fallback-{os.getpid()}.env"
        assert overlay.read_text(encoding="utf-8") == (
            f"ALPINE_IMAGE=docker.io/library/alpine:3.20@{FAKE_DIGEST}\nUV_IMAGE=ghcr.io/astral-sh/uv:0.12.15\n"
        )


class TestMirrorVerb:
    def _write_tree(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            f"[tool.docker-devkit.mirror]\nprefix = '{MIRROR_PREFIX}'\n", encoding="utf-8"
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def test_copies_each_mirror_target_from_upstream(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = CommandRecorder()
        monkeypatch.setattr(mirror, "bash", recorder.run)
        monkeypatch.chdir(tmp_path)
        self._write_tree(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code == 0
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy ghcr.io/astral-sh/uv:0.12.15 {MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        ]
        assert "Mirrored images: 1" in result.stdout

    def test_errors_without_mirror_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(mirror.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)


class TestUpstreamAwareLockResolution:
    def _write_tree(self, tmp_path: Path, *, mirror_table: bool) -> None:
        table = f"[tool.docker-devkit.mirror]\nprefix = '{MIRROR_PREFIX}'\n" if mirror_table else ""
        (tmp_path / "pyproject.toml").write_text(f"[project]\nname = 'stack'\n\n{table}", encoding="utf-8")
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def test_resolves_mirror_declarations_against_upstream(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror_table=True)
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
        assert f"API_IMAGE=ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}" in lock
        assert f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}" in lock

    def test_without_table_inspects_the_declared_reference(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror_table=False)
        monkeypatch.chdir(tmp_path)
        recorder = DigestRecorder()
        monkeypatch.setattr(build_docker, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)

        build_docker.run_build(lock_only=True)

        assert recorder.inspected == [
            f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]


class TestBakeConsumesEnvOverlay:
    def _write_tree(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            f"[project]\nname = 'stack'\n\n[tool.docker-devkit.mirror]\nprefix = '{MIRROR_PREFIX}'\n",
            encoding="utf-8",
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text(MANIFEST, encoding="utf-8")

    def _fake_bake(self, monkeypatch: pytest.MonkeyPatch) -> BakeEnvProbe:
        probe = BakeEnvProbe()
        monkeypatch.setattr(build_docker, "bash", probe.run)
        monkeypatch.setattr(build_docker, "_check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_docker, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        return probe

    def test_overlay_wins_over_lock_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        probe = self._fake_bake(monkeypatch)
        upstream = f"ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"

        build_docker.run_build(gpu="none", env_overlay={"UV_IMAGE": upstream})

        assert probe.uv_image == upstream

    def test_without_overlay_bake_sees_mirror_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        probe = self._fake_bake(monkeypatch)

        build_docker.run_build(gpu="none")

        assert probe.uv_image == f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
