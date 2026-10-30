import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit import documents
from docker_devkit.config import DockerDevkitConfig, ServiceBuild
from docker_devkit.documents import compute_service_shas, parse_image_lock, render_bake

REPOSITORY = "ghcr.io/outernet-foundation/example"


def _sha_config(*dockerfiles: str) -> DockerDevkitConfig:
    config = DockerDevkitConfig.model_construct(requires=">=0")
    config.service_builds = {
        PurePosixPath(dockerfile).parent.name: ServiceBuild(
            name=PurePosixPath(dockerfile).parent.name, dockerfile=dockerfile
        )
        for dockerfile in dockerfiles
    }
    return config


def _render_config(
    services: dict[str, ServiceBuild],
    *,
    repository: str | None = REPOSITORY,
    base_images: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> DockerDevkitConfig:
    config = DockerDevkitConfig.model_construct(
        requires=">=0",
        repository=repository,
        base_images=base_images or {},
        platforms=platforms or [],
    )
    config.service_builds = services
    return config


def _rendered_services(rendered: str) -> dict[str, Any]:
    return yaml.safe_load(rendered)["services"]


def test_parse_image_lock_rejects_entries_without_a_digest(tmp_path: Path) -> None:
    lock = tmp_path / "images.lock"
    lock.write_text("LOKI_IMAGE=ghcr.io/example/loki:3.5.0\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="not digest-pinned"):
        parse_image_lock(lock)


class TestRenderBakeDeterminism:
    def test_identical_configs_render_byte_identical_files(self):
        def build() -> DockerDevkitConfig:
            return _render_config(
                {
                    "api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile"),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        contexts=["neural-networks-base"],
                        variants=["cuda", "rocm"],
                        sha_args=["LOCALIZER_SHA"],
                    ),
                },
                base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            )

        assert render_bake(build(), push=False, host="linux/amd64") == render_bake(
            build(), push=False, host="linux/amd64"
        )


class TestRenderBakeTagDerivation:
    def test_unfanned_service_carries_one_target_with_latest_and_sha_tags(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert list(services) == ["api"]
        assert services["api"]["build"]["tags"] == [
            f"{REPOSITORY}/api:latest",
            f"{REPOSITORY}/api:${{API_SHA}}",
        ]

    def test_variant_fans_derive_per_variant_targets_sharing_one_sha_env(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert list(services) == ["localizer-cuda", "localizer-rocm"]
        assert services["localizer-cuda"]["build"]["tags"] == [
            f"{REPOSITORY}/localizer-cuda:latest",
            f"{REPOSITORY}/localizer-cuda:${{LOCALIZER_SHA}}",
        ]
        assert services["localizer-rocm"]["build"]["tags"] == [
            f"{REPOSITORY}/localizer-rocm:latest",
            f"{REPOSITORY}/localizer-rocm:${{LOCALIZER_SHA}}",
        ]


class TestRenderBakeArgs:
    def test_every_base_image_key_is_injected_on_every_service(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15", "ALPINE_IMAGE": "alpine:3.20"},
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["args"] == {
            "UV_BASE_IMAGE": "${UV_BASE_IMAGE:-}",
            "ALPINE_IMAGE": "${ALPINE_IMAGE:-}",
        }

    def test_sha_args_render_only_for_declaring_dockerfiles(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile"),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        sha_args=["LOCALIZER_SHA"],
                    ),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert "LOCALIZER_SHA" not in services["api"]["build"]["args"]
        assert services["localizer"]["build"]["args"] == {"LOCALIZER_SHA": "${LOCALIZER_SHA:?err}"}

    def test_fanned_targets_carry_the_torch_device_literal(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["args"]["TORCH_DEVICE"] == "cuda"
        assert services["localizer-rocm"]["build"]["args"]["TORCH_DEVICE"] == "rocm"


class TestRenderBakeContexts:
    def test_contexts_render_target_prefixed_and_variant_matched(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "neural-networks-base": ServiceBuild(
                        name="neural-networks-base",
                        dockerfile="workloads/neural-networks-base/Dockerfile",
                        variants=["cuda", "rocm"],
                    ),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        contexts=["neural-networks-base"],
                        variants=["cuda", "rocm"],
                    ),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-cuda"
        }
        assert services["localizer-rocm"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-rocm"
        }

    def test_unfanned_context_references_the_bare_target(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "base": ServiceBuild(name="base", dockerfile="workloads/base/Dockerfile"),
                    "app": ServiceBuild(name="app", dockerfile="workloads/app/Dockerfile", contexts=["base"]),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["app"]["build"]["additional_contexts"] == {"base": "target:base"}


class TestRenderBakeCache:
    def test_push_mode_renders_registry_cache_refs_per_target(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda"],
                    )
                }),
                push=True,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["cache_to"] == [
            (
                f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda,"
                "mode=max,image-manifest=true,oci-mediatypes=true"
            )
        ]
        assert services["localizer-cuda"]["build"]["cache_from"] == [
            f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda"
        ]

    def test_load_mode_renders_no_cache_refs(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert "cache_to" not in services["api"]["build"]
        assert "cache_from" not in services["api"]["build"]


class TestRenderBakeDev:
    def test_dev_targets_the_dev_stage_under_one_service_dev_tag(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
                dev=True,
            )
        )

        assert services["api"]["build"]["target"] == "dev"
        assert services["api"]["build"]["tags"] == [f"{REPOSITORY}/api:dev"]

    def test_fanned_variants_share_the_service_dev_tag(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
                dev=True,
            )
        )

        assert services["localizer-cuda"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]
        assert services["localizer-rocm"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]


class TestRenderBakePlatforms:
    def test_multi_arch_declaration_collapses_to_the_host_in_load_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    platforms=["linux/amd64", "linux/arm64"],
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["platforms"] == ["linux/amd64"]

    def test_multi_arch_declaration_rides_through_in_push_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    platforms=["linux/amd64", "linux/arm64"],
                ),
                push=True,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["platforms"] == ["linux/amd64", "linux/arm64"]

    def test_single_declared_platform_rides_through_in_load_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"zed-capture": ServiceBuild(name="zed-capture", dockerfile="workloads/zed-capture/Dockerfile")},
                    platforms=["linux/arm64"],
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["zed-capture"]["build"]["platforms"] == ["linux/arm64"]

    def test_undeclared_platforms_render_no_key(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert "platforms" not in services["api"]["build"]


def test_render_bake_carries_only_bake_read_keys():
    rendered = render_bake(
        _render_config(
            {
                "api": ServiceBuild(
                    name="api",
                    dockerfile="workloads/api/Dockerfile",
                    contexts=["base"],
                    sha_args=["API_SHA"],
                ),
                "base": ServiceBuild(name="base", dockerfile="workloads/base/Dockerfile"),
            },
            base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
        ),
        push=False,
        host="linux/amd64",
    )

    document = yaml.safe_load(rendered)
    assert list(document) == ["services"]
    for build in (service["build"] for service in document["services"].values()):
        assert set(build) <= {
            "dockerfile",
            "platforms",
            "args",
            "tags",
            "additional_contexts",
            "cache_to",
            "cache_from",
        }


def test_render_bake_with_no_services_renders_an_empty_document():
    assert _rendered_services(render_bake(_render_config({}), push=False, host="linux/amd64")) == {}


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
    _commit_all(tmp_path)
    return tmp_path


class TestComputeServiceShasZeroServices:
    def _fail_bash(self, command: str) -> str:
        raise AssertionError(f"zero services must not shell out, saw: {command}")

    def test_a_serviceless_repo_returns_empty_without_git_or_dockerignore_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        config = _sha_config()
        monkeypatch.setattr(documents, "bash_output", self._fail_bash)
        monkeypatch.setattr(documents, "bash", self._fail_bash)

        assert compute_service_shas(tmp_path, config) == {}


class TestComputeServiceShas:
    def test_should_return_deterministic_hashes(self, repo: Path):
        shas1 = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        shas2 = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        assert shas1 == shas2

    def test_should_have_tree_prefix(self, repo: Path):
        shas = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        for sha in shas.values():
            assert sha.startswith("tree-")

    def test_should_return_only_tagged_services(self, repo: Path):
        shas = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        assert set(shas.keys()) == {"APP_SHA", "WORKER_SHA"}

    def test_should_change_only_affected_service_when_service_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        (repo / "workloads" / "app" / "main.py").write_text("print('changed')")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_should_change_all_services_when_shared_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        (repo / "packages" / "lib.py").write_text("x = 2")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] != shas_after["WORKER_SHA"]

    def test_should_not_change_when_ignored_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        (repo / "README.md").write_text("changed readme")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        assert shas_before == shas_after

    def test_should_not_change_when_untagged_workload_file_changes(self, repo: Path):
        shas_before = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        (repo / "workloads" / "loki").mkdir()
        (repo / "workloads" / "loki" / "config.yaml").write_text("unused by tagged services")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        assert shas_before == shas_after

    def test_should_not_change_from_uncommitted_edits(self, repo: Path):
        shas_before = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        (repo / "workloads" / "app" / "main.py").write_text("print('uncommitted')")

        shas_after = compute_service_shas(repo, _sha_config("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))
        assert shas_before == shas_after

    def test_should_raise_when_services_span_multiple_top_level_dirs(self, repo: Path):
        config = _sha_config("workloads/app/Dockerfile", "other/worker/Dockerfile")

        with pytest.raises(RuntimeError, match="multiple top-level"):
            compute_service_shas(repo, config)
