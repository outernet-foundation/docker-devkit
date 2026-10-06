import json
from pathlib import Path
from typing import Any

import pytest
import typer

from docker_devkit import build_docker
from docker_devkit.build_docker import DIGEST_FILE_NAME, distill_digest_manifest, push_image_digests

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REPO_ZED = "ghcr.io/outernet-foundation/placeframe-capture-tool/zed-capture"
REPO_AOA = "ghcr.io/outernet-foundation/placeframe-capture-tool/aoa-bridge"


def _baked(target: str, *, refs: str, digest: str) -> dict[str, dict[str, str]]:
    return {target: {"image.name": refs, "containerimage.digest": digest}}


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


def test_distills_single_target_single_tag() -> None:
    baked = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A)
    assert distill_digest_manifest(baked, ["zed-capture"]) == {
        "zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc"]}
    }


def test_distills_multiple_tags_from_comma_separated_refs() -> None:
    baked = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc,{REPO_ZED}:run-42", digest=DIGEST_A)
    assert distill_digest_manifest(baked, ["zed-capture"]) == {
        "zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc", "run-42"]}
    }


def test_distills_multiple_targets() -> None:
    baked = {
        "zed-capture": {"image.name": f"{REPO_ZED}:tree-abc", "containerimage.digest": DIGEST_A},
        "aoa-bridge": {"image.name": f"{REPO_AOA}:tree-def", "containerimage.digest": DIGEST_B},
    }
    assert distill_digest_manifest(baked, ["zed-capture", "aoa-bridge"]) == {
        "zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc"]},
        "aoa-bridge": {"ref": REPO_AOA, "digest": DIGEST_B, "tags": ["tree-def"]},
    }


def test_strips_digest_suffix_from_refs() -> None:
    baked = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc@{DIGEST_A}", digest=DIGEST_A)
    entry = distill_digest_manifest(baked, ["zed-capture"])["zed-capture"]
    assert entry["ref"] == REPO_ZED
    assert entry["tags"] == ["tree-abc"]


def test_push_image_digests_is_noop_without_builds_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = PushBuildRecorder()
    monkeypatch.setattr(build_docker, "push_build", recorder)
    baked = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A)
    push_image_digests(None, 42, baked, ["zed-capture"])
    assert recorder.calls == []


def test_push_image_digests_rejects_zero_run_number() -> None:
    with pytest.raises(typer.BadParameter):
        push_image_digests("ghcr.io/owner/builds", 0, {}, [])


def test_push_image_digests_pushes_manifest_to_images_digests_all(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = PushBuildRecorder()
    monkeypatch.setattr(build_docker, "push_build", recorder)
    baked = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc,{REPO_ZED}:run-42", digest=DIGEST_A)
    push_image_digests("ghcr.io/owner/builds", 42, baked, ["zed-capture"])
    assert len(recorder.calls) == 1
    registry, project, platform, tag, _source_directory, paths = recorder.calls[0]
    assert registry == "ghcr.io/owner/builds"
    assert project == "images-digests"
    assert platform == "all"
    assert tag == "run-42"
    assert paths == [DIGEST_FILE_NAME]
    assert recorder.manifests == [
        {"zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc", "run-42"]}}
    ]
