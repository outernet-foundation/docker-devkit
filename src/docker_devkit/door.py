from __future__ import annotations

from .config import RouteConfig

DOOR_SERVICE_NAME = "door"
DOOR_LISTEN_PORT = 8443
DOOR_CONFIG_NAME = "devkit-door-caddyfile"
DOOR_CONFIG_TARGET = "/etc/caddy/Caddyfile"
CADDY_IMAGE_HOLE = "${CADDY_IMAGE:?err}"


def render_caddyfile(routes: list[RouteConfig], auth_route: RouteConfig | None) -> str:
    # handle blocks are mutually exclusive and caddy sorts them longest-path-first, so
    # declaration order cannot shadow; the catch-all /* rides as a bare handle.
    lines = [f":{DOOR_LISTEN_PORT} {{"]
    for route in routes:
        lines.extend(_route_block(route, auth_route))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _route_block(route: RouteConfig, auth_route: RouteConfig | None) -> list[str]:
    matcher = "" if route.path == "/*" else f" {route.path}"
    body: list[str] = []
    if route.raw is not None:
        body = ["\t\t" + line for line in route.raw.splitlines()]
    else:
        body = []
        if route is auth_route:
            body.append(f"\t\treverse_proxy {route.upstream} {{")
            body.append("\t\t\theader_up X-Forwarded-Proto {$FORWARDED_PROTO}")
            body.append("\t\t\theader_up X-Forwarded-Port {$FORWARDED_PORT}")
            body.append("\t\t}")
        else:
            body.append(f"\t\treverse_proxy {route.upstream}")
    head = "\thandle_path" if route.strip_prefix else "\thandle"
    return [f"{head}{matcher} {{", *body, "\t}"]


def parse_public_url(public_url: str) -> tuple[str, str]:
    # The forwarded scheme/port pair keycloak's redirect-URI matching needs, derived from
    # the public URL exactly as the retired gateway entrypoint derived it.
    scheme, _, rest = public_url.partition("://")
    hostport = rest.partition("/")[0]
    if ":" in hostport:
        port = hostport.rpartition(":")[2]
    elif scheme == "https":
        port = "443"
    else:
        port = "80"
    return scheme, port


def door_service_stanza(
    published: str,
    environment: dict[str, str] | None,
) -> dict[str, object]:
    stanza: dict[str, object] = {
        "image": CADDY_IMAGE_HOLE,
        "configs": [{"source": DOOR_CONFIG_NAME, "target": DOOR_CONFIG_TARGET}],
        "ports": [{"target": str(DOOR_LISTEN_PORT), "published": published, "protocol": "tcp"}],
    }
    if environment:
        stanza["environment"] = environment
    return stanza
