import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from typer.testing import CliRunner

from docker_devkit import compose
from docker_devkit.compose import ComposeContext
from docker_devkit.config import LOCK_PATH

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64
KEYCLOAK_REF = "oci://ghcr.io/org/keycloak@" + FAKE_DIGEST
LIVEKIT_REF = "oci://ghcr.io/org/livekit@" + FAKE_DIGEST
PINNED_COMPOSE_VERSION = "5.6.1"

LOCKED_ERROR = (
    f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {FAKE_DIGEST}) is not mirrored yet; "
    "the CI mirror job populates it when the declaring change runs"
)

DEV_TOGGLES = "outernet.internal/dev-toggles"
OVERLAY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-dev-overlay-\S+\.yaml)")
ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-assembly-\S+\.yaml)")

runner = CliRunner()


@pytest.fixture(scope="session")
def pinned_compose(tmp_path_factory: pytest.TempPathFactory) -> str:
    # Pin the exact compose binary this suite asserts include-merge behavior against,
    # so a host's compose version cannot change what the pin verifies.
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}[platform.machine()]
    binary = tmp_path_factory.mktemp("compose-bin") / "docker-compose"
    release = (
        "https://github.com/docker/compose/releases/download/"
        f"v{PINNED_COMPOSE_VERSION}/docker-compose-linux-{architecture}"
    )
    bash(f"curl -fsSL {release} -o {binary}")
    binary.chmod(0o755)
    return str(binary)


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    (root / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _write_lock(root: Path, entries: dict[str, str] | None = None) -> None:
    declared = entries or {
        "CADDY_IMAGE": MIRROR_PREFIX + "/docker.io/library/caddy:2.8.4-alpine@" + FAKE_DIGEST,
    }
    root.joinpath(*LOCK_PATH.parts).write_text(
        "".join(f"{name}={reference}\n" for name, reference in declared.items()), encoding="utf-8"
    )


def _write_bundle_output(root: Path, name: str, services: list[str]) -> Path:
    path = root / "stack" / "generated" / "compose" / f"compose.{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"services": {service: {"image": f"demo/{service}:1"} for service in services}}),
        encoding="utf-8",
    )
    return path


def _write_workload(root: Path, name: str, service_name: str, annotations: dict[str, str] | None = None) -> None:
    path = root / "stack" / "score" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"name": service_name}
    if annotations is not None:
        metadata["annotations"] = annotations
    path.write_text(yaml.safe_dump({"metadata": metadata}), encoding="utf-8")


def _no_profiles(compose_prefix: str) -> list[str]:
    return []


def _defined_profiles(compose_prefix: str) -> list[str]:
    return ["keycloak", "tunnel-ngrok"]


def _rathole_profiles(compose_prefix: str) -> list[str]:
    return ["tunnel-rathole"]


def _silent_handoff(_command: str) -> None:
    return None


def _probe_present(command: str) -> str:
    assert "imagetools inspect" in command
    return ""


def _probe_absent(command: str) -> str:
    assert "imagetools inspect" in command
    raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found\n")


def _probe_unauthorized(command: str) -> str:
    assert "imagetools inspect" in command
    raise CalledProcessError(1, command, stderr="unauthorized: authentication required\n")


class SelectiveProbe:
    def __init__(self, present: str) -> None:
        self.present = present
        self.probed: list[str] = []

    def run(self, command: str) -> str:
        reference = command.rpartition("inspect ")[2]
        self.probed.append(reference)
        if reference == self.present:
            return ""
        raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found")


class HandoffRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)


class AssemblyRecorder(HandoffRecorder):
    # Captures the materialized per-run root compose file alongside the handoff command.
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.documents.append(yaml.safe_load(Path(match.group(1)).read_text(encoding="utf-8")))


class ServicesOutput:
    def __init__(self, *names: str) -> None:
        self.names = list(names)

    def run(self, command: str) -> str:
        assert "config --services" in command
        return "".join(f"{name}\n" for name in self.names)


class PresentProbeAndServices:
    # The full up-path bash_output: the mirror probe finds every entry present, and the
    # assembly validation resolves the given service set.
    def __init__(self, *names: str) -> None:
        self.names = list(names)

    def run(self, command: str) -> str:
        if "imagetools inspect" in command:
            return ""
        assert "config --services" in command
        return "".join(f"{name}\n" for name in self.names)


class TestProfileForwarding:
    def _invoke_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> HandoffRecorder:
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "enumerate_profiles", _defined_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_forwards_profiles_declared_in_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env "
                "--profile tunnel-ngrok up --pull missing -d"
            )
        ]

    def test_splits_commas_strips_whitespace_and_deduplicates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text('COMPOSE_PROFILES=" keycloak , , tunnel-ngrok ,keycloak"\n', encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env "
                "--profile keycloak --profile tunnel-ngrok up --pull missing -d"
            )
        ]

    def test_shell_environment_overrides_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        monkeypatch.setenv("COMPOSE_PROFILES", "tunnel-rathole")
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "enumerate_profiles", _rathole_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env "
                "--profile tunnel-rathole up --pull missing -d"
            )
        ]

    def test_empty_value_forwards_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env up --pull missing -d"
        ]

    def test_unknown_profile_fails_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=keycloak,ghost\n", encoding="utf-8")
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "ghost" in str(result.exception)

    def test_absent_profiles_forward_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env up --pull missing -d"
        ]


def _write_consumer_tree(root: Path, *, bundles: str | None = "cuda,keycloak") -> Path:
    # The canonical consumer: an auth group (keycloak ref), a placeframe group (local
    # cuda/rocm options with riding routes), and a bare riding livekit ref with ports.
    # bundles=None writes no BUNDLES line at all.
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "gateway": "${GATEWAY_PORT:?err}",
            "services": {
                "auth": {
                    "options": {
                        "keycloak": {
                            "ref": KEYCLOAK_REF,
                            "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                        }
                    },
                },
                "placeframe": {
                    "options": {
                        "cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
                        "rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
                    },
                    "routes": [
                        {"path": "/grafana/*", "upstream": "grafana:3000"},
                        {"path": "/*", "upstream": "api:8000"},
                    ],
                },
                "livekit": {
                    "ref": LIVEKIT_REF,
                    "ports": ["${LIVEKIT_SIGNALING_PORT:?err}/${LIVEKIT_SIGNALING_PORT:?err}/tcp"],
                },
            },
        },
    )
    _write_lock(root)
    cuda = _write_bundle_output(root, "cuda", ["api", "grafana"])
    _write_bundle_output(root, "rocm", ["api", "grafana"])
    _write_workload(root, "api.yaml", "api")
    environment = "GATEWAY_PORT=58080\nLIVEKIT_SIGNALING_PORT=6178\nPUBLIC_URL=http://192.168.8.10:58080\n"
    if bundles is not None:
        environment = f"BUNDLES={bundles}\n" + environment
    (root / ".env").write_text(environment, encoding="utf-8")
    return cuda


class TestSelectionLaw:
    def _invoke_up(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        bundles: str | None = "cuda,keycloak",
        services: str = "api grafana door livekit keycloak",
    ) -> AssemblyRecorder:
        _write_consumer_tree(tmp_path, bundles=bundles)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.setattr(compose, "bash_output", PresentProbeAndServices(*services.split()).run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_options_of_different_groups_select_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = self._invoke_up(tmp_path, monkeypatch)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [
            KEYCLOAK_REF,
            (tmp_path / "stack/generated/compose/compose.cuda.yaml").resolve().as_posix(),
            LIVEKIT_REF,
        ]

    def test_zero_selection_is_legal_when_riders_and_routes_carry_the_assembly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "gateway": "8080",
                "services": {
                    "livekit": {
                        "ref": LIVEKIT_REF,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                        "ports": ["6178/6178"],
                    }
                },
            },
        )
        _write_lock(tmp_path)
        (tmp_path / ".env").write_text("BUNDLES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.setattr(compose, "bash_output", PresentProbeAndServices("livekit", "door").run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [LIVEKIT_REF]

    def test_an_options_only_repo_refuses_a_selection_of_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "gateway": "8080",
                "services": {
                    "placeframe": {
                        "options": {
                            "cuda": {
                                "workloads": ["api.yaml"],
                                "routes": [{"path": "/*", "upstream": "api:8000"}],
                            }
                        }
                    }
                },
                "repository": "ghcr.io/org/demo",
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "cuda", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.setattr(compose, "bash_output", ServicesOutput("api", "door").run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "selects nothing and no bare entry rides" in str(result.exception)

    def test_unknown_name_refuses_listing_the_vocabulary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="cuda,ghost")

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        message = str(result.exception)
        assert "ghost" in message
        assert "cuda" in message and "keycloak" in message and "livekit" in message

    def test_a_riding_entry_name_is_not_selectable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="livekit")

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "options are" in str(result.exception)

    def test_two_options_of_one_group_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="cuda,rocm")

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "at most one option per group (placeframe)" in str(result.exception)

    def test_shell_environment_overrides_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch)
        monkeypatch.setenv("BUNDLES", "rocm,keycloak")

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output

    def test_stray_selection_in_an_undeclaring_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("BUNDLES=cuda\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "declares no services" in str(result.exception)

    def test_missing_local_output_refuses_naming_generate_score(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        (tmp_path / "stack" / "generated" / "compose" / "compose.cuda.yaml").unlink()
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "generate-score" in str(result.exception)


class TestUpMaterialization:
    def test_root_file_carries_name_includes_door_and_port_stanzas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.setattr(
            compose,
            "bash_output",
            PresentProbeAndServices("api", "grafana", "door", "livekit", "keycloak").run,
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["name"] == "demo"
        assert document["include"] == [
            KEYCLOAK_REF,
            (tmp_path / "stack/generated/compose/compose.cuda.yaml").resolve().as_posix(),
            LIVEKIT_REF,
        ]
        door = document["services"]["door"]
        assert door["image"] == "${CADDY_IMAGE:?err}"
        assert door["ports"] == [{"target": "8443", "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}]
        assert door["environment"] == {
            "FORWARDED_PROTO": "http",
            "FORWARDED_PORT": "58080",
        }
        assert document["services"]["livekit"]["ports"] == [
            {
                "target": "${LIVEKIT_SIGNALING_PORT:?err}",
                "published": "${LIVEKIT_SIGNALING_PORT:?err}",
                "protocol": "tcp",
            }
        ]
        content = document["configs"]["devkit-door-caddyfile"]["content"]
        assert "handle_path /auth/*" in content
        assert "header_up X-Forwarded-Proto {$$FORWARDED_PROTO}" in content
        assert "reverse_proxy grafana:3000" in content
        assert content.rstrip().endswith("\thandle {\n\t\treverse_proxy api:8000\n\t}\n}")

    def test_the_anonymous_edge_omits_the_auth_route_and_auth_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path, bundles="cuda")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.setattr(compose, "bash_output", PresentProbeAndServices("api", "grafana", "door", "livekit").run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        door = document["services"]["door"]
        assert "environment" not in door
        content = document["configs"]["devkit-door-caddyfile"]["content"]
        assert "/auth/*" not in content

    def test_the_door_refuses_without_the_caddy_lock_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        _write_lock(tmp_path, entries={"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST})
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "CADDY_IMAGE" in str(result.exception)

    def test_an_auth_selection_without_public_url_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        (tmp_path / ".env").write_text(
            "BUNDLES=cuda,keycloak\nGATEWAY_PORT=58080\nLIVEKIT_SIGNALING_PORT=6178\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "PUBLIC_URL" in str(result.exception)

    def test_a_local_output_defining_the_door_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        _write_bundle_output(tmp_path, "cuda", ["api", "grafana", "door"])
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "reserved door service" in str(result.exception)


class TestUpValidation:
    def _invoke_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: str):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.setattr(compose, "bash_handoff", _silent_handoff)
        monkeypatch.setattr(compose, "bash_output", ServicesOutput(*services.split()).run)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(compose.up_app, [])

    def test_a_route_upstream_absent_from_the_assembly_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._invoke_up(tmp_path, monkeypatch, "api door livekit keycloak")

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "/grafana/* upstream grafana" in str(result.exception)

    def test_the_selected_auth_upstream_absent_from_the_assembly_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = self._invoke_up(tmp_path, monkeypatch, "api grafana door livekit")

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "route /auth/* upstream keycloak" in str(result.exception)

    def test_a_port_owning_service_absent_from_the_assembly_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = self._invoke_up(tmp_path, monkeypatch, "api grafana door keycloak")

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "livekit" in str(result.exception)


class TestServiceExclusion:
    def test_two_selected_outputs_sharing_a_service_refuse_at_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "gateway": "8080",
                "repository": "ghcr.io/org/demo",
                "services": {
                    "left": {
                        "options": {
                            "a": {"workloads": ["api.yaml"], "routes": [{"path": "/*", "upstream": "api:8000"}]}
                        }
                    },
                    "right": {"options": {"b": {"workloads": ["api.yaml"]}}},
                },
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "a", ["api"])
        _write_bundle_output(tmp_path, "b", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=a,b\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        message = str(result.exception)
        assert "compose.a.yaml" in message and "compose.b.yaml" in message
        assert "api" in message

    def test_the_same_overlap_stays_legal_at_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "gateway": "8080",
                "repository": "ghcr.io/org/demo",
                "services": {
                    "left": {
                        "options": {
                            "a": {"workloads": ["api.yaml"], "routes": [{"path": "/*", "upstream": "api:8000"}]}
                        }
                    },
                    "right": {"options": {"b": {"workloads": ["api.yaml"]}}},
                },
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "a", ["api"])
        _write_bundle_output(tmp_path, "b", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=a,b\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.documents == [{"name": "demo"}]


class TestDownTeardown:
    def test_down_hands_off_a_name_only_root_with_remove_orphans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code == 0, result.output
        assert len(recorder.commands) == 1
        command = recorder.commands[0]
        assert command.startswith("docker compose -f ")
        assert f" --project-directory {tmp_path} --env-file .env --env-file {tmp_path / LOCK_PATH}" in command
        assert command.endswith("down --remove-orphans")
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        assert match is not None
        assert not Path(match.group(1)).exists()
        [document] = recorder.documents
        assert document == {"name": "demo"}

    def test_down_is_identical_under_every_selection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands: list[str] = []
        for bundles in ("cuda,keycloak", "", None):
            tree = tmp_path / f"tree-{len(commands)}"
            tree.mkdir()
            _write_consumer_tree(tree, bundles=bundles)
            monkeypatch.delenv("BUNDLES", raising=False)
            recorder = AssemblyRecorder()
            monkeypatch.setattr(compose, "bash_handoff", recorder.run)
            monkeypatch.chdir(tree)

            result = runner.invoke(compose.down_app, [])

            assert result.exit_code == 0, result.output
            normalized = re.sub(r"\S*devkit-assembly-\S+\.yaml", "ASSEMBLY", recorder.commands[0])
            commands.append(normalized.replace(str(tree), "ROOT"))
        assert commands[0] == commands[1] == commands[2]

    def test_down_volumes_flag_rides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, ["--volumes"])

        assert result.exit_code == 0, result.output
        assert recorder.commands[0].endswith("down --remove-orphans -v")

    def test_down_without_config_uses_the_default_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env down --remove-orphans"
        ]


def _write_dev_tree(
    root: Path,
    *,
    dockerfile: str,
    api_annotations: dict[str, str] | None = None,
    edge_annotations: dict[str, str] | None = None,
) -> None:
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "services": {
                "apps": {
                    "options": {
                        "core": {"workloads": ["api.yaml"]},
                        "edge": {"workloads": ["edge.yaml"]},
                    }
                }
            },
        },
    )
    service_directory = root / "workloads" / "api"
    service_directory.mkdir(parents=True, exist_ok=True)
    (service_directory / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    _write_bundle_output(root, "core", ["api"])
    _write_bundle_output(root, "edge", ["edge"])
    _write_workload(root, "api.yaml", "api", api_annotations)
    _write_workload(root, "edge.yaml", "edge", edge_annotations)
    (root / ".env").write_text("BUNDLES=core\n", encoding="utf-8")


MULTI_STAGE_DOCKERFILE = "FROM ghcr.io/org/base:1 AS dev\nFROM dev AS runtime\n"
SINGLE_STAGE_DOCKERFILE = "FROM ghcr.io/org/base:1 AS dev\n"


class TestDevOverlay:
    def test_dev_stage_service_gets_dev_image_repo_bind_and_toggle_holes(self, tmp_path: Path):
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true,DEBUG_API=false"},
        )

        context = ComposeContext.bring_up(tmp_path, dev=True)

        assert context.dev_overlay is not None
        assert context.compose_files[-1] == context.dev_overlay
        document = yaml.safe_load(context.dev_overlay.read_text(encoding="utf-8"))
        assert document == {
            "services": {
                "api": {
                    "image": "ghcr.io/org/demo/api:dev",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": tmp_path.resolve().as_posix(),
                            "target": "/app",
                            "read_only": True,
                        }
                    ],
                    "environment": {"RELOAD": "${RELOAD:-true}", "DEBUG_API": "${DEBUG_API:-false}"},
                }
            }
        }
        overlay = context.dev_overlay
        assert overlay is not None
        overlay.unlink()
        assembly_root = context.assembly_root
        assert assembly_root is not None
        assembly_root.unlink(missing_ok=True)

    def test_a_single_stage_dev_label_is_not_dev_shape_and_refuses(self, tmp_path: Path):
        _write_dev_tree(tmp_path, dockerfile=SINGLE_STAGE_DOCKERFILE)

        with pytest.raises(RuntimeError, match="no dev shape"):
            ComposeContext.bring_up(tmp_path, dev=True)

    def test_toggle_only_service_gets_environment_holes_without_an_image(self, tmp_path: Path):
        _write_dev_tree(tmp_path, dockerfile=SINGLE_STAGE_DOCKERFILE, api_annotations={DEV_TOGGLES: "RELOAD=true"})

        context = ComposeContext.bring_up(tmp_path, dev=True)

        assert context.dev_overlay is not None
        document = yaml.safe_load(context.dev_overlay.read_text(encoding="utf-8"))
        assert document == {"services": {"api": {"environment": {"RELOAD": "${RELOAD:-true}"}}}}
        overlay = context.dev_overlay
        assert overlay is not None
        overlay.unlink()
        assembly_root = context.assembly_root
        assert assembly_root is not None
        assembly_root.unlink(missing_ok=True)

    def test_overlay_targets_only_services_the_selection_defines(self, tmp_path: Path):
        # api carries the dev shape, but the selection is the edge option alone; a dev
        # overlay keying api would add a half-defined service, so only edge rides.
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true"},
            edge_annotations={DEV_TOGGLES: "DEBUG_EDGE=false"},
        )
        (tmp_path / ".env").write_text("BUNDLES=edge\n", encoding="utf-8")

        context = ComposeContext.bring_up(tmp_path, dev=True)

        assert context.dev_overlay is not None
        document = yaml.safe_load(context.dev_overlay.read_text(encoding="utf-8"))
        assert document == {"services": {"edge": {"environment": {"DEBUG_EDGE": "${DEBUG_EDGE:-false}"}}}}
        overlay = context.dev_overlay
        assert overlay is not None
        overlay.unlink()
        assembly_root = context.assembly_root
        assert assembly_root is not None
        assembly_root.unlink(missing_ok=True)

    def test_up_dev_without_dev_shape_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(tmp_path, dockerfile=SINGLE_STAGE_DOCKERFILE)
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, ["--dev"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "no dev shape" in str(result.exception)

    def test_up_dev_builds_both_shapes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(tmp_path, dockerfile=MULTI_STAGE_DOCKERFILE)
        monkeypatch.delenv("BUNDLES", raising=False)
        builds: list[dict[str, object]] = []

        def _record_build(**kwargs: object) -> None:
            builds.append(dict(kwargs))

        monkeypatch.setattr(compose, "run_build", _record_build)
        monkeypatch.setattr(compose, "bash_handoff", _silent_handoff)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, ["--build", "--dev"])

        assert result.exit_code == 0, result.output
        assert builds == [{}, {"dev": True}]

    def test_up_dev_layers_and_cleans_the_overlay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true"},
        )
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, ["--dev"])

        assert result.exit_code == 0, result.output
        overlay_match = OVERLAY_ARGUMENT_PATTERN.search(recorder.commands[0])
        assert overlay_match is not None
        assert not Path(overlay_match.group(1)).exists()

    @pytest.mark.parametrize(
        "annotation",
        [
            "RELOAD",
            "reload=true",
            "RELOAD=",
            "RELOAD=true,RELOAD=false",
        ],
    )
    def test_malformed_annotations_refuse(self, tmp_path: Path, annotation: str):
        _write_dev_tree(tmp_path, dockerfile=MULTI_STAGE_DOCKERFILE, api_annotations={DEV_TOGGLES: annotation})

        with pytest.raises(RuntimeError):
            ComposeContext.bring_up(tmp_path, dev=True)

    def test_non_string_annotation_refuses(self, tmp_path: Path):
        _write_dev_tree(tmp_path, dockerfile=MULTI_STAGE_DOCKERFILE, api_annotations={DEV_TOGGLES: "RELOAD=true"})
        path = tmp_path / "stack" / "score" / "api.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["metadata"]["annotations"][DEV_TOGGLES] = True
        path.write_text(yaml.safe_dump(document), encoding="utf-8")

        with pytest.raises(RuntimeError, match="NAME=default"):
            ComposeContext.bring_up(tmp_path, dev=True)


class TestLockRequirement:
    def _write_lock_free_tree(self, tmp_path: Path, sections: dict[str, object] | None = None) -> None:
        _write_config(tmp_path, sections)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        (tmp_path / "compose.yml").write_text("services:\n  api:\n    image: example:1\n", encoding="utf-8")

    def test_leftover_manifest_refuses_at_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("services: {}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "manifest dissolved" in str(result.exception)

    def test_lock_free_repo_assembles_without_the_lock_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "enumerate_profiles", _no_profiles)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env down --remove-orphans"
        ]

    def test_present_lock_still_rides_the_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text("ALPINE_IMAGE=alpine:3.20\n", encoding="utf-8")
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.down_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} "
                f"--env-file .env --env-file {tmp_path / LOCK_PATH} down --remove-orphans"
            )
        ]

    def test_lock_free_repo_skips_the_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_lock_free_tree(tmp_path)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -f compose.yml --project-directory {tmp_path} --env-file .env up --pull missing -d"
        ]


class TestMirroredLockEnforcement:
    def _write_mirrored_tree(self, tmp_path: Path, lock_text: str) -> None:
        _write_config(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(lock_text, encoding="utf-8")

    def test_missing_entry_fails_closed_with_the_locked_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "bash_output", _probe_absent)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert str(result.exception) == LOCKED_ERROR

    def test_probe_failure_is_not_reported_as_an_unmirrored_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "bash_output", _probe_unauthorized)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "not mirrored yet" not in message
        assert "not evidence the image is missing" in message
        assert "authentication required" in message
        assert "alpine" in message

    def test_present_entries_proceed_to_bring_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}\n"
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_output", _probe_present)
        monkeypatch.setattr(compose, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            (
                f"docker compose -f compose.yml --project-directory {tmp_path} "
                f"--env-file .env --env-file {tmp_path / LOCK_PATH} up --pull missing -d"
            )
        ]

    def test_reports_the_first_missing_entry_deterministically(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        present = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        absent = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"
        probe = SelectiveProbe(present)
        self._write_mirrored_tree(tmp_path, f"UV_IMAGE={present}\nALPINE_IMAGE={absent}\n")
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.setattr(compose, "bash_output", probe.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(compose.up_app, [])

        assert result.exit_code != 0
        assert "alpine" in str(result.exception)
        assert probe.probed == [absent]


class TestComposePortMergeOverIncludes:
    # The single-root assembly relies on compose merging parent-file port stanzas into
    # services its includes define. Compose 2.24 through 2.39 rejects that shape
    # ("services.<name> conflicts with imported resource"), so the pin runs against a
    # version-pinned standalone binary — the exact release, never whatever the host
    # installed — to keep the behavior assertion deterministic.
    def test_parent_ports_add_to_and_reset_included_ports(self, tmp_path: Path, pinned_compose: str) -> None:
        child = tmp_path / "child.yaml"
        child.write_text(
            'services:\n  svc:\n    image: docker.io/library/alpine:3.20\n    ports:\n      - "18443:8443"\n',
            encoding="utf-8",
        )
        parent = tmp_path / "parent.yaml"
        parent.write_text(
            'include:\n  - ./child.yaml\nservices:\n  svc:\n    ports:\n      - "58080:8443"\n',
            encoding="utf-8",
        )

        rendered = bash_output(f"{pinned_compose} -f {parent} --project-directory {tmp_path} config --format json")
        document = yaml.safe_load(rendered)
        ports = document["services"]["svc"]["ports"]
        assert {port["published"] for port in ports} == {"18443", "58080"}

        reset_parent = tmp_path / "reset-parent.yaml"
        reset_parent.write_text(
            "include:\n  - ./child.yaml\nservices:\n  svc:\n    ports: !reset []\n",
            encoding="utf-8",
        )
        rendered = bash_output(
            f"{pinned_compose} -f {reset_parent} --project-directory {tmp_path} config --format json"
        )
        document = yaml.safe_load(rendered)
        assert "ports" not in document["services"]["svc"]
