from docker_devkit.config import RouteConfig
from docker_devkit.door import (
    DOOR_LISTEN_PORT,
    door_service_stanza,
    parse_public_url,
    render_caddyfile,
)


def route(**kwargs: object) -> RouteConfig:
    return RouteConfig.model_validate({"path": kwargs.pop("path"), "upstream": kwargs.pop("upstream"), **kwargs})


class TestRenderCaddyfile:
    def test_the_site_listens_on_the_door_port(self) -> None:
        caddyfile = render_caddyfile([route(path="/*", upstream="api:8000")], None)

        assert caddyfile.startswith(f":{DOOR_LISTEN_PORT} {{\n")

    def test_the_catch_all_rides_as_a_bare_handle(self) -> None:
        caddyfile = render_caddyfile([route(path="/*", upstream="api:8000")], None)

        assert "\thandle {\n\t\treverse_proxy api:8000\n\t}\n}" in caddyfile

    def test_wildcard_paths_render_as_handle_matchers(self) -> None:
        caddyfile = render_caddyfile([route(path="/livekit-token/*", upstream="livekit-token:8000")], None)

        assert "\thandle /livekit-token/* {\n\t\treverse_proxy livekit-token:8000\n\t}" in caddyfile

    def test_strip_prefix_renders_as_handle_path(self) -> None:
        caddyfile = render_caddyfile([route(path="/auth/*", upstream="keycloak:8080", strip_prefix=True)], None)

        assert "\thandle_path /auth/* {\n\t\treverse_proxy keycloak:8080\n\t}" in caddyfile

    def test_the_auth_route_carries_the_forwarded_headers(self) -> None:
        auth = route(path="/auth/*", upstream="keycloak:8080", strip_prefix=True)
        caddyfile = render_caddyfile([auth], auth)

        assert "\t\t\treverse_proxy keycloak:8080 {" in caddyfile.replace("\t\treverse_proxy", "\t\t\treverse_proxy")
        assert "header_up X-Forwarded-Proto {$FORWARDED_PROTO}" in caddyfile
        assert "header_up X-Forwarded-Port {$FORWARDED_PORT}" in caddyfile

    def test_plain_routes_carry_no_forwarded_headers(self) -> None:
        auth = route(path="/auth/*", upstream="keycloak:8080", strip_prefix=True)
        caddyfile = render_caddyfile([auth, route(path="/*", upstream="api:8000")], auth)

        assert "header_up" not in caddyfile.split("handle {", 1)[1]

    def test_a_raw_route_rides_verbatim_under_its_path(self) -> None:
        caddyfile = render_caddyfile(
            [route(path="/x/*", upstream="x:1", raw='respond "raw" 200')],
            None,
        )

        assert '\thandle /x/* {\n\t\trespond "raw" 200\n\t}' in caddyfile

    def test_blocks_render_in_declaration_order(self) -> None:
        caddyfile = render_caddyfile(
            [
                route(path="/*", upstream="api:8000"),
                route(path="/grafana/*", upstream="grafana:3000"),
            ],
            None,
        )

        assert caddyfile.index("reverse_proxy api:8000") < caddyfile.index("reverse_proxy grafana:3000")


class TestParsePublicUrl:
    def test_http_with_an_explicit_port(self) -> None:
        assert parse_public_url("http://192.168.8.10:58080") == ("http", "58080")

    def test_https_with_an_explicit_port(self) -> None:
        assert parse_public_url("https://example.dev:8443/path") == ("https", "8443")

    def test_https_defaults_to_443(self) -> None:
        assert parse_public_url("https://example.dev") == ("https", "443")

    def test_http_defaults_to_80(self) -> None:
        assert parse_public_url("http://example.dev") == ("http", "80")


class TestDoorServiceStanza:
    def test_the_stanza_carries_the_caddy_hole_config_mount_and_port(self) -> None:
        stanza = door_service_stanza("${GATEWAY_PORT:?err}", None)

        assert stanza == {
            "image": "${CADDY_IMAGE:?err}",
            "configs": [{"source": "devkit-door-caddyfile", "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": "8443", "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}],
        }

    def test_the_auth_environment_rides_when_auth_is_selected(self) -> None:
        stanza = door_service_stanza("8080", {"FORWARDED_PROTO": "http", "FORWARDED_PORT": "58080"})

        assert stanza["environment"] == {
            "FORWARDED_PROTO": "http",
            "FORWARDED_PORT": "58080",
        }
