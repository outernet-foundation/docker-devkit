import json
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from docker_devkit import publish_compose
from docker_devkit.config import PublishComposeConfig, load_config

REPOSITORY = "ghcr.io/outernet-foundation/lbe-toolkit"
FAKE_SHA = "a" * 40
FAKE_DIGEST = "sha256:" + "b" * 64
CADDY_LOCK_REF = f"ghcr.io/outernet-foundation/mirror/docker.io/library/caddy:2.10@sha256:{'c' * 64}"

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _git_output(command: str) -> str:
    if command == "git rev-parse HEAD":
        return FAKE_SHA
    return "feature/tier-bundle"


class PublishingRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.baked_snapshots: dict[str, str] = {}

    def run(self, command: str, stdin_text: str | None = None) -> None:
        self.commands.append(command)
        tokens = command.split()
        for index, word in enumerate(tokens[:-1]):
            if word == "-f":
                baked_path = Path(tokens[index + 1])
                self.baked_snapshots[baked_path.name] = baked_path.read_text(encoding="utf-8")


class DigestResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, reference: str) -> str:
        self.calls.append(reference)
        return FAKE_DIGEST


def _service_shas(root: Path, bake: object) -> dict[str, str]:
    return {"LIVEKIT_TOKEN_SHA": "tree-abc"}


class TestPublishComposeConfig:
    def test_loads_declared_section(self, tmp_path: Path):
        _write_config(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "livekit-tier",
                    "files": ["workloads/livekit-tier/compose.yml"],
                }
            },
        )

        config = load_config(tmp_path)

        assert config is not None
        assert config.publish_compose == PublishComposeConfig(
            repository=REPOSITORY, artifact="livekit-tier", files=["workloads/livekit-tier/compose.yml"]
        )

    def test_rejects_unknown_keys(self, tmp_path: Path):
        _write_config(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "livekit-tier",
                    "files": ["compose.yml"],
                    "bogus": "true",
                }
            },
        )

        with pytest.raises(ValidationError):
            load_config(tmp_path)

    def test_rejects_repository_without_host(self):
        with pytest.raises(ValidationError):
            PublishComposeConfig(repository="lbe-toolkit", artifact="livekit-tier", files=["compose.yml"])

    def test_rejects_trailing_slash_repository(self):
        with pytest.raises(ValidationError):
            PublishComposeConfig(repository=f"{REPOSITORY}/", artifact="livekit-tier", files=["compose.yml"])

    def test_rejects_empty_files(self):
        with pytest.raises(ValidationError):
            PublishComposeConfig(repository=REPOSITORY, artifact="livekit-tier", files=[])

    def test_rejects_artifact_with_uppercase(self):
        with pytest.raises(ValidationError):
            PublishComposeConfig(repository=REPOSITORY, artifact="LiveKit-Tier", files=["compose.yml"])

    def test_rejects_duplicate_variants(self):
        with pytest.raises(ValidationError):
            PublishComposeConfig(
                repository=REPOSITORY, artifact="tier", files=["compose.yml"], variants=["cuda", "cuda"]
            )


class TestBakeSources:
    def test_substitutes_known_vars_and_keeps_consumer_holes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "compose.yml").write_text(
            "services:\n"
            "  api:\n"
            '    image: "registry/api:${API_SHA:?err}"\n'
            "    environment:\n"
            '      KEY: "${API_KEY:?err}"\n'
            '      MODE: "${API_MODE:-default}"\n',
            encoding="utf-8",
        )

        baked_paths = publish_compose.bake_sources(tmp_path / "baked", ["nested/compose.yml"], {"API_SHA": "tree-abc"})

        assert baked_paths == [tmp_path / "baked" / "nested" / "compose.yml"]
        assert baked_paths[0].read_text(encoding="utf-8") == (
            "services:\n"
            "  api:\n"
            '    image: "registry/api:tree-abc"\n'
            "    environment:\n"
            '      KEY: "${API_KEY:?err}"\n'
            '      MODE: "${API_MODE:-default}"\n'
        )


class TestFinalizeBaked:
    def test_inlines_configs_pins_own_refs_and_leaves_foreign_refs_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / "Caddyfile").write_text("reverse_proxy {$UPSTREAM}\n", encoding="utf-8")
        baked = tmp_path / "compose.yml"
        baked.write_text(
            "services:\n"
            "  token:\n"
            f"    image: {REPOSITORY}/livekit-token:tree-abc\n"
            "  ingress:\n"
            f"    image: {CADDY_LOCK_REF}\n"
            "  other:\n"
            "    image: ghcr.io/other-org/other-service:1.0\n"
            "configs:\n"
            "  caddyfile:\n"
            "    file: Caddyfile\n",
            encoding="utf-8",
        )
        resolver = DigestResolver()
        monkeypatch.setattr(publish_compose, "resolve_remote_digest", resolver.resolve)
        monkeypatch.chdir(tmp_path)

        publish_compose.finalize_baked([baked], REPOSITORY)

        document = yaml.safe_load(baked.read_text(encoding="utf-8"))
        assert document["services"]["token"]["image"] == f"{REPOSITORY}/livekit-token@{FAKE_DIGEST}"
        assert document["services"]["ingress"]["image"] == CADDY_LOCK_REF
        assert document["services"]["other"]["image"] == "ghcr.io/other-org/other-service:1.0"
        assert document["configs"]["caddyfile"] == {"content": "reverse_proxy {$$UPSTREAM}\n"}
        assert resolver.calls == [f"{REPOSITORY}/livekit-token:tree-abc"]

    def test_pins_repeated_references_through_one_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        reference = f"{REPOSITORY}/livekit-token:tree-abc"
        first = tmp_path / "one.yml"
        first.write_text(f"services:\n  a:\n    image: {reference}\n", encoding="utf-8")
        second = tmp_path / "two.yml"
        second.write_text(f"services:\n  b:\n    image: {reference}\n", encoding="utf-8")
        resolver = DigestResolver()
        monkeypatch.setattr(publish_compose, "resolve_remote_digest", resolver.resolve)

        publish_compose.finalize_baked([first, second], REPOSITORY)

        pinned = f"{REPOSITORY}/livekit-token@{FAKE_DIGEST}"
        assert resolver.calls == [reference]
        assert first.read_text(encoding="utf-8") == f"services:\n  a:\n    image: {pinned}\n"
        assert second.read_text(encoding="utf-8") == f"services:\n  b:\n    image: {pinned}\n"


class TestPublishComposeVerb:
    def _write_tree(self, tmp_path: Path, sections: dict[str, object] | None = None) -> None:
        _write_config(
            tmp_path,
            sections
            or {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "livekit-tier",
                    "files": ["compose.yml"],
                }
            },
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("services: {}\n", encoding="utf-8")
        (tmp_path / "workloads" / "images.lock").write_text(f"CADDY_IMAGE={CADDY_LOCK_REF}\n", encoding="utf-8")
        (tmp_path / "Caddyfile").write_text("reverse_proxy {$UPSTREAM}\n", encoding="utf-8")
        (tmp_path / "compose.yml").write_text(
            "services:\n"
            "  livekit-token:\n"
            f"    image: {REPOSITORY}/livekit-token:${{LIVEKIT_TOKEN_SHA:?err}}\n"
            "    environment:\n"
            '      LIVEKIT_API_KEY: "${LIVEKIT_API_KEY:?err}"\n'
            "  ingress:\n"
            '    image: "${CADDY_IMAGE:?err}"\n'
            "    configs:\n"
            "      - source: caddyfile\n"
            "        target: /etc/caddy/Caddyfile\n"
            "configs:\n"
            "  caddyfile:\n"
            "    file: Caddyfile\n",
            encoding="utf-8",
        )

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PublishingRecorder:
        recorder = PublishingRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("REGISTRY_USERNAME", "publisher")
        monkeypatch.setenv("REGISTRY_TOKEN", "secret")
        monkeypatch.setattr(publish_compose, "bash", recorder.run)
        monkeypatch.setattr(publish_compose, "bash_output", _git_output)
        monkeypatch.setattr(publish_compose, "compute_service_shas", _service_shas)
        monkeypatch.setattr(publish_compose, "resolve_remote_digest", DigestResolver().resolve)
        return recorder

    def test_publishes_sha_and_branch_tags_with_baked_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        recorder = self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands[0].startswith("docker login ghcr.io -u publisher")
        publish_commands = [command for command in recorder.commands if command.startswith("docker compose")]
        assert len(publish_commands) == 2
        assert f"publish {REPOSITORY}/livekit-tier:{FAKE_SHA} --yes" in publish_commands[0]
        assert f"publish {REPOSITORY}/livekit-tier:feature-tier-bundle --yes" in publish_commands[1]
        document = yaml.safe_load(recorder.baked_snapshots["compose.yml"])
        assert document["services"]["livekit-token"]["image"] == f"{REPOSITORY}/livekit-token@{FAKE_DIGEST}"
        assert document["services"]["ingress"]["image"] == CADDY_LOCK_REF
        assert document["services"]["livekit-token"]["environment"]["LIVEKIT_API_KEY"] == "${LIVEKIT_API_KEY:?err}"
        assert document["configs"]["caddyfile"] == {"content": "reverse_proxy {$$UPSTREAM}\n"}
        assert os.environ.get("LIVEKIT_API_KEY") == publish_compose.STUB_VALUE
        monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)

    def test_publishes_variant_leg_with_suffixed_artifact_and_gpu_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_tree(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "stack",
                    "files": ["compose.{gpu}.yml"],
                    "variants": ["cuda", "rocm"],
                }
            },
        )
        (tmp_path / "compose.cuda.yml").write_text(
            (tmp_path / "compose.yml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (tmp_path / "compose.rocm.yml").write_text("services: {}\n", encoding="utf-8")
        recorder = self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, ["--variant", "cuda"])

        assert result.exit_code == 0, result.output
        publish_commands = [command for command in recorder.commands if command.startswith("docker compose")]
        assert len(publish_commands) == 2
        assert f"publish {REPOSITORY}/stack-cuda:{FAKE_SHA} --yes" in publish_commands[0]
        assert "compose.cuda.yml" in recorder.baked_snapshots
        monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)

    def test_requires_variant_when_declared(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "stack",
                    "files": ["compose.{gpu}.yml"],
                    "variants": ["cuda"],
                }
            },
        )
        self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code != 0

    def test_rejects_undeclared_variant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, ["--variant", "cuda"])

        assert result.exit_code != 0

    def test_rejects_gpu_placeholder_without_variants(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "tier",
                    "files": ["compose.{gpu}.yml"],
                }
            },
        )
        (tmp_path / "compose.cuda.yml").write_text("services: {}\n", encoding="utf-8")
        self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)

    def test_errors_without_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("REGISTRY_USERNAME", "publisher")
        monkeypatch.setenv("REGISTRY_TOKEN", "secret")

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)

    def test_errors_without_registry_credentials(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)


class TestPlan:
    def test_no_section_plans_disabled_without_credentials(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, ["--plan"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []

    def test_no_config_file_plans_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, ["--plan"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []

    def test_section_without_variants_plans_one_bare_leg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "livekit-tier",
                    "files": ["compose.yml"],
                }
            },
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, ["--plan"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{}]

    def test_declared_variants_plan_one_leg_each(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "publish-compose": {
                    "repository": REPOSITORY,
                    "artifact": "stack",
                    "files": ["compose.{gpu}.yml"],
                    "variants": ["cuda", "rocm"],
                }
            },
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, ["--plan"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{"variant": "cuda"}, {"variant": "rocm"}]
