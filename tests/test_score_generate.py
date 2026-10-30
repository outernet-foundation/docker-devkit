import os
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from pydantic import ValidationError

from docker_devkit import score_generate
from docker_devkit.config import DockerDevkitConfig
from docker_devkit.documents import BindVolume, ComposeService
from docker_devkit.score_generate import (
    generate_k8s,
    tool_requirements,
    write_generated_file,
)
from docker_devkit.provisioners import compiled_provisioners

DIGEST = "sha256:" + "a" * 64


def _root_config(**sections: object) -> DockerDevkitConfig:
    payload: dict[str, object] = {"requires": ">=0.1"}
    payload.update(sections)
    return DockerDevkitConfig.model_validate(payload)


def test_tool_requirements_skip_score_k8s_for_compose_only() -> None:
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="rathole",
        services={"rathole-client": {"workloads": ["rathole-client.yaml"]}},
    )

    binaries = [item.binary for item in tool_requirements(config, "both")]

    assert binaries == ["score-compose"]


def test_tool_pin_defaults_live_on_the_root_model() -> None:
    config = _root_config(k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}})

    assert config.helm_version == "3.22.1"
    assert config.score_compose_version == "0.42.0"
    assert config.score_k8s_version == "0.15.0"

    overridden = _root_config(
        k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}},
        helm_version="3.23.0",
    )

    assert overridden.helm_version == "3.23.0"


def test_tool_requirements_gate_helm_on_charts_and_target() -> None:
    with_charts = _root_config(k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}})

    assert [item.binary for item in tool_requirements(with_charts, "both")] == ["score-compose", "score-k8s", "helm"]
    assert [item.binary for item in tool_requirements(with_charts, "compose")] == ["score-compose"]

    without_charts = _root_config(k8s={"workloads": ["api.yaml"]})

    assert [item.binary for item in tool_requirements(without_charts, "both")] == ["score-compose", "score-k8s"]


def test_tool_requirements_carry_fetch_facts_for_every_tool() -> None:
    with_charts = _root_config(k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}})

    for item in tool_requirements(with_charts, "both"):
        assert item.version
        assert item.release.startswith("https://")
        assert item.archive.startswith("https://")
        assert item.extract

    helm = tool_requirements(with_charts, "both")[-1]

    assert helm.extract == "--strip-components=1 linux-amd64/helm"


def test_bind_volume_carries_required_source_and_target() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "bind", "source": "a", "target": "/b"}]})

    assert volume.volumes is not None
    assert isinstance(volume.volumes[0], BindVolume)
    assert volume.volumes[0].source == "a"
    assert volume.volumes[0].target == "/b"


def test_malformed_bind_fails_validation_instead_of_passing_through() -> None:
    with pytest.raises(ValidationError):
        ComposeService.model_validate({"volumes": [{"type": "bind", "target": "/b"}]})


def test_non_bind_volume_kind_passes_through_untyped() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "tmpfs", "target": "/cache"}]})

    assert volume.volumes is not None
    assert not isinstance(volume.volumes[0], BindVolume)


def test_write_normalises_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)

    write_generated_file(path, "content\n")

    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text(encoding="utf-8") == "content\n"


WORKLOAD_MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: workload\n"
HELM_RENDERED = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: chart\n"
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
CHART_REF = "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"
MIRRORED_CHART = f"{MIRROR_PREFIX}/ghcr.io/grafana/helm-charts/loki@{DIGEST}"
LOCK_IMAGES = {
    "GRAFANA_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/grafana/grafana:11.6.0@" + DIGEST,
    "LOKI_CHART": MIRRORED_CHART,
}
VALUES_PATH = Path("stack/score/k8s/values/loki.yaml")


class K8sBashRecorder:
    def __init__(self, output_path: Path, state_path: Path) -> None:
        self.output_path = output_path
        self.state_path = state_path
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: Path | None = None) -> None:
        self.commands.append(command)
        if command.startswith("score-k8s generate"):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(WORKLOAD_MANIFEST, encoding="utf-8")
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text("workloads: {}\n", encoding="utf-8")


class HelmRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.values_contents: list[str] = []

    def run(self, command: str) -> str:
        self.commands.append(command)
        for argument in command.split("--values ")[1:]:
            self.values_contents.append(Path(argument.split()[0]).read_text(encoding="utf-8"))
        return HELM_RENDERED


def _charts_config() -> DockerDevkitConfig:
    return _root_config(k8s={"charts": {"loki": CHART_REF}})


def _write_values(tmp_path: Path, text: str) -> None:
    path = tmp_path / VALUES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _generate_with_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: DockerDevkitConfig, images: dict[str, str]
) -> HelmRecorder:
    output = Path("stack/generated/k8s/manifests.yaml")
    k8s = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    helm = HelmRecorder()
    monkeypatch.setattr(score_generate, "bash", k8s.run)
    monkeypatch.setattr(score_generate, "bash_output", helm.run)
    generate_k8s(tmp_path, config, output, images)
    return helm


def test_values_without_markers_pass_through_as_the_committed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_values(tmp_path, "service:\n  port: 3000\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    assert helm.commands == [
        (f"helm template loki oci://{MIRRORED_CHART} --values {(tmp_path / VALUES_PATH).as_posix()}")
    ]
    assert helm.values_contents == ["service:\n  port: 3000\n"]


def test_a_missing_chart_lock_entry_refuses_naming_the_chart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "service:\n  port: 3000\n")
    config = _charts_config()

    with pytest.raises(RuntimeError, match=r"chart 'loki'.*LOKI_CHART"):
        _generate_with_chart(monkeypatch, tmp_path, config, {})


def test_values_with_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources").mkdir(parents=True)
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").write_text(
        "apiVersion: 1\ndatasources:\n  - name: Loki\n    url: http://loki:3100\n",
        encoding="utf-8",
    )
    _write_values(
        tmp_path,
        "datasources:\n  datasources.yaml:\n    include-yaml: workloads/grafana/provisioning/datasources/datasources.yaml\n",
    )
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    argument = helm.commands[0].split("--values ")[1].split()[0]
    assert argument != (tmp_path / VALUES_PATH).as_posix()
    assert argument.endswith("values/loki.yaml")
    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["datasources"]["datasources.yaml"] == {
        "apiVersion": 1,
        "datasources": [{"name": "Loki", "url": "http://loki:3100"}],
    }


def test_text_markers_in_values_dump_as_literal_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "loki").mkdir(parents=True)
    (tmp_path / "workloads" / "loki" / "config.yaml").write_text(
        "storage:\n  s3:\n    access_key_id: ${S3_ACCESS_KEY}\n",
        encoding="utf-8",
    )
    _write_values(tmp_path, "loki:\n  config:\n    include: workloads/loki/config.yaml\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    assert "config: |" in helm.values_contents[0]
    assert "${S3_ACCESS_KEY}" in helm.values_contents[0]


def test_values_with_lock_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "image:\n  lock-image:\n    key: GRAFANA_IMAGE\n    digest_field: sha\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["registry"] == "ghcr.io/outernet-foundation/mirror/docker.io/grafana"
    assert resolved["image"]["repository"] == "grafana"
    assert resolved["image"]["tag"] == "11.6.0"
    assert resolved["image"]["sha"] == DIGEST.removeprefix("sha256:")


def test_values_lock_markers_resolve_after_includes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads").mkdir()
    (tmp_path / "workloads" / "images.yaml").write_text(
        "image:\n  lock-image: GRAFANA_IMAGE\n",
        encoding="utf-8",
    )
    _write_values(tmp_path, "include-yaml: workloads/images.yaml\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["digest"] == DIGEST


class ComposeBashRecorder:
    def __init__(self, generated: str) -> None:
        self.generated = generated
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: Path | None = None) -> None:
        self.commands.append(command)
        if command.startswith("score-compose generate"):
            output = Path(command.rsplit("--output ", 1)[1].split()[0])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(self.generated, encoding="utf-8")


GENERATED_COMPOSE = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: workloads/api/config.yaml\n"
    "        target: /etc/placeframe/config.yaml\n"
    "  pf-postgres:\n"
    "    image: pg:16\n"
)


def _generate_compose(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generated: str) -> Path:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("containers: {}\n", encoding="utf-8")
    (tmp_path / "workloads" / "api").mkdir(parents=True)
    (tmp_path / "workloads" / "api" / "config.yaml").write_text("key: ${FOO}\n", encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={"core": {"workloads": ["api.yaml"]}},
    )

    score_generate.generate_compose(tmp_path, config, {})
    return tmp_path / "stack" / "generated" / "compose" / "compose.core.yaml"


def test_generate_compose_collapses_doubled_names_and_inlines_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GENERATED_COMPOSE)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["api", "pf-postgres"]
    api = document["services"]["api"]
    assert api["image"] == "ghcr.io/org/api:1"
    assert "volumes" not in api
    assert api["configs"] == [{"source": "config.yaml", "target": "/etc/placeframe/config.yaml"}]
    assert document["configs"] == {"config.yaml": {"content": "key: $${FOO}\n"}}


def test_generate_compose_collapses_doubled_names_with_no_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  gateway-gateway:\n    image: ghcr.io/org/gateway:1\n",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["gateway"]
    assert document["services"]["gateway"]["image"] == "ghcr.io/org/gateway:1"
    assert "configs" not in document


def test_generate_compose_refuses_a_rename_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    colliding = GENERATED_COMPOSE + "  api:\n    image: ghcr.io/org/api:2\n"

    with pytest.raises(SystemExit, match="collides"):
        _generate_compose(monkeypatch, tmp_path, colliding)


def test_generate_compose_leaves_undoubled_names_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  tunnel-rathole-rathole-client:\n    image: ghcr.io/org/rathole-client:1\n",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["tunnel-rathole-rathole-client"]


GATED_COMPOSE = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    depends_on:\n"
    "      wait-for-resources:\n"
    "        condition: service_started\n"
    "  pf-postgres:\n"
    "    image: pg:16\n"
    "    depends_on: [wait-for-resources]\n"
    "  wait-for-resources:\n"
    "    image: ghcr.io/org/alpine:1\n"
)


def test_generate_compose_renames_the_gate_and_its_references_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GATED_COMPOSE)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["api", "pf-postgres", "wait-for-resources-core"]
    assert document["services"]["api"]["depends_on"] == {"wait-for-resources-core": {"condition": "service_started"}}
    assert document["services"]["pf-postgres"]["depends_on"] == ["wait-for-resources-core"]


def test_generate_compose_gate_names_differ_per_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("containers: {}\n", encoding="utf-8")
    (tmp_path / "stack" / "score" / "edge.yaml").write_text("containers: {}\n", encoding="utf-8")
    recorder = ComposeBashRecorder(GATED_COMPOSE)
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={
            "core": {"workloads": ["api.yaml"]},
            "edge": {"workloads": ["edge.yaml"]},
        },
    )

    score_generate.generate_compose(tmp_path, config, {})

    core = yaml.safe_load((tmp_path / "stack/generated/compose/compose.core.yaml").read_text(encoding="utf-8"))
    edge = yaml.safe_load((tmp_path / "stack/generated/compose/compose.edge.yaml").read_text(encoding="utf-8"))
    assert "wait-for-resources-core" in core["services"]
    assert "wait-for-resources-edge" in edge["services"]


def test_generate_compose_refuses_a_gate_rename_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    colliding = GATED_COMPOSE + "  wait-for-resources-core:\n    image: ghcr.io/org/api:2\n"

    with pytest.raises(SystemExit, match="collides"):
        _generate_compose(monkeypatch, tmp_path, colliding)


class WorkloadContentsRecorder:
    def __init__(self) -> None:
        self.workload_contents: dict[str, str] = {}

    def run(self, command: str, *, cwd: Path | None = None) -> None:
        if command.startswith("score-compose generate"):
            for argument in command.split()[2:]:
                if argument.startswith("--output"):
                    break
                path = Path(argument)
                self.workload_contents[path.name] = path.read_text(encoding="utf-8")
            output = Path(command.rsplit("--output ", 1)[1].split()[0])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("services: {}\n", encoding="utf-8")


def test_workload_lock_markers_resolve_into_the_rendered_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "containers:\n  api:\n    image:\n      lock: API_IMAGE\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={"core": {"workloads": ["api.yaml"]}},
    )

    score_generate.generate_compose(tmp_path, config, {"API_IMAGE": "ghcr.io/org/api@sha256:" + "a" * 64})

    assert (
        recorder.workload_contents["api.yaml"]
        == "containers:\n  api:\n    image: ghcr.io/org/api@sha256:" + "a" * 64 + "\n"
    )


def test_workload_lock_marker_naming_no_lock_entry_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "containers:\n  api:\n    image:\n      lock: API_IMAGE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(score_generate, "bash", WorkloadContentsRecorder().run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={"core": {"workloads": ["api.yaml"]}},
    )

    with pytest.raises(RuntimeError, match=r"workload api\.yaml.*API_IMAGE.*does not exist"):
        score_generate.generate_compose(tmp_path, config, {})


SHIPPED_TEMPLATES = {
    "wait-for-resources-image.tpl",
    "probe-adapter-healthchecks.tpl",
    "restart.tpl",
    "gpu-cuda.tpl",
    "gpu-rocm.tpl",
    "gpu-pod.tpl",
    "scale-to-zero.tpl",
}


def test_shipped_patch_templates_resolve() -> None:
    directory = files("docker_devkit") / "patch_templates"

    assert {item.name for item in directory.iterdir()} == SHIPPED_TEMPLATES
    for name in SHIPPED_TEMPLATES:
        assert (directory / name).read_text(encoding="utf-8").strip()


SELF_GATING_FALLBACK = "{{- if not $matched }}\n[]\n{{- end }}"


def test_self_gating_templates_fall_back_to_an_empty_patches_document() -> None:
    # score-compose and score-k8s refuse a document-less template output (comment-only or
    # empty renders fail at unmarshal), so a template that can emit zero ops must end with
    # the empty-list fallback and set the matched flag where it emits ops. The gate-pin
    # template emits unconditionally and is exempt.
    directory = files("docker_devkit") / "patch_templates"

    for name in SHIPPED_TEMPLATES - {"wait-for-resources-image.tpl"}:
        text = (directory / name).read_text(encoding="utf-8")
        assert SELF_GATING_FALLBACK in text
        assert "{{- $matched = true }}" in text


def test_the_healthcheck_template_ships_both_probe_adapter_spells() -> None:
    text = (files("docker_devkit") / "patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert score_generate.PROBE_ADAPTERS == ("python-urllib", "bash-http")
    assert "urllib.request.urlopen" in text
    assert "/dev/tcp/127.0.0.1" in text


def test_the_bash_http_spell_escapes_its_dollar_for_compose_interpolation() -> None:
    # The healthcheck test string is app-owned data the consumer's include-time
    # interpolation scans: an un-doubled $status renders as an empty compose
    # variable and the case statement dies as a syntax error (keycloak verified
    # unhealthy on a live daemon exactly this way), so the spell must carry the
    # compose-escaped $$status.
    text = (files("docker_devkit") / "patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert "$$status" in text
    assert "$status" not in text.replace("$$status", "")


def test_the_bash_http_spell_uses_the_slash_port_spelling() -> None:
    # bash intercepts /dev/tcp/host/port — slash only. A colon-separated port
    # (127.0.0.1:9000) is not a net redirection at all: bash falls through to
    # opening the literal file and the healthcheck dies with ENOENT (verified
    # against the live keycloak container — the colon form failed while the
    # slash form connected on the same socket).
    text = (files("docker_devkit") / "patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert "/dev/tcp/127.0.0.1/{{ .port }}" in text
    assert "/dev/tcp/127.0.0.1:{{ .port }}" not in text


PROVISIONER_WITH_SERVICES = """
- uri: template://example/config
  type: example-config
  services:
    example-config:
      image: ${EXAMPLE_IMAGE:?err}
"""

PROVISIONER_WITHOUT_SERVICES = """
- uri: template://example/state
  type: example-state
  state:
    key: value
"""

GATE_LOCK = {"ALPINE_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.21.0@" + DIGEST}


def _bundle_config(**bundle: object) -> DockerDevkitConfig:
    return _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={"core": {"workloads": ["api.yaml"], **bundle}},  # type: ignore[arg-type]
    )


def _compose_init_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: DockerDevkitConfig,
    images: dict[str, str],
    sources: dict[str, str],
) -> list[list[str]]:
    # One template-name list per bundle rendering, in declaration order.
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    recorder = ComposeBashRecorder("services: {}\n")
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    score_generate.generate_compose(tmp_path, config, images)
    inits = [command for command in recorder.commands if command.startswith("score-compose init")]
    return [[Path(token).name for token in command.split() if token.endswith(".tpl")] for command in inits]


WORKLOAD_REFERENCING_CONFIG = """
resources:
  config:
    type: example-config
containers: {}
"""

WORKLOAD_REFERENCING_STATE = """
resources:
  state:
    type: example-state
containers: {}
"""


def test_gate_pin_registers_when_provisioners_declare_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        GATE_LOCK,
        {
            "stack/score/api.yaml": WORKLOAD_REFERENCING_CONFIG,
            "stack/score/compose/config.yaml": PROVISIONER_WITH_SERVICES,
        },
    )

    assert templates == [
        ["probe-adapter-healthchecks.tpl", "restart.tpl", "wait-for-resources-image.tpl"],
    ]


def test_gate_pin_stays_unregistered_without_provisioner_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": WORKLOAD_REFERENCING_STATE,
            "stack/score/compose/state.yaml": PROVISIONER_WITHOUT_SERVICES,
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_gate_pin_refuses_a_gate_forming_run_without_the_lock_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    with pytest.raises(RuntimeError, match="ALPINE_IMAGE"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": WORKLOAD_REFERENCING_CONFIG,
                "stack/score/compose/config.yaml": PROVISIONER_WITH_SERVICES,
            },
        )


def test_provisioners_register_by_resource_type_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        "stack/score/api.yaml": WORKLOAD_REFERENCING_STATE,
        "stack/score/gateway.yaml": WORKLOAD_REFERENCING_CONFIG,
        "stack/score/compose/config.yaml": PROVISIONER_WITH_SERVICES,
        "stack/score/compose/state.yaml": PROVISIONER_WITHOUT_SERVICES,
        "stack/score/compose/orphan.yaml": "- uri: template://example/orphan\n  type: orphan-type\n",
    }
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    config = _bundle_config(workloads=["api.yaml", "gateway.yaml"])
    recorder = ComposeBashRecorder("services: {}\n")
    monkeypatch.setattr(score_generate, "bash", recorder.run)

    score_generate.generate_compose(tmp_path, config, GATE_LOCK)

    init = next(command for command in recorder.commands if command.startswith("score-compose init"))
    provisioners = [token for token in init.split() if token.endswith(".yaml")]
    # Both referenced files compile (deterministic filename order); the orphan — a type
    # nothing references — silently no-ships.
    assert [Path(token).name for token in provisioners] == ["config.yaml", "state.yaml"]


WORKLOAD_WITH_LIVENESS = """
containers:
  api:
    image: ghcr.io/org/api:1
    livenessProbe:
      httpGet:
        path: /health
        port: 5678
"""

WORKLOAD_WITH_READINESS = """
metadata:
  annotations:
    outernet.internal/probe-adapter: python-urllib
containers:
  api:
    image: ghcr.io/org/api:1
    readinessProbe:
      httpGet:
        path: /ready
        port: 5678
"""

WORKLOAD_WITH_UNADAPTED_READINESS = """
containers:
  api:
    image: ghcr.io/org/api:1
    readinessProbe:
      httpGet:
        path: /ready
        port: 5678
"""


def test_httpget_probe_without_the_adapter_annotation_refuses_naming_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    with pytest.raises(SystemExit, match=r"api\.yaml.*readinessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_UNADAPTED_READINESS,
            },
        )

    with pytest.raises(SystemExit, match=r"api\.yaml.*livenessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_LIVENESS,
            },
        )


def test_unknown_probe_adapter_values_refuse_naming_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    with pytest.raises(SystemExit, match=r"api\.yaml.*supported adapters"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": "metadata:\n  annotations:\n    outernet.internal/probe-adapter: curl\ncontainers: {}\n",
            },
        )


def test_adapted_probe_bearing_workloads_register_the_healthcheck_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": WORKLOAD_WITH_READINESS,
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_restart_values_outside_the_compose_enum_refuse_naming_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    with pytest.raises(SystemExit, match=r"api\.yaml.*restart policies"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": (
                    "metadata:\n  annotations:\n    outernet.internal/restart: always-please\ncontainers: {}\n"
                ),
            },
        )


def test_restart_annotation_with_an_enum_value_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": (
                "metadata:\n  annotations:\n    outernet.internal/restart: on-failure\ncontainers: {}\n"
            ),
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_publish_annotation_refuses_naming_its_death(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(SystemExit, match=r"api\.yaml.*outernet\.internal/publish.*died"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": (
                    'metadata:\n  annotations:\n    outernet.internal/publish: "8000/8000"\ncontainers: {}\n'
                ),
            },
        )


def test_consumer_interpolated_names_must_match_declared_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(SystemExit, match=r"api\.yaml.*files:"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": (
                    "metadata:\n"
                    "  annotations:\n"
                    "    outernet.internal/consumer-interpolated: /etc/rathole/client.toml\n"
                    "containers: {}\n"
                ),
            },
        )


WORKLOAD_WITH_INTERPOLATED_FILE = """\
metadata:
  annotations:
    outernet.internal/consumer-interpolated: /etc/rathole/client.toml
containers:
  rathole-client:
    image: ghcr.io/org/rathole:1
    files:
      /etc/rathole/client.toml:
        content: |
          remote_addr = "$${TUNNEL_CONTROL_ENDPOINT:?err}"
"""

INTERPOLATED_GENERATED_COMPOSE = (
    "services:\n"
    "  rathole-client-rathole-client:\n"
    "    image: ghcr.io/org/rathole:1\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: .score-compose/mounts/files/rathole-client-files-client.toml\n"
    "        target: /etc/rathole/client.toml\n"
)


class MaterializingComposeRecorder(ComposeBashRecorder):
    # score-compose materialises files: content into its state dir at generate; the
    # recorder recreates that so the inliner can read it (the per-bundle state-dir
    # reset deletes anything pre-created).
    def __init__(self, generated: str, materialized: Path, text: str) -> None:
        super().__init__(generated)
        self.materialized = materialized
        self.text = text

    def run(self, command: str, *, cwd: Path | None = None) -> None:
        super().run(command, cwd=cwd)
        if command.startswith("score-compose generate"):
            self.materialized.parent.mkdir(parents=True, exist_ok=True)
            self.materialized.write_text(self.text, encoding="utf-8")


def test_consumer_interpolated_binds_ride_undoubled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    score_dir = tmp_path / "stack" / "score"
    score_dir.mkdir(parents=True)
    (score_dir / "rathole-client.yaml").write_text(WORKLOAD_WITH_INTERPOLATED_FILE, encoding="utf-8")
    recorder = MaterializingComposeRecorder(
        INTERPOLATED_GENERATED_COMPOSE,
        score_dir / ".score-compose/mounts/files/rathole-client-files-client.toml",
        'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n',
    )
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={"rathole-client": {"workloads": ["rathole-client.yaml"]}},
    )

    score_generate.generate_compose(tmp_path, config, {})

    output = tmp_path / "stack" / "generated" / "compose" / "compose.rathole-client.yaml"
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    service = document["services"]["rathole-client"]
    assert service["configs"] == [{"source": "rathole-client-files-client.toml", "target": "/etc/rathole/client.toml"}]
    assert (
        document["configs"]["rathole-client-files-client.toml"]["content"]
        == 'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n'
    )


def test_gpu_compose_templates_register_per_the_renderings_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        services={
            "core-cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
            "core-rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
        },
    )

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": "containers: {}\n",
        },
    )

    assert templates == [
        ["probe-adapter-healthchecks.tpl", "restart.tpl", "gpu-cuda.tpl"],
        ["probe-adapter-healthchecks.tpl", "restart.tpl", "gpu-rocm.tpl"],
    ]


def test_k8s_builtin_templates_register_on_every_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("containers: {}\n", encoding="utf-8")
    output = Path("stack/generated/k8s/manifests.yaml")
    recorder = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    monkeypatch.setattr(score_generate, "bash", recorder.run)
    monkeypatch.setattr(score_generate, "bash_output", HelmRecorder().run)
    config = _root_config(k8s={"workloads": ["api.yaml"]})

    generate_k8s(tmp_path, config, output, {})

    init = next(command for command in recorder.commands if command.startswith("score-k8s init"))
    assert [Path(token).name for token in init.split() if token.endswith(".tpl")] == [
        "gpu-pod.tpl",
        "scale-to-zero.tpl",
    ]


# The A0 spike verdict, pinned against the real binary: the suite runs without the
# score tools, so these tests skip wherever score-compose is absent or off-pin.
A0_ESCAPED_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ${SPIKE_IMAGE:?err}
    files:
      /etc/spike/holes.toml:
        content: |
          doubled_err = "$${SPIKE_SET:?err}"
          doubled_default = "$${SPIKE_UNSET:-fallback}"
          doubled_bare = "$${SPIKE_SET}"
          lone_dollar = "$$"
          dotted = "${metadata.name}"
"""

A0_MATERIALIZED = (
    'doubled_err = "${SPIKE_SET:?err}"\n'
    'doubled_default = "${SPIKE_UNSET:-fallback}"\n'
    'doubled_bare = "${SPIKE_SET}"\n'
    'lone_dollar = "$"\n'
    'dotted = "spike"\n'
)

A0_ENV_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ${SPIKE_BARE_SET}
    variables:
      DOUBLED_ERR: $${SPIKE_SET:?err}
      DOUBLED_DEFAULT: $${SPIKE_UNSET:-fallback}
"""

A0_REFUSED_FILES_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ghcr.io/example/spike:1.0
    files:
      /etc/spike/holes.toml:
        content: |
          err = "${SPIKE_SET:?err}"
"""

A0_REFUSED_VARIABLES_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ghcr.io/example/spike:1.0
    variables:
      CONTROL: ${SPIKE_SET}
"""


def _skip_unless_pinned_score_compose() -> None:
    if shutil.which("score-compose") is None:
        pytest.skip("score-compose is not on PATH; the A0 pin needs the pinned binary")
    pinned = DockerDevkitConfig.model_fields["score_compose_version"].default
    if f"score-compose {pinned} (" not in bash_output("score-compose version"):
        pytest.skip(f"score-compose on PATH is not the pinned {pinned}")


def test_files_placeholders_materialize_undoubled(tmp_path: Path) -> None:
    _skip_unless_pinned_score_compose()
    (tmp_path / "escaped.yaml").write_text(A0_ESCAPED_WORKLOAD, encoding="utf-8")
    bash("score-compose init --project spike --no-sample", cwd=tmp_path)
    bash(
        "score-compose generate escaped.yaml --output escaped-compose.yaml",
        cwd=tmp_path,
        env={**os.environ, "SPIKE_SET": "hello", "SPIKE_IMAGE": "ghcr.io/example/spike:1.0"},
    )

    materialized = tmp_path / ".score-compose/mounts/files/spike-files-holes.toml"
    assert materialized.read_text(encoding="utf-8") == A0_MATERIALIZED

    artifact = yaml.safe_load((tmp_path / "escaped-compose.yaml").read_text(encoding="utf-8"))
    service = artifact["services"]["spike-spike"]
    assert service["image"] == "${SPIKE_IMAGE:?err}"
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": ".score-compose/mounts/files/spike-files-holes.toml",
            "target": "/etc/spike/holes.toml",
        }
    ]


def test_variable_placeholders_refuse_or_stay_escaped(tmp_path: Path) -> None:
    _skip_unless_pinned_score_compose()
    (tmp_path / "env.yaml").write_text(A0_ENV_WORKLOAD, encoding="utf-8")
    (tmp_path / "refused-files.yaml").write_text(A0_REFUSED_FILES_WORKLOAD, encoding="utf-8")
    (tmp_path / "refused-variables.yaml").write_text(A0_REFUSED_VARIABLES_WORKLOAD, encoding="utf-8")
    bash("score-compose init --project spike --no-sample", cwd=tmp_path)
    bash(
        "score-compose generate env.yaml --output env-compose.yaml",
        cwd=tmp_path,
        env={**os.environ, "SPIKE_BARE_SET": "ghcr.io/example/set:2.0"},
    )

    service = yaml.safe_load((tmp_path / "env-compose.yaml").read_text(encoding="utf-8"))["services"]["spike-spike"]
    assert service["image"] == "${SPIKE_BARE_SET}"
    assert service["environment"] == {
        "DOUBLED_ERR": "$${SPIKE_SET:?err}",
        "DOUBLED_DEFAULT": "$${SPIKE_UNSET:-fallback}",
    }

    with pytest.raises(CalledProcessError):
        bash("score-compose generate refused-files.yaml --output refused.yaml", cwd=tmp_path)
    with pytest.raises(CalledProcessError):
        bash("score-compose generate refused-variables.yaml --output refused.yaml", cwd=tmp_path)


ORDER_INDEPENDENCE_PROVISIONER = """\
- uri: template://example/order
  type: order-provisioner
  services:
    order-service:
      image: ghcr.io/example/service:1
"""

ORDER_INDEPENDENCE_SECOND_PROVISIONER = """\
- uri: template://example/other
  type: other-provisioner
  services:
    other-service:
      image: ghcr.io/example/other:1
"""


def test_provisioner_registration_order_does_not_change_generated_output(tmp_path: Path) -> None:
    # Provisioner registration derives by closure in deterministic filename order; the
    # committed artifacts were generated under the old config-declared order. This pins
    # that score-compose's output is order-independent, wherever the pinned binary exists.
    # The fixtures ride the real compile path (authored mappings -> string form) and the
    # workload references both types, so both provisioners actually instantiate.
    _skip_unless_pinned_score_compose()
    source = tmp_path / "score"
    source.mkdir()
    (source / "first.yaml").write_text(ORDER_INDEPENDENCE_PROVISIONER, encoding="utf-8")
    (source / "second.yaml").write_text(ORDER_INDEPENDENCE_SECOND_PROVISIONER, encoding="utf-8")
    workload = tmp_path / "workload.yaml"
    workload.write_text(
        "apiVersion: score.dev/v1b1\n"
        "metadata:\n"
        "  name: order\n"
        "containers:\n"
        "  order:\n"
        "    image: ghcr.io/example/order:1\n"
        "resources:\n"
        "  first:\n"
        "    type: order-provisioner\n"
        "  second:\n"
        "    type: other-provisioner\n",
        encoding="utf-8",
    )

    outputs: dict[str, str] = {}
    with compiled_provisioners(source, ["first.yaml", "second.yaml"], tmp_path, {}) as compiled:
        registrations = {
            "forward": compiled.paths,
            "reversed": list(reversed(compiled.paths)),
        }
        for label, paths in registrations.items():
            sandbox = tmp_path / label
            sandbox.mkdir()
            flags = "".join(f" --provisioners {path.as_posix()}" for path in paths)
            bash(f"score-compose init --project order --no-sample{flags}", cwd=sandbox)
            bash(f"score-compose generate {workload.as_posix()} --output {sandbox.as_posix()}/out.yaml", cwd=sandbox)
            outputs[label] = (sandbox / "out.yaml").read_text(encoding="utf-8")

    assert "order-service" in outputs["forward"]
    assert outputs["forward"] == outputs["reversed"]
