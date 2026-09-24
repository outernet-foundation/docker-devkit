from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit import image_refs
from docker_devkit.documents import BakeDocument
from docker_devkit.image_refs import (
    ImageReference,
    VersionCoupling,
    VersionSite,
    bake_base_image_refs,
    declared_references,
    resolve_remote_digest,
    stray_references,
    strip_build_args,
    unpinned_references,
    version_coupling_violations,
)

FAKE_DIGEST = "sha256:" + "a" * 64

VERSION_COUPLINGS = [
    VersionCoupling(
        name="uv",
        pyproject_key="tool.uv.required-version",
        sites=(VersionSite("uv base tag", "compose*.bake.yml", r"uv:([^@]+?)-", "UV_BASE_IMAGE"),),
    ),
    VersionCoupling(
        name="python",
        pyproject_key="project.requires-python",
        sites=(
            VersionSite("uv base python component", "compose*.bake.yml", r"python([0-9][0-9.]*)", "UV_BASE_IMAGE"),
            VersionSite("python base tag", "compose.zed.bake.yml", r"python:([0-9][0-9.]*)", "PYTHON_BASE_IMAGE"),
            VersionSite("uv python install", "docker/zed-capture/Dockerfile", r"uv python install ([0-9][0-9.]*)"),
        ),
    ),
]


def _inspect_output_with_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return f"Name: x\nDigest: {FAKE_DIGEST}\n"


def _inspect_output_without_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return "Name: x\n"


class TestBakeBaseImageRefs:
    def test_should_yield_base_images(self):
        bake = BakeDocument.model_validate({"x-base-images": {"ALPINE_IMAGE": "alpine:3.20"}, "services": {}})
        assert bake_base_image_refs(bake) == [ImageReference("ALPINE_IMAGE", "alpine:3.20")]

    def test_should_return_empty_when_no_base_images(self):
        bake = BakeDocument.model_validate({"services": {}})
        assert bake_base_image_refs(bake) == []


class TestDeclaredReferences:
    def test_should_yield_bake_declarations(self, tmp_path: Path):
        (tmp_path / "compose.bake.yml").write_text("x-base-images:\n  ALPINE_IMAGE: alpine:3.20\n", encoding="utf-8")
        assert declared_references(tmp_path) == [ImageReference("ALPINE_IMAGE", "alpine:3.20")]

    def test_should_return_empty_for_empty_tree(self, tmp_path: Path):
        assert declared_references(tmp_path) == []

    def test_should_scan_only_bake_files(self, tmp_path: Path):
        (tmp_path / "compose.yml").write_text(
            "services:\n  minio:\n    x-image-ref: docker.io/minio/minio:latest\n", encoding="utf-8"
        )
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        assert declared_references(tmp_path) == []

    def test_should_reject_unpinned_declaration(self, tmp_path: Path):
        (tmp_path / "compose.bake.yml").write_text("x-base-images:\n  ALPINE_IMAGE: alpine\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            declared_references(tmp_path)


class TestStrayReferences:
    def test_should_collect_dockerfile_and_score_sources(self, tmp_path: Path):
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM --platform=linux/amd64 python:3.13-slim AS base\n"
            "COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/\n"
            "COPY --from=base /x /y\n"
            "RUN pip install x\n",
            encoding="utf-8",
        )
        score_directory = tmp_path / "score"
        score_directory.mkdir()
        (score_directory / "app.provisioners.yaml").write_text(
            "containers:\n  - name: keycloak\n    image: quay.io/keycloak/keycloak:26.3.5@sha256:abc\n",
            encoding="utf-8",
        )
        assert stray_references(tmp_path) == [
            ImageReference("", "python:3.13-slim"),
            ImageReference("", "ghcr.io/astral-sh/uv:latest"),
            ImageReference("", "quay.io/keycloak/keycloak:26.3.5@sha256:abc"),
        ]

    def test_should_return_empty_for_empty_tree(self, tmp_path: Path):
        assert stray_references(tmp_path) == []


class TestStripBuildArgs:
    def test_should_remove_build_arg_suffixes(self):
        assert strip_build_args("grafana/alloy:v1.9.0${ALLOY_DIGEST}") == "grafana/alloy:v1.9.0"

    def test_should_return_empty_for_pure_build_arg(self):
        assert not strip_build_args("${BASE_IMAGE}")


class TestUnpinnedReferences:
    def test_should_return_empty_when_everything_is_pinned(self, tmp_path: Path):
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv${UV_BASE_DIGEST}\n"
            "FROM base AS dev\n"
            "COPY --from=ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv${UV_BASE_DIGEST} /uv /uvx /usr/local/bin/\n"
            f"FROM ghcr.io/outernet-foundation/mirror/docker.io/library/python@sha256:{'b' * 64}\n",
            encoding="utf-8",
        )
        assert unpinned_references(tmp_path) == []

    def test_should_flag_slash_refs_without_tag_digest_or_arg(self, tmp_path: Path):
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM ghcr.io/outernet-foundation/mirror/docker.io/library/caddy\n", encoding="utf-8"
        )
        assert unpinned_references(tmp_path) == ["ghcr.io/outernet-foundation/mirror/docker.io/library/caddy"]

    def test_should_ignore_stage_names_and_arg_only_refs(self, tmp_path: Path):
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM neural-networks-base AS dev\nCOPY --from=build /x /y\n", encoding="utf-8"
        )
        assert unpinned_references(tmp_path) == []


class TestVersionCouplingViolations:
    def _write_version_tree(
        self,
        tmp_path: Path,
        uv_tag: str = "0.12.15-python3.13-trixie-slim",
        python_tag: str = "3.13-slim@sha256:" + "c" * 64,
        uv_python_install: str = "3.13",
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.13"\n\n[tool.uv]\nrequired-version = "==0.12.15"\n', encoding="utf-8"
        )
        (tmp_path / "compose.bake.yml").write_text(
            f"x-base-images:\n  UV_BASE_IMAGE: ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv:{uv_tag}\n",
            encoding="utf-8",
        )
        (tmp_path / "compose.zed.bake.yml").write_text(
            "x-base-images:\n"
            f"  UV_BASE_IMAGE: ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv:{uv_tag}\n"
            f"  PYTHON_BASE_IMAGE: ghcr.io/outernet-foundation/mirror/docker.io/library/python:{python_tag}\n",
            encoding="utf-8",
        )
        docker_directory = tmp_path / "docker" / "zed-capture"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(f"RUN uv python install {uv_python_install}\n", encoding="utf-8")

    def test_should_return_empty_when_versions_agree(self, tmp_path: Path):
        self._write_version_tree(tmp_path)
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == []

    def test_should_flag_drifted_uv_base_tag(self, tmp_path: Path):
        self._write_version_tree(tmp_path, uv_tag="0.12.14-python3.13-trixie-slim")
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            f"uv: uv base tag at {tmp_path / 'compose.bake.yml'}: expected 0.12.15, found 0.12.14",
            f"uv: uv base tag at {tmp_path / 'compose.zed.bake.yml'}: expected 0.12.15, found 0.12.14",
        ]

    def test_should_flag_unversioned_uv_base_tag(self, tmp_path: Path):
        self._write_version_tree(tmp_path, uv_tag="python3.13-trixie-slim")
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            f"uv: uv base tag at {tmp_path / 'compose.bake.yml'}: expected 0.12.15, found python3.13",
            f"uv: uv base tag at {tmp_path / 'compose.zed.bake.yml'}: expected 0.12.15, found python3.13",
        ]

    def test_should_flag_uv_base_without_python_component(self, tmp_path: Path):
        self._write_version_tree(tmp_path, uv_tag="0.12.15-trixie-slim")
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            f"python: uv base python component at {tmp_path / 'compose.bake.yml'}: expected 3.13, found <missing>",
            f"python: uv base python component at {tmp_path / 'compose.zed.bake.yml'}: expected 3.13, found <missing>",
        ]

    def test_should_flag_python_component_drift(self, tmp_path: Path):
        self._write_version_tree(tmp_path, python_tag="3.14-slim@sha256:" + "c" * 64, uv_python_install="3.14")
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            f"python: python base tag at {tmp_path / 'compose.zed.bake.yml'}: expected 3.13, found 3.14",
            f"python: uv python install at {tmp_path / 'docker' / 'zed-capture' / 'Dockerfile'}: expected 3.13, found 3.14",
        ]

    def test_should_flag_missing_base_image_key(self, tmp_path: Path):
        self._write_version_tree(tmp_path)
        (tmp_path / "compose.zed.bake.yml").write_text(
            "x-base-images:\n"
            "  UV_BASE_IMAGE: ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv:0.12.15-python3.13-trixie-slim\n",
            encoding="utf-8",
        )
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            f"python: python base tag at {tmp_path / 'compose.zed.bake.yml'}: expected 3.13, found <missing>"
        ]

    def test_should_flag_site_whose_files_are_absent(self, tmp_path: Path):
        self._write_version_tree(tmp_path)
        (tmp_path / "docker" / "zed-capture" / "Dockerfile").unlink()
        assert version_coupling_violations(tmp_path, VERSION_COUPLINGS) == [
            "python: uv python install at docker/zed-capture/Dockerfile: expected 3.13, found <missing>"
        ]

    def test_should_raise_when_pyproject_key_is_absent(self, tmp_path: Path):
        self._write_version_tree(tmp_path)
        (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.13"\n', encoding="utf-8")
        with pytest.raises(RuntimeError):
            version_coupling_violations(tmp_path, VERSION_COUPLINGS)


class TestResolveRemoteDigest:
    def test_should_return_parsed_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_with_digest)
        assert resolve_remote_digest("docker.io/library/alpine:3.20") == FAKE_DIGEST

    def test_should_raise_when_digest_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_without_digest)
        with pytest.raises(RuntimeError):
            resolve_remote_digest("docker.io/library/alpine:3.20")
