from pathlib import Path

import pytest
from pydantic import ValidationError

from stack_lifecycle import image_refs
from stack_lifecycle.image_refs import (
    ImageReference,
    bake_base_image_refs,
    collect_repo_references,
    compose_service_refs,
    resolve_remote_digest,
    strip_build_args,
)

FAKE_DIGEST = "sha256:" + "a" * 64


def _inspect_output_with_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return f"Name: x\nDigest: {FAKE_DIGEST}\n"


def _inspect_output_without_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return "Name: x\n"


class TestComposeServiceRefs:
    def test_should_yield_services_with_x_image_ref(self):
        document = {
            "services": {
                "minio": {"x-image-ref": "docker.io/minio/minio:latest", "environment": {"FOO": "BAR"}},
                "api": {"build": {"dockerfile": "docker/api/Dockerfile"}},
            }
        }
        assert compose_service_refs(document) == [ImageReference("minio", "docker.io/minio/minio:latest")]

    def test_should_return_empty_when_no_services(self):
        assert compose_service_refs({"volumes": {}}) == []
        assert compose_service_refs({}) == []

    def test_should_reject_malformed_service(self):
        with pytest.raises(ValidationError):
            compose_service_refs({"services": {"broken": "not-a-mapping"}})


class TestBakeBaseImageRefs:
    def test_should_yield_base_images(self):
        assert bake_base_image_refs({"x-base-images": {"ALPINE_DIGEST": "alpine:3.20"}, "services": {}}) == [
            ImageReference("ALPINE_DIGEST", "alpine:3.20")
        ]

    def test_should_return_empty_when_no_base_images(self):
        assert bake_base_image_refs({"services": {}}) == []


class TestCollectRepoReferences:
    def test_should_collect_all_source_kinds(self, tmp_path: Path):
        (tmp_path / "compose.yml").write_text(
            "services:\n  minio:\n    x-image-ref: docker.io/minio/minio:latest\n", encoding="utf-8"
        )
        (tmp_path / "compose.bake.yml").write_text("x-base-images:\n  ALPINE_DIGEST: alpine:3.20\n", encoding="utf-8")
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM --platform=linux/amd64 python:3.13-slim AS base\nRUN pip install x\n", encoding="utf-8"
        )
        score_directory = tmp_path / "score"
        score_directory.mkdir()
        (score_directory / "app.provisioners.yaml").write_text(
            "containers:\n  - name: keycloak\n    image: quay.io/keycloak/keycloak:26.3.5@sha256:abc\n",
            encoding="utf-8",
        )
        assert collect_repo_references(tmp_path) == [
            ImageReference("minio", "docker.io/minio/minio:latest"),
            ImageReference("ALPINE_DIGEST", "alpine:3.20"),
            ImageReference("", "python:3.13-slim"),
            ImageReference("", "quay.io/keycloak/keycloak:26.3.5@sha256:abc"),
        ]

    def test_should_return_empty_for_empty_tree(self, tmp_path: Path):
        assert collect_repo_references(tmp_path) == []


class TestStripBuildArgs:
    def test_should_remove_build_arg_suffixes(self):
        assert strip_build_args("grafana/alloy:v1.9.0${ALLOY_DIGEST}") == "grafana/alloy:v1.9.0"

    def test_should_return_empty_for_pure_build_arg(self):
        assert not strip_build_args("${BASE_IMAGE}")


class TestResolveRemoteDigest:
    def test_should_return_parsed_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_with_digest)
        assert resolve_remote_digest("docker.io/library/alpine:3.20") == FAKE_DIGEST

    def test_should_raise_when_digest_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_without_digest)
        with pytest.raises(RuntimeError):
            resolve_remote_digest("docker.io/library/alpine:3.20")
