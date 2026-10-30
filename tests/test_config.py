import importlib.metadata
from typing import Any
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit.config import (
    DEV_SENTINEL,
    LEFTOVER_LOCK_PATH,
    LEFTOVER_MANIFEST_PATH,
    LOCK_PATH,
    DockerDevkitConfig,
    ServiceEntry,
    compose_bundles,
    derive_services,
    load_config,
)

DIGEST = "sha256:" + "a" * 64


def write_config(tmp_path: Path, payload: dict[str, object]) -> None:
    (tmp_path / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def write_lock(tmp_path: Path, entries: dict[str, str] | None = None) -> None:
    (tmp_path / LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    lines = entries or {"ALPINE_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.21.0@" + DIGEST}
    (tmp_path / LOCK_PATH).write_text(
        "".join(f"{name}={reference}\n" for name, reference in lines.items()), encoding="utf-8"
    )


def write_dockerfile(tmp_path: Path, service: str, text: str, top: str = "workloads") -> None:
    directory = tmp_path / top / service
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def test_load_config_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert load_config(tmp_path) is None


def _installed_version(version: str) -> Any:
    def _version(_name: str) -> str:
        return version

    return _version


class TestRequires:
    def test_omitted_is_required(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"project_name": "example"})

        with pytest.raises(ValidationError, match="requires"):
            load_config(tmp_path)

    def test_empty_string_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ""})

        with pytest.raises(ValidationError, match="requires"):
            load_config(tmp_path)

    def test_invalid_specifier_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": "not-a-specifier"})

        with pytest.raises(ValidationError, match="requires"):
            load_config(tmp_path)

    def test_dev_sentinel_skips_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version(DEV_SENTINEL))
        write_config(tmp_path, {"requires": ">=0.1"})

        config = load_config(tmp_path)

        assert config is not None and config.requires == ">=0.1"

    def test_in_range_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.18"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = load_config(tmp_path)

        assert config is not None and config.requires == ">=0.1.18"

    def test_dev_channel_prerelease_satisfies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.19.dev5"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = load_config(tmp_path)

        assert config is not None

    def test_out_of_range_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.17"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        with pytest.raises(SystemExit, match="does not satisfy requires"):
            load_config(tmp_path)


def test_load_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    (tmp_path / "docker-devkit.yaml").write_text('requires: ">=0.1"\nrequires: ">=0.2"\n', encoding="utf-8")

    with pytest.raises(Exception, match=r"Duplicate key"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": ">=0.1", "lifecycle_prefix": "ghcr.io/x/mirror"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_config(tmp_path)


class TestOldSectionsDied:
    def test_generate_score_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "generate-score": {"project_name": "example"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            load_config(tmp_path)

    def test_publish_compose_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "publish-compose": {"repository": "ghcr.io/org/repo"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            load_config(tmp_path)

    def test_lifecycle_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "lifecycle": {"files": ["compose.yml"]}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            load_config(tmp_path)

    def test_bundles_key_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "bundles": {"core": {"workloads": ["api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            load_config(tmp_path)


class TestBaseImages:
    def test_tag_and_digest_forms_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15", "API_IMAGE": "ghcr.io/org/api@" + DIGEST},
            },
        )
        write_lock(tmp_path)

        config = load_config(tmp_path)

        assert config is not None
        assert config.base_images == {
            "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
            "API_IMAGE": "ghcr.io/org/api@" + DIGEST,
        }

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine"}})

        with pytest.raises(ValidationError, match="path:tag"):
            load_config(tmp_path)

    def test_env_hole_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"ALPINE_IMAGE": "${ALPINE_REF}"}})

        with pytest.raises(ValidationError, match="path:tag"):
            load_config(tmp_path)

    def test_lowercase_key_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"uv_image": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(ValidationError, match="ALL_CAPS"):
            load_config(tmp_path)


class TestCharts:
    def test_tag_form_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"], "charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}},
            },
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        config = load_config(tmp_path)

        assert config is not None and config.k8s is not None
        assert config.k8s.charts == {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}

    def test_digest_form_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST}}},
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        config = load_config(tmp_path)

        assert config is not None and config.k8s is not None
        assert config.k8s.rendering

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki"}}}
        )

        with pytest.raises(ValidationError, match="tag or a digest"):
            load_config(tmp_path)

    def test_non_oci_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "ghcr.io/grafana/helm-charts/loki:7.3.0"}}}
        )

        with pytest.raises(ValidationError, match="upstream-native OCI"):
            load_config(tmp_path)

    def test_env_hole_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:${LOKI_VERSION}"}},
            },
        )

        with pytest.raises(ValidationError, match="tag or a digest"):
            load_config(tmp_path)

    def test_missing_values_file_refuses_at_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        write_lock(tmp_path)

        with pytest.raises(RuntimeError, match=r"values/loki\.yaml.*derived path"):
            load_config(tmp_path)


class TestK8sSection:
    def test_workloads_vars_and_charts_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
            },
        )

        config = load_config(tmp_path)

        assert config is not None and config.k8s is not None
        assert config.k8s.workloads == ["api.yaml"]
        assert config.k8s.vars == {"GPU_VARIANT": "cuda"}

    def test_empty_section_rejected(self, tmp_path: Path) -> None:
        # strictyaml hands a bare section key an empty string, so an empty k8s section is
        # only spellable as the bare key — which refuses shape validation.
        (tmp_path / "docker-devkit.yaml").write_text('requires: ">=0.1"\nk8s:\n', encoding="utf-8")

        with pytest.raises(ValidationError, match="Input should be a valid dictionary"):
            load_config(tmp_path)

    def test_vars_only_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"vars": {"GPU_VARIANT": "cuda"}}})

        with pytest.raises(ValidationError, match="neither workloads nor charts"):
            load_config(tmp_path)


class TestServices:
    def test_groups_options_and_bare_entries_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "gateway": "${GATEWAY_PORT:?err}",
                "services": {
                    "auth": {
                        "options": {
                            "keycloak": {
                                "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
                                "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                            }
                        },
                    },
                    "placeframe": {
                        "options": {
                            "cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
                            "rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
                        },
                        "routes": [{"path": "/*", "upstream": "api:8000"}],
                    },
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/livekit@" + DIGEST,
                        "routes": [{"path": "/livekit-token/*", "upstream": "livekit-token:8000"}],
                        "ports": ["${LIVEKIT_SIGNALING_PORT:?err}/${LIVEKIT_SIGNALING_PORT:?err}/tcp"],
                    },
                },
            },
        )

        config = load_config(tmp_path)

        assert config is not None
        bundles = compose_bundles(config)
        assert [bundle.name for bundle in bundles] == ["cuda", "rocm"]
        assert bundles[0].output == Path("stack/generated/compose/compose.cuda.yaml")
        assert bundles[0].vars == {"GPU_VARIANT": "cuda"}
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert livekit.remote
        assert livekit.ports[0].service == "livekit"

    def test_an_entry_carrying_both_sources_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {
                    "both": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "workloads": ["api.yaml"],
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="exactly one source"):
            load_config(tmp_path)

    def test_an_entry_carrying_neither_source_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"neither": {"routes": [{"path": "/*", "upstream": "api:8000"}]}},
            },
        )

        with pytest.raises(ValidationError, match="neither ref: nor workloads:"):
            load_config(tmp_path)

    def test_vars_beside_a_ref_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {
                    "remote": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "vars": {"GPU_VARIANT": "cuda"},
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="a ref carries its producer's vars"):
            load_config(tmp_path)

    def test_an_unpinned_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "oci://ghcr.io/org/repo/x:main"}},
            },
        )

        with pytest.raises(ValidationError, match="digest-pinned OCI include"):
            load_config(tmp_path)

    def test_an_empty_workloads_list_refuses(self) -> None:
        with pytest.raises(ValidationError, match="at least one workload"):
            DockerDevkitConfig.model_validate({
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"empty": {"workloads": []}},
            })

    def test_services_require_project_name(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}}},
        )

        with pytest.raises(ValidationError, match="project_name is required"):
            load_config(tmp_path)

    def test_local_entries_require_repository(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"core": {"workloads": ["api.yaml"]}},
            },
        )

        with pytest.raises(ValidationError, match="repository is required"):
            load_config(tmp_path)

    def test_remote_only_services_need_no_repository(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/livekit@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert load_config(tmp_path) is not None


class TestFlatVocabulary:
    def _config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": services,
        }

    def test_an_option_colliding_with_an_entry_name_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._config({
                "livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST},
                "tunnel": {"options": {"livekit": {"ref": "oci://ghcr.io/org/repo/y@" + DIGEST}}},
            }),
        )

        with pytest.raises(ValidationError, match=r"one vocabulary.*livekit"):
            load_config(tmp_path)

    def test_an_option_colliding_across_groups_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._config({
                "one": {"options": {"cuda": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}}},
                "two": {"options": {"cuda": {"ref": "oci://ghcr.io/org/repo/y@" + DIGEST}}},
            }),
        )

        with pytest.raises(ValidationError, match=r"one vocabulary.*cuda"):
            load_config(tmp_path)


class TestAuthGroup:
    def _auth_config(self, options: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": {"auth": {"options": options}},
        }

    def test_an_auth_option_with_exactly_one_route_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
                    "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                }
            }),
        )

        assert load_config(tmp_path) is not None

    def test_an_auth_option_without_a_route_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({"keycloak": {"ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST}}),
        )

        with pytest.raises(ValidationError, match="auth option declares exactly one route"):
            load_config(tmp_path)

    def test_an_auth_option_with_two_routes_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
                    "routes": [
                        {"path": "/auth/*", "upstream": "keycloak:8080"},
                        {"path": "/auth2/*", "upstream": "keycloak:8080"},
                    ],
                }
            }),
        )

        with pytest.raises(ValidationError, match="exactly one route"):
            load_config(tmp_path)

    def test_a_bare_entry_named_auth_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"auth": {"ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST}},
            },
        )

        with pytest.raises(ValidationError, match="auth is a reserved group name"):
            load_config(tmp_path)


class TestRoutes:
    def _routed(self, routes: list[object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST, "routes": routes}},
        }

    def test_door_wide_path_uniqueness_refuses_duplicates(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {
                    "one": {
                        "options": {
                            "a": {
                                "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                                "routes": [{"path": "/x/*", "upstream": "x:1"}],
                            }
                        },
                    },
                    "two": {
                        "options": {
                            "b": {
                                "ref": "oci://ghcr.io/org/repo/y@" + DIGEST,
                                "routes": [{"path": "/x/*", "upstream": "y:1"}],
                            }
                        },
                    },
                },
            },
        )

        with pytest.raises(ValidationError, match="door-wide unique"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        "path",
        ["grafana/*", "//grafana/*", "/grafana//x", "/*/*", "/grafana /x", "grafana", "/grafana*", "/*grafana"],
    )
    def test_malformed_paths_refuse(self, path: str, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": path, "upstream": "api:8000"}]))

        with pytest.raises(ValidationError, match="must be a /-rooted segment chain"):
            load_config(tmp_path)

    def test_plain_and_wildcard_paths_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([
                {"path": "/*", "upstream": "api:8000"},
                {"path": "/livekit-token/*", "upstream": "livekit-token:8000"},
            ]),
        )

        config = load_config(tmp_path)

        assert config is not None

    def test_an_upstream_without_a_literal_port_refuses(self, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": "/*", "upstream": "api:${PORT}"}]))

        with pytest.raises(ValidationError, match="a service name and a literal port"):
            load_config(tmp_path)

    def test_strip_prefix_on_a_non_wildcard_path_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([{"path": "/grafana", "upstream": "grafana:3000", "strip_prefix": True}]),
        )

        with pytest.raises(ValidationError, match="must end in /\\*"):
            load_config(tmp_path)

    def test_a_raw_route_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([{"path": "/x/*", "upstream": "x:1", "raw": 'respond "hi" 200'}]),
        )

        config = load_config(tmp_path)

        assert config is not None


class TestGatewayKey:
    def test_routes_without_gateway_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match=r"gateway.*required"):
            load_config(tmp_path)

    def test_gateway_without_routes_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}},
            },
        )

        with pytest.raises(ValidationError, match="no route exists"):
            load_config(tmp_path)

    def test_a_literal_port_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8443",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert load_config(tmp_path) is not None

    def test_a_malformed_gateway_value_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "${gateway port}",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="literal port number or a"):
            load_config(tmp_path)


class TestPorts:
    def _ports_config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "gateway": "8080",
            "services": services,
        }

    def test_string_form_names_the_entry_service_with_tcp_default(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": ["${PORT:?err}/6178", "6179/6179/udp"],
                }
            }),
        )

        config = load_config(tmp_path)

        assert config is not None
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert [(port.service, port.published, port.target, port.protocol) for port in livekit.ports] == [
            ("livekit", "${PORT:?err}", "6178", "tcp"),
            ("livekit", "6179", "6179", "udp"),
        ]

    def test_option_level_string_form_names_the_option(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "tunnel": {
                    "options": {
                        "ngrok": {
                            "workloads": ["tunnel-ngrok.yaml"],
                            "ports": ["4040/4040"],
                        }
                    }
                },
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )
        write_lock(tmp_path)

        config = load_config(tmp_path)

        assert config is not None

    def test_a_group_level_string_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "tunnel": {"options": {"ngrok": {"workloads": ["tunnel-ngrok.yaml"]}}, "ports": ["4040/4040"]},
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )
        write_lock(tmp_path)

        with pytest.raises(ValidationError, match="mapping form"):
            load_config(tmp_path)

    def test_the_mapping_form_spells_the_owning_service_and_host_ip(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "placeframe": {
                    "options": {"cuda": {"workloads": ["api.yaml"]}},
                    "ports": [
                        {
                            "service": "api",
                            "host_ip": "127.0.0.1",
                            "published": "${API_DEBUG_PORT:-5679}",
                            "target": "5678",
                        }
                    ],
                },
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )
        write_lock(tmp_path)

        config = load_config(tmp_path)

        assert config is not None

    def test_a_non_port_non_hole_field_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": ["eight/eight"],
                }
            }),
        )

        with pytest.raises(ValidationError, match="literal port number or a"):
            load_config(tmp_path)

    def test_a_malformed_string_form_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": ["1/2/3/4"],
                }
            }),
        )

        with pytest.raises(ValidationError, match="published/target"):
            load_config(tmp_path)


class TestRepository:
    def test_repository_without_host_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "lbe-toolkit"})

        with pytest.raises(ValidationError, match="registry-host/repository-path"):
            load_config(tmp_path)

    def test_trailing_slash_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "ghcr.io/org/repo/"})

        with pytest.raises(ValidationError, match="trailing slash"):
            load_config(tmp_path)

    def test_repository_without_service_builds_loads(self, tmp_path: Path) -> None:
        # The publish shape for a repo whose whole runtime is its own workload set: no
        # Dockerfile service builds, repository naming the artifact registry.
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"rathole-client": {"workloads": ["rathole-client.yaml"]}},
            },
        )
        write_lock(tmp_path)

        config = load_config(tmp_path)

        assert config is not None and config.repository == "ghcr.io/org/repo"
        assert config.service_builds == {}


class TestPlatforms:
    def test_arch_pairs_load(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["linux/arm64"]})

        config = load_config(tmp_path)

        assert config is not None and config.platforms == ["linux/arm64"]

    def test_garbage_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["arm64"]})

        with pytest.raises(ValidationError, match="os/arch pair"):
            load_config(tmp_path)


class TestLeftoverManifestGuard:
    def test_leftover_manifest_refuses_loudly(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1"})
        manifest = tmp_path / LEFTOVER_MANIFEST_PATH
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("x-base-images:\n  ALPINE_IMAGE: alpine:3.21\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="manifest dissolved"):
            load_config(tmp_path)

    def test_leftover_lock_at_the_dead_path_refuses_loudly(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1"})
        stale_lock = tmp_path / LEFTOVER_LOCK_PATH
        stale_lock.parent.mkdir(parents=True, exist_ok=True)
        stale_lock.write_text("UV_IMAGE=alpine:3.21\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match=r"the lock lives at docker-devkit\.lock"):
            load_config(tmp_path)


class TestLockPairing:
    def test_base_images_without_lock_refuse(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(RuntimeError, match="image lock is required"):
            load_config(tmp_path)

    def test_charts_without_lock_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="image lock is required"):
            load_config(tmp_path)

    def test_lock_beside_base_images_loads(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"}})
        write_lock(tmp_path)

        assert load_config(tmp_path) is not None

    def test_no_declarations_need_no_lock(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "services": {"livekit": {"ref": "oci://ghcr.io/x/y@" + DIGEST}},
                "project_name": "example",
            },
        )

        assert load_config(tmp_path) is not None


class TestServiceDerivation:
    def test_dockerfile_dirs_derive_bare_services(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "database-manager", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")

        services = derive_services(tmp_path)

        # The service name IS the directory name — the old manifest's create-database key
        # renames to database-manager under directory-derived naming.
        assert sorted(services) == ["api", "database-manager"]
        assert services["api"].targets == ["api"]
        assert services["api"].dockerfile == "workloads/api/Dockerfile"
        assert services["api"].contexts == []

    def test_bare_identifier_from_declares_a_context_edge(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\nARG LOCALIZER_SHA\n")

        services = derive_services(tmp_path)

        assert services["localizer"].contexts == ["neural-networks-base"]
        assert services["localizer"].sha_args == ["LOCALIZER_SHA"]

    def test_stage_names_are_not_context_edges(self, tmp_path: Path) -> None:
        write_dockerfile(
            tmp_path,
            "api",
            "ARG UV_IMAGE\nFROM ${UV_IMAGE} AS base\nFROM base AS dev\nFROM base AS prod\n",
        )

        services = derive_services(tmp_path)

        assert services["api"].contexts == []

    def test_multi_stage_dockerfile_declares_a_dev_stage(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM docker.io/library/python:3.13-slim AS base\nFROM base AS dev\n")

        services = derive_services(tmp_path)

        assert services["api"].dev_stage

    def test_single_stage_dev_label_is_not_a_dev_stage(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM docker.io/library/python:3.13-slim AS dev\n")

        services = derive_services(tmp_path)

        assert not services["api"].dev_stage

    def test_bare_identifier_naming_no_sibling_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM python\n")

        with pytest.raises(RuntimeError, match="FROM python names no service directory"):
            derive_services(tmp_path)

    def test_tagged_and_registry_refs_stay_external(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "migrator", "FROM golang:1.22-alpine AS build\nFROM ghcr.io/org/base:1\n")

        services = derive_services(tmp_path)

        assert services["migrator"].contexts == []

    def test_torch_device_arg_fans_over_the_gpu_family(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")

        services = derive_services(tmp_path)

        assert services["neural-networks-base"].variants == ["cuda", "rocm"]
        assert services["neural-networks-base"].targets == ["neural-networks-base-cuda", "neural-networks-base-rocm"]

    def test_the_fan_propagates_along_context_edges(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\n")

        services = derive_services(tmp_path)

        assert services["localizer"].variants == ["cuda", "rocm"]
        assert services["localizer"].targets == ["localizer-cuda", "localizer-rocm"]

    def test_an_independent_fan_over_a_fanned_base_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\nARG TORCH_DEVICE\n")

        with pytest.raises(RuntimeError, match="inherits its base's variant set"):
            derive_services(tmp_path)

    def test_parents_disagreeing_on_variants_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "fanned-base", "FROM alpine:3.21\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "plain-base", "FROM alpine:3.21\n")
        write_dockerfile(tmp_path, "dependent", "FROM fanned-base AS a\nFROM plain-base AS b\n")

        with pytest.raises(RuntimeError, match="disagree on gpu variants"):
            derive_services(tmp_path)

    def test_sha_arg_naming_no_service_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM alpine:3.21\nARG GHOST_SHA\n")

        with pytest.raises(RuntimeError, match="ARG GHOST_SHA names no derived service"):
            derive_services(tmp_path)

    def test_dockerfiles_spanning_multiple_top_level_dirs_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM alpine:3.21\n")
        write_dockerfile(tmp_path, "bridge", "FROM alpine:3.21\n", top="docker")

        with pytest.raises(RuntimeError, match="span multiple top-level directories"):
            derive_services(tmp_path)

    def test_repository_required_when_services_exist(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM alpine:3.21\n")
        write_config(tmp_path, {"requires": ">=0.1"})

        with pytest.raises(RuntimeError, match="repository is required"):
            load_config(tmp_path)

    def test_derived_services_attach_to_the_loaded_config(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM alpine:3.21\n")
        write_config(tmp_path, {"requires": ">=0.1", "repository": "ghcr.io/org/repo"})

        config = load_config(tmp_path)

        assert config is not None
        assert sorted(config.service_builds) == ["api"]


def test_direct_model_construction_is_valid_for_tests() -> None:
    config = DockerDevkitConfig.model_validate({
        "requires": ">=0.1",
        "repository": "ghcr.io/org/repo",
        "project_name": "example",
        "services": {"core": {"workloads": ["api.yaml"]}},
    })

    assert compose_bundles(config)[0].name == "core"
