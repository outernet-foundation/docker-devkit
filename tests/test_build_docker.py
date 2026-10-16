import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from docker_devkit import build_docker
from docker_devkit.build_docker import DIGEST_FILE_NAME

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
BUILD_SHA = "c" * 40
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
REPO_ZED = "ghcr.io/outernet-foundation/placeframe-capture-tool/zed-capture"
REPO_AOA = "ghcr.io/outernet-foundation/placeframe-capture-tool/aoa-bridge"

runner = CliRunner()

BAKE_DATA: dict[str, object] = {
    "services": {
        "api": {"build": {"dockerfile": "docker/api/Dockerfile", "tags": ["registry/api:${API_SHA}"]}},
        "postgres": {
            "build": {"dockerfile": "docker/postgres/Dockerfile", "tags": ["registry/postgres:${POSTGRES_SHA}"]}
        },
        "seaweedfs": {
            "build": {"dockerfile": "docker/seaweedfs/Dockerfile", "tags": ["registry/seaweedfs:${SEAWEEDFS_SHA}"]}
        },
        "neural-networks-base-cuda": {"build": {"dockerfile": "docker/neural-networks-base-cuda/Dockerfile"}},
        "neural-networks-base-rocm": {"build": {"dockerfile": "docker/neural-networks-base-rocm/Dockerfile"}},
        "localizer-cuda": {
            "build": {"dockerfile": "docker/localizer/Dockerfile", "tags": ["registry/localizer-cuda:${LOCALIZER_SHA}"]}
        },
        "reconstructor-cuda": {
            "build": {
                "dockerfile": "docker/reconstructor/Dockerfile",
                "tags": ["registry/reconstructor-cuda:${RECONSTRUCTOR_SHA}"],
            }
        },
        "localizer-rocm": {
            "build": {"dockerfile": "docker/localizer/Dockerfile", "tags": ["registry/localizer-rocm:${LOCALIZER_SHA}"]}
        },
        "reconstructor-rocm": {
            "build": {
                "dockerfile": "docker/reconstructor/Dockerfile",
                "tags": ["registry/reconstructor-rocm:${RECONSTRUCTOR_SHA}"],
            }
        },
        "zed-capture": {
            "build": {
                "dockerfile": "docker/zed-capture/Dockerfile",
                "tags": ["registry/zed-capture:${ZED_CAPTURE_SHA}"],
            }
        },
    },
    "x-cross-compile-targets": ["zed-capture"],
}

MIRROR_MANIFEST = (
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

PUSH_MANIFEST = (
    "x-registry-cache: ghcr.io/example/cache\n"
    "services:\n"
    "  zed-capture:\n"
    "    build:\n"
    "      dockerfile: docker/zed-capture/Dockerfile\n"
    "      tags:\n"
    "        - ghcr.io/example/zed-capture:latest\n"
    "  aoa-bridge:\n"
    "    build:\n"
    "      dockerfile: docker/aoa-bridge/Dockerfile\n"
    "      tags:\n"
    "        - ghcr.io/example/aoa-bridge:latest\n"
)


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _shard_key(targets: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(targets)).encode("utf-8")).hexdigest()[:8]


def _bake_targets(commands: list[str]) -> list[str]:
    command = next(command for command in commands if command.startswith("docker buildx bake"))
    words = command.split()
    stance = "--load" if "--load" in words else "--push"
    return words[words.index(stance) + 1 :]


def _baked(target: str, *, refs: str, digest: str) -> dict[str, dict[str, str]]:
    return {target: {"image.name": refs, "containerimage.digest": digest}}


def _no_service_shas(root: Path, bake: object) -> dict[str, str]:
    return {}


def _no_gc_limits(min_gb: int = 60) -> None:
    return None


def _fixed_digest(reference: str) -> str:
    return DIGEST_A


def _head_sha(command: str) -> str:
    return BUILD_SHA


def _noop_install_oras() -> None:
    return None


class DigestRecorder:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def resolve(self, reference: str) -> str:
        self.inspected.append(reference)
        return DIGEST_A


class BakeRunner:
    def __init__(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        self.entries = entries or {}
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("docker buildx bake"):
            targets = _bake_targets([command])
            entries = {target: self.entries.get(target, {}) for target in targets}
            Path("metadata.json").write_text(json.dumps(entries), encoding="utf-8")


class BakeEnvProbe:
    def __init__(self) -> None:
        self.uv_image: str | None = None

    def run(self, command: str) -> None:
        self.uv_image = os.environ.get("UV_IMAGE")
        Path("metadata.json").write_text('{"api": {}}', encoding="utf-8")


class PushBuildRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, Path, list[str]]] = []
        self.manifests: list[dict[str, Any]] = []

    def __call__(
        self,
        registry: str,
        project: str,
        platform: str,
        tag: str,
        source_directory: Path,
        paths: list[str],
    ) -> None:
        self.calls.append((registry, project, platform, tag, source_directory, paths))
        manifest_path = Path(source_directory) / paths[0]
        self.manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))


class TestDefaultTargets:
    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        data: dict[str, Any] = BAKE_DATA,
    ) -> list[str]:
        bake = BakeRunner()
        monkeypatch.setattr(build_docker, "bash", bake.run)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(build_docker, "_check_gc_limits", _no_gc_limits)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "workloads").mkdir(parents=True, exist_ok=True)
        (tmp_path / "workloads" / "images.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

        result = runner.invoke(build_docker.app, arguments)

        assert result.exit_code == 0, result.output
        return _bake_targets(bake.commands)

    def test_none_yields_common_partition(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "none"]) == ["api", "postgres", "seaweedfs"]

    def test_gpu_only_yields_just_that_gpus_services(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "cuda", "--gpu-only"]) == [
            "localizer-cuda",
            "reconstructor-cuda",
        ]
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "rocm", "--gpu-only"]) == [
            "localizer-rocm",
            "reconstructor-rocm",
        ]

    def test_default_with_gpu_yields_common_plus_that_gpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "cuda"]) == [
            "api",
            "postgres",
            "seaweedfs",
            "localizer-cuda",
            "reconstructor-cuda",
        ]

    def test_untagged_services_are_excluded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        for arguments in (
            ["--gpu", "none"],
            ["--gpu", "cuda"],
            ["--gpu", "cuda", "--gpu-only"],
            ["--gpu", "rocm", "--gpu-only"],
        ):
            targets = self._invoke(monkeypatch, tmp_path, arguments)
            assert "neural-networks-base-cuda" not in targets
            assert "neural-networks-base-rocm" not in targets

    def test_cross_compile_services_always_excluded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        for arguments in (["--gpu", "none"], ["--gpu", "cuda"], ["--gpu", "cuda", "--gpu-only"]):
            assert "zed-capture" not in self._invoke(monkeypatch, tmp_path, arguments)

    def test_missing_cross_compile_key_is_tolerated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        data = {
            "services": {"api": {"build": {"dockerfile": "docker/api/Dockerfile", "tags": ["registry/api:${API_SHA}"]}}}
        }

        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "none"], data) == ["api"]


class TestPushShelving:
    def _prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entries: dict[str, dict[str, str]]
    ) -> PushBuildRecorder:
        recorder = PushBuildRecorder()
        monkeypatch.setattr(build_docker, "bash", BakeRunner(entries).run)
        monkeypatch.setattr(build_docker, "bash_output", _head_sha)
        monkeypatch.setattr(build_docker, "push_build", recorder)
        monkeypatch.setattr(build_docker, "install_oras", _noop_install_oras)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(build_docker, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setenv("REGISTRY", "ghcr.io/owner")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "workloads").mkdir(parents=True, exist_ok=True)
        (tmp_path / "workloads" / "images.yml").write_text(PUSH_MANIFEST, encoding="utf-8")
        return recorder

    def test_shelves_the_distilled_manifest_under_images_digests_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc,{REPO_ZED}:latest", digest=DIGEST_A)
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build_docker.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture"])

        assert result.exit_code == 0, result.output
        assert len(recorder.calls) == 1
        registry, project, platform, tag, _source_directory, paths = recorder.calls[0]
        assert registry == "ghcr.io/owner/builds"
        assert project == "images-digests"
        assert platform == "all"
        assert tag == f"sha-{BUILD_SHA}-{_shard_key(['zed-capture'])}"
        assert paths == [DIGEST_FILE_NAME]
        assert recorder.manifests == [
            {"zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc", "latest"]}}
        ]

    def test_distills_multiple_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = {
            **_baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A),
            **_baked("aoa-bridge", refs=f"{REPO_AOA}:tree-def", digest=DIGEST_B),
        }
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(
            build_docker.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture", "--targets", "aoa-bridge"]
        )

        assert result.exit_code == 0, result.output
        assert recorder.manifests == [
            {
                "zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc"]},
                "aoa-bridge": {"ref": REPO_AOA, "digest": DIGEST_B, "tags": ["tree-def"]},
            }
        ]

    def test_strips_digest_suffix_from_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc@{DIGEST_A}", digest=DIGEST_A)
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build_docker.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture"])

        assert result.exit_code == 0, result.output
        entry = recorder.manifests[0]["zed-capture"]
        assert entry["ref"] == REPO_ZED
        assert entry["tags"] == ["tree-abc"]

    def test_refuses_target_with_no_tagged_references(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}@{DIGEST_A}", digest=DIGEST_A)
        self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build_docker.app, ["--push", "--gpu", "cuda"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)

    def test_shard_tag_is_order_insensitive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = {
            **_baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A),
            **_baked("aoa-bridge", refs=f"{REPO_AOA}:tree-def", digest=DIGEST_B),
        }
        first = self._prepare(monkeypatch, tmp_path, entries)

        result_first = runner.invoke(
            build_docker.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture aoa-bridge"]
        )

        assert result_first.exit_code == 0, result_first.output
        second = self._prepare(monkeypatch, tmp_path, entries)

        result_second = runner.invoke(
            build_docker.app, ["--push", "--gpu", "cuda", "--targets", "aoa-bridge", "--targets", "zed-capture"]
        )

        assert result_second.exit_code == 0, result_second.output
        expected = f"sha-{BUILD_SHA}-{_shard_key(['zed-capture', 'aoa-bridge'])}"
        assert first.calls[0][3] == expected
        assert second.calls[0][3] == expected


class TestLockRecording:
    def _write_tree(self, tmp_path: Path, *, mirror: bool, manifest: str = MIRROR_MANIFEST) -> None:
        if mirror:
            _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        else:
            _write_config(tmp_path)
        (tmp_path / "workloads").mkdir(parents=True, exist_ok=True)
        (tmp_path / "workloads" / "images.yml").write_text(manifest, encoding="utf-8")

    def _invoke_lock(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]) -> DigestRecorder:
        recorder = DigestRecorder()
        monkeypatch.setattr(build_docker, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build_docker.app, arguments)

        assert result.exit_code == 0, result.output
        return recorder

    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=True)

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])

        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]
        lock = (tmp_path / "workloads" / "images.lock").read_text(encoding="utf-8")
        assert f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}" in lock
        assert f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}" in lock

    def test_written_lock_carries_no_generated_header(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=True)

        self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])

        lock = (tmp_path / "workloads" / "images.lock").read_text(encoding="utf-8")
        assert lock.startswith("API_IMAGE=")

    def test_requires_a_mirror_section_for_base_images(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=False)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build_docker.app, ["--lock-only"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)

    def test_digest_pinned_declarations_pass_through_prefixed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifest = f"x-base-images:\n  ALPINE_IMAGE: docker.io/library/alpine@{DIGEST_A}\n"
        self._write_tree(tmp_path, mirror=True, manifest=manifest)

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])

        assert recorder.inspected == []
        lock = (tmp_path / "workloads" / "images.lock").read_text(encoding="utf-8")
        assert f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine@{DIGEST_A}" in lock

    def test_cached_entries_resolve_again_only_on_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path, mirror=True)
        (tmp_path / "workloads" / "images.lock").write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}\n"
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}\n",
            encoding="utf-8",
        )

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])
        assert recorder.inspected == []

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only", "--upgrade"])
        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]


class TestBakeSeesLockValues:
    def test_bake_sees_mirror_prefixed_lock_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"mirror": {"prefix": MIRROR_PREFIX}})
        (tmp_path / "workloads").mkdir(parents=True, exist_ok=True)
        (tmp_path / "workloads" / "images.yml").write_text(MIRROR_MANIFEST, encoding="utf-8")
        probe = BakeEnvProbe()
        monkeypatch.setattr(build_docker, "bash", probe.run)
        monkeypatch.setattr(build_docker, "_check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_docker, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setattr(build_docker, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build_docker.app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert probe.uv_image == f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"
