import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from docker_devkit import crane, publish_compose
from docker_devkit.config import LOCK_PATH, load_config

REPOSITORY = "ghcr.io/outernet-foundation/lbe-toolkit"
FAKE_SHA = "a" * 40
FAKE_DIGEST = "sha256:" + "b" * 64
CADDY_LOCK_REF = f"ghcr.io/outernet-foundation/mirror/docker.io/library/caddy:2.10@sha256:{'c' * 64}"

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _command_output(command: str) -> str:
    if command == "git rev-parse HEAD":
        return FAKE_SHA
    if command.startswith("crane digest "):
        return FAKE_DIGEST
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


def _invoke_publishing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PublishingRecorder:
    recorder = PublishingRecorder()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REGISTRY_USERNAME", "publisher")
    monkeypatch.setenv("REGISTRY_TOKEN", "secret")
    monkeypatch.setattr(publish_compose, "bash", recorder.run)
    monkeypatch.setattr(crane, "bash", recorder.run)
    monkeypatch.setattr(publish_compose, "bash_output", _command_output)
    monkeypatch.setattr(publish_compose, "compute_service_shas", _service_shas)
    monkeypatch.setattr(publish_compose, "resolve_remote_digest", DigestResolver().resolve)
    return recorder


SEAM_COMPOSE = (
    "services:\n"
    "  api:\n"
    f"    image: {REPOSITORY}/api:tree-abc\n"
    "    environment:\n"
    '      AUTH_ISSUER_URL: "${AUTH_ISSUER_URL:?err}"\n'
)

ROOT_BUNDLE_SECTION = {
    "repository": REPOSITORY,
    "project_name": "example",
    "services": {
        "core-cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
        "core-rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
        "gateway-open": {"workloads": ["gateway.yaml"]},
    },
}


class TestPublishGating:
    def test_the_old_publish_compose_section_refuses(self, tmp_path: Path):
        _write_config(tmp_path, {"publish-compose": {"repository": REPOSITORY}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            load_config(tmp_path)

    def test_bundles_without_repository_refuse_at_load(self, tmp_path: Path):
        _write_config(
            tmp_path,
            {"project_name": "example", "services": {"core": {"workloads": ["api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match="repository is required"):
            load_config(tmp_path)


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

    def test_authored_content_configs_ride_through_verbatim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        baked = tmp_path / "compose.yml"
        baked.write_text(
            "services:\n"
            "  rathole-client:\n"
            '    image: "${RATHOLE_IMAGE:?err}"\n'
            "configs:\n"
            "  client-toml:\n"
            "    content: |\n"
            '      remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        publish_compose.finalize_baked([baked], REPOSITORY)

        document = yaml.safe_load(baked.read_text(encoding="utf-8"))
        assert document["configs"]["client-toml"] == {"content": 'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n'}

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
                "repository": REPOSITORY,
                "project_name": "livekit-tier",
                "services": {"livekit-tier": {"workloads": ["livekit-token.yaml"]}},
            },
        )
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(f"CADDY_IMAGE={CADDY_LOCK_REF}\n", encoding="utf-8")
        (tmp_path / "Caddyfile").write_text("reverse_proxy {$UPSTREAM}\n", encoding="utf-8")
        artifact = tmp_path / "stack" / "generated" / "compose" / "compose.livekit-tier.yaml"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(
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
        return _invoke_publishing(monkeypatch, tmp_path)

    def test_publishes_once_under_the_sha_tag_and_aliases_the_branch_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_tree(tmp_path)
        recorder = self._invoke(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands[0].startswith("docker login ghcr.io -u publisher")
        publish_commands = [command for command in recorder.commands if command.startswith("docker compose")]
        assert len(publish_commands) == 1
        assert f"publish {REPOSITORY}/livekit-tier:{FAKE_SHA} --yes" in publish_commands[0]
        tag_commands = [command for command in recorder.commands if command.startswith("crane tag")]
        assert tag_commands == [f"crane tag {REPOSITORY}/livekit-tier:{FAKE_SHA} feature-tier-bundle"]
        assert f"Digest {REPOSITORY}/livekit-tier:{FAKE_SHA} {FAKE_DIGEST}" in result.stdout
        assert f"Digest {REPOSITORY}/livekit-tier:feature-tier-bundle {FAKE_DIGEST}" in result.stdout
        document = yaml.safe_load(recorder.baked_snapshots["compose.livekit-tier.yaml"])
        assert document["services"]["livekit-token"]["image"] == f"{REPOSITORY}/livekit-token@{FAKE_DIGEST}"
        assert document["services"]["ingress"]["image"] == CADDY_LOCK_REF
        assert document["services"]["livekit-token"]["environment"]["LIVEKIT_API_KEY"] == "${LIVEKIT_API_KEY:?err}"
        assert document["configs"]["caddyfile"] == {"content": "reverse_proxy {$$UPSTREAM}\n"}
        assert os.environ.get("LIVEKIT_API_KEY") == publish_compose.STUB_VALUE
        monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)

    def test_appends_the_sha_to_digest_table_to_the_job_summary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        self._invoke(monkeypatch, tmp_path)
        summary_path = tmp_path / "job-summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code == 0, result.output
        summary = summary_path.read_text(encoding="utf-8")
        assert "| livekit-tier |" in summary
        assert f"`{REPOSITORY}/livekit-tier:{FAKE_SHA}`" in summary
        assert f"`{REPOSITORY}/livekit-tier:feature-tier-bundle`" in summary
        assert f"`{FAKE_DIGEST}`" in summary

    def test_errors_without_bundles(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"repository": REPOSITORY})
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


class TestBundleMode:
    def _write_tree(self, tmp_path: Path, sections: dict[str, object] | None = None) -> None:
        _write_config(tmp_path, sections or ROOT_BUNDLE_SECTION)
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(f"ALPINE_IMAGE=alpine:3.20@sha256:{'a' * 64}\n", encoding="utf-8")
        compose_dir = tmp_path / "stack" / "generated" / "compose"
        compose_dir.mkdir(parents=True)
        for name in ("compose.core-cuda.yaml", "compose.core-rocm.yaml", "compose.gateway-open.yaml"):
            (compose_dir / name).write_text(SEAM_COMPOSE, encoding="utf-8")

    def test_plan_emits_one_leg_per_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
        monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

        result = runner.invoke(publish_compose.app, ["--plan"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [
            {"bundle": "core-cuda"},
            {"bundle": "core-rocm"},
            {"bundle": "gateway-open"},
        ]

    def test_bundle_flag_publishes_that_leg_under_the_bundle_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_tree(tmp_path)
        recorder = _invoke_publishing(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, ["--bundle", "core-cuda"])

        assert result.exit_code == 0, result.output
        publish_commands = [command for command in recorder.commands if command.startswith("docker compose")]
        assert len(publish_commands) == 1
        assert f"publish {REPOSITORY}/core-cuda:{FAKE_SHA} --yes" in publish_commands[0]
        assert [command for command in recorder.commands if command.startswith("crane tag")] == [
            f"crane tag {REPOSITORY}/core-cuda:{FAKE_SHA} feature-tier-bundle"
        ]
        assert "compose.core-cuda.yaml" in recorder.baked_snapshots
        assert "compose.gateway-open.yaml" not in recorder.baked_snapshots
        monkeypatch.delenv("AUTH_ISSUER_URL", raising=False)

    def test_bare_invocation_publishes_every_bundle_leg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        recorder = _invoke_publishing(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, [])

        assert result.exit_code == 0, result.output
        publish_commands = [command for command in recorder.commands if command.startswith("docker compose")]
        assert len(publish_commands) == 3
        published = [command.split("publish ")[1].rsplit(":", 1)[0] for command in publish_commands]
        assert published == [
            f"{REPOSITORY}/core-cuda",
            f"{REPOSITORY}/core-rocm",
            f"{REPOSITORY}/gateway-open",
        ]
        assert [command for command in recorder.commands if command.startswith("crane tag")] == [
            f"crane tag {REPOSITORY}/core-cuda:{FAKE_SHA} feature-tier-bundle",
            f"crane tag {REPOSITORY}/core-rocm:{FAKE_SHA} feature-tier-bundle",
            f"crane tag {REPOSITORY}/gateway-open:{FAKE_SHA} feature-tier-bundle",
        ]
        assert sorted(recorder.baked_snapshots) == [
            "compose.core-cuda.yaml",
            "compose.core-rocm.yaml",
            "compose.gateway-open.yaml",
        ]
        monkeypatch.delenv("AUTH_ISSUER_URL", raising=False)

    def test_unknown_bundle_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        _invoke_publishing(monkeypatch, tmp_path)

        result = runner.invoke(publish_compose.app, ["--bundle", "core-fabric"])

        assert result.exit_code != 0


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
