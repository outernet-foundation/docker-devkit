import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit.documents import BakeDocument, compute_service_shas, parse_bake

BAKE_CONTENT = """\
services:
  app:
    build:
      context: .
      dockerfile: workloads/app/Dockerfile
      tags:
        - "registry/app:${APP_SHA}"
  worker:
    build:
      context: .
      dockerfile: workloads/worker/Dockerfile
      tags:
        - "registry/worker:${WORKER_SHA}"
  base:
    build:
      context: .
      dockerfile: workloads/base/Dockerfile
"""


def _bake_with(reference: str) -> BakeDocument:
    return BakeDocument.model_validate({"x-base-images": {"TEST_IMAGE": reference}})


class TestBaseImagesValidator:
    def test_should_accept_tagged_reference(self):
        bake = _bake_with("alpine:3.20")
        assert bake.base_images == {"TEST_IMAGE": "alpine:3.20"}

    def test_should_accept_digest_pinned_reference(self):
        reference = f"ghcr.io/outernet-foundation/mirror/docker.io/library/alpine@sha256:{'a' * 64}"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_accept_tag_and_digest_reference(self):
        reference = f"ghcr.io/outernet-foundation/mirror/docker.io/library/python:3.13-slim@sha256:{'a' * 64}"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_accept_registry_port_reference(self):
        reference = "localhost:5000/org/image:1.0"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_default_to_empty(self):
        assert BakeDocument.model_validate({"services": {}}).base_images == {}

    def test_should_reject_bare_path(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine")

    def test_should_reject_empty_tag(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:")

    def test_should_reject_build_arg_hole(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.20${ALPINE_DIGEST}")

    def test_should_reject_short_digest(self):
        with pytest.raises(ValidationError):
            _bake_with(f"ghcr.io/outernet-foundation/mirror/docker.io/library/alpine@sha256:{'a' * 63}")

    def test_should_reject_whitespace(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.20 slim")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / ".dockerignore").write_text("*\n!workloads/\n!packages/\n")
    (tmp_path / "workloads" / "app").mkdir(parents=True)
    (tmp_path / "workloads" / "app" / "main.py").write_text("print('hello')")
    (tmp_path / "workloads" / "worker").mkdir(parents=True)
    (tmp_path / "workloads" / "worker" / "run.py").write_text("print('work')")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "lib.py").write_text("x = 1")
    (tmp_path / "README.md").write_text("ignored")
    manifest = tmp_path / "workloads" / "images.yml"
    manifest.write_text(BAKE_CONTENT)
    _commit_all(tmp_path)
    return tmp_path


class TestComputeServiceShas:
    def test_should_return_deterministic_hashes(self, repo: Path):
        shas1 = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        shas2 = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        assert shas1 == shas2

    def test_should_have_tree_prefix(self, repo: Path):
        shas = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        for sha in shas.values():
            assert sha.startswith("tree-")

    def test_should_return_only_tagged_services(self, repo: Path):
        shas = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        assert set(shas.keys()) == {"APP_SHA", "WORKER_SHA"}

    def test_should_change_only_affected_service_when_service_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        (repo / "workloads" / "app" / "main.py").write_text("print('changed')")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_should_change_all_services_when_shared_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        (repo / "packages" / "lib.py").write_text("x = 2")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] != shas_after["WORKER_SHA"]

    def test_should_not_change_when_ignored_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        (repo / "README.md").write_text("changed readme")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        assert shas_before == shas_after

    def test_should_not_change_when_untagged_workload_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        (repo / "workloads" / "loki").mkdir()
        (repo / "workloads" / "loki" / "config.yaml").write_text("unused by tagged services")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        assert shas_before == shas_after

    def test_should_not_change_from_uncommitted_edits(self, repo: Path):
        shas_before = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))

        (repo / "workloads" / "app" / "main.py").write_text("print('uncommitted')")

        shas_after = compute_service_shas(repo, parse_bake(repo / "workloads" / "images.yml"))
        assert shas_before == shas_after

    def test_should_raise_when_services_span_multiple_top_level_dirs(self, repo: Path):
        bake = BakeDocument.model_validate({
            "services": {
                "app": {"build": {"dockerfile": "workloads/app/Dockerfile", "tags": ["registry/app:latest"]}},
                "worker": {"build": {"dockerfile": "other/worker/Dockerfile", "tags": ["registry/worker:latest"]}},
            }
        })

        with pytest.raises(RuntimeError, match="multiple top-level"):
            compute_service_shas(repo, bake)
