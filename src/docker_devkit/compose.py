from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from os import environ
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any

import typer
import yaml
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from .build_docker import run_build
from .config import (
    AUTH_GROUP_NAME,
    DockerDevkitConfig,
    ENV_NAME_PATTERN,
    LOCK_PATH,
    PortDeclaration,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    bundle_output,
    compose_bundles,
    load_config,
)
from .documents import parse_image_lock
from .door import (
    DOOR_CONFIG_NAME,
    DOOR_SERVICE_NAME,
    door_service_stanza,
    parse_public_url,
    render_caddyfile,
)
from .host import parse_env_file

ENV_FILE = Path(".env")
DEFAULT_COMPOSE_FILE = Path("compose.yml")
BUNDLES_VARIABLE = "BUNDLES"
DEV_TOGGLES_ANNOTATION = "outernet.internal/dev-toggles"
DEV_MOUNT_TARGET = "/app"

_MANIFEST_ABSENT_PATTERN = re.compile(r"\b404\b|not found|manifest unknown", re.IGNORECASE)

up_app = typer.Typer(add_completion=False)
down_app = typer.Typer(add_completion=False)


@dataclass(frozen=True)
class ComposeContext:
    root: Path
    config: DockerDevkitConfig | None
    compose_files: list[Path]
    environment: dict[str, str] | None
    dev_overlay: Path | None = None
    assembly_root: Path | None = None

    @classmethod
    def bring_up(cls, root: Path, *, dev: bool) -> ComposeContext:
        config = load_config(root)

        if not (root / ENV_FILE).exists():
            raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

        environment = _merged_env(root)

        if config is None or not config.services:
            if _selection_names(environment):
                raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
            return cls(root, config, [DEFAULT_COMPOSE_FILE], environment)

        choices = _resolve_selection(config, environment)
        sources, local_outputs = _selected_sources(root, config, choices)
        _refuse_service_collisions(root, local_outputs)
        routes = _assembly_routes(config, choices)
        ports = _assembly_ports(config, choices)

        assembly_root = _write_assembly_root(
            root, config, sources, routes, ports, _auth_route(config, choices), environment
        )
        context = cls(root, config, [assembly_root], environment, assembly_root=assembly_root)
        try:
            _validate_assembly(context, routes, ports, _auth_route(config, choices))
        except BaseException:
            assembly_root.unlink(missing_ok=True)
            raise

        _print_assembly(config, choices, routes)

        dev_overlay = _generate_dev_overlay(root, config, local_outputs) if dev else None
        if dev and dev_overlay is None:
            raise RuntimeError(
                "--dev found no dev shape in the assembly: a service Dockerfile declaring a "
                f"dev stage or a workload carrying {DEV_TOGGLES_ANNOTATION}"
            )

        return cls(
            root,
            config,
            [assembly_root, *([dev_overlay] if dev_overlay is not None else [])],
            environment,
            dev_overlay=dev_overlay,
            assembly_root=assembly_root,
        )

    @classmethod
    def teardown(cls, root: Path) -> ComposeContext:
        config = load_config(root)

        if not (root / ENV_FILE).exists():
            raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

        if config is None or not config.services:
            return cls(root, config, [DEFAULT_COMPOSE_FILE], None)
        if not config.project_name:
            raise RuntimeError(
                "docker-devkit.yaml declares no services; a builds-only or k8s-only repo has no compose stack"
            )

        # Teardown keys on the project, not on a file list: a name-only root plus
        # --remove-orphans removes every container the project ever created — any
        # selection, any profile — without resolving a single include, so an option's
        # env holes cannot break an open-mode teardown.
        assembly_root = _write_minimal_root(config)
        return cls(root, config, [assembly_root], None, assembly_root=assembly_root)

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_PATH

    def command(self, *arguments: str) -> str:
        files_arguments = " ".join(f"-f {compose_file}" for compose_file in self.compose_files)
        lock_argument = f" --env-file {self.lock_file}" if self.lock_file.exists() else ""
        prefix = f"docker compose {files_arguments} --project-directory {self.root} --env-file .env{lock_argument}"
        return " ".join([prefix, *arguments])


@up_app.command()
def up(
    attached: Annotated[bool, typer.Option("--attached", "-a", help="Run in foreground (not detached)")] = False,
    quiet_pull: Annotated[
        bool,
        typer.Option("--quiet-pull", "-q", help="Suppress per-layer pull progress (still shows pull/push totals)."),
    ] = False,
    build: Annotated[
        bool,
        typer.Option("--build", help="Build all images locally before bringing the stack up; skips pulling"),
    ] = False,
    dev: Annotated[
        bool,
        typer.Option("--dev", help="Layer the generated dev overlay (dev-stage images, repo bind, .env toggle holes)."),
    ] = False,
) -> None:
    root = Path.cwd()
    context = ComposeContext.bring_up(root, dev=dev)

    if build and (context.config is None or not (context.config.repository or context.config.base_images)):
        raise typer.BadParameter(
            "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
        )

    config = context.config
    if config is not None and context.lock_file.exists():
        # Fail closed over every lock entry — each mirror-prefixed by construction under
        # the declared or default prefix — distinguishing a manifest the registry truly
        # lacks (the declaring change has not been through CI's mirror job; no escape
        # flag) from a probe that failed before reaching a verdict (credentials, network,
        # daemon availability).
        for value in sorted(parse_image_lock(context.lock_file).root.values()):
            message = _mirror_probe_message(value)
            if message is None:
                continue
            raise RuntimeError(message)

    if build:
        run_build()
        if dev:
            # The overlay rides :dev images, so a dev bring-up builds both shapes.
            run_build(dev=True)

    # compose ignores COMPOSE_PROFILES whenever a --profile flag is present, so forward the
    # selection explicitly; shell env wins over .env, comma-split and deduplicated.
    raw_profiles = (context.environment or {}).get("COMPOSE_PROFILES", "")
    profiles: list[str] = []
    for entry in raw_profiles.split(","):
        profile = entry.strip()
        if profile and profile not in profiles:
            profiles.append(profile)
    if profiles:
        defined = enumerate_profiles(context.command())
        unknown = [profile for profile in profiles if profile not in defined]
        if unknown:
            raise RuntimeError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )
    up_command = context.command(*(f"--profile {profile}" for profile in profiles), "up")
    if not build:
        # tree-<sha> tags are content-addressed and may not be pushed, so pull only what is missing locally
        up_command += " --pull missing"
        if quiet_pull:
            up_command += " --quiet-pull"
    if not attached:
        up_command += " -d"

    _handoff(context, up_command)


@down_app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    context = ComposeContext.teardown(Path.cwd())
    command = context.command("down", "--remove-orphans")
    if volumes:
        command += " -v"

    _handoff(context, command)


def _handoff(context: ComposeContext, command: str) -> None:
    try:
        bash_handoff(command)
    finally:
        if context.dev_overlay is not None:
            context.dev_overlay.unlink(missing_ok=True)
        if context.assembly_root is not None:
            context.assembly_root.unlink(missing_ok=True)


def _merged_env(root: Path) -> dict[str, str]:
    values = parse_env_file(root / ENV_FILE)
    values.update(environ)
    return values


def _selection_names(environment: dict[str, str]) -> list[str]:
    names: list[str] = []
    for entry in environment.get(BUNDLES_VARIABLE, "").split(","):
        name = entry.strip()
        if name and name not in names:
            names.append(name)
    return names


def _resolve_selection(config: DockerDevkitConfig, environment: dict[str, str]) -> dict[str, str]:
    # BUNDLES= names options only: at most one per group, zero legal when riders carry the
    # assembly. Returns the group -> option choices.
    names = _selection_names(environment)
    options: dict[str, str] = {}
    riding: list[str] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            for option_name in entry.options:
                options[option_name] = name
        else:
            riding.append(name)
    if not names and not riding and options:
        raise RuntimeError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry rides; this repo's options are {sorted(options)}"
        )
    unknown = [name for name in names if name not in options]
    if unknown:
        riding_note = f"; riding entries, always on: {riding}" if riding else ""
        raise RuntimeError(
            f"{BUNDLES_VARIABLE}= names {unknown}; the selectable options are {sorted(options)}{riding_note}"
        )
    choices: dict[str, str] = {}
    for name in names:
        group = options[name]
        if group in choices:
            raise RuntimeError(
                f"{BUNDLES_VARIABLE}= selects {choices[group]} and {name}; at most one option per group ({group})"
            )
        choices[group] = name
    return choices


def _selected_sources(root: Path, config: DockerDevkitConfig, choices: dict[str, str]) -> tuple[list[str], list[Path]]:
    # The include list in services declaration order: every riding entry plus each group's
    # selected option, as an OCI ref (remote) or an absolute bundle-output path (local).
    sources: list[str] = []
    local_outputs: list[Path] = []

    def add(entry: ServiceEntry, name: str) -> None:
        if entry.ref is not None:
            sources.append(entry.ref)
            return
        output = root / bundle_output(name)
        if not output.is_file():
            raise RuntimeError(
                f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
            )
        if DOOR_SERVICE_NAME in _service_names(root, output.relative_to(root)):
            raise RuntimeError(
                f"bundle output {output.as_posix()} defines the reserved door service {DOOR_SERVICE_NAME!r}"
            )
        sources.append(output.resolve().as_posix())
        local_outputs.append(output)

    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            option_name = choices.get(name)
            if option_name is not None:
                add(entry.options[option_name], option_name)
        else:
            add(entry, name)
    return sources, local_outputs


def _assembly_routes(config: DockerDevkitConfig, choices: dict[str, str]) -> list[RouteConfig]:
    routes: list[RouteConfig] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            routes.extend(entry.routes)
            option_name = choices.get(name)
            if option_name is not None:
                routes.extend(entry.options[option_name].routes)
        else:
            routes.extend(entry.routes)
    return routes


def _assembly_ports(config: DockerDevkitConfig, choices: dict[str, str]) -> list[PortDeclaration]:
    ports: list[PortDeclaration] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            ports.extend(entry.ports)
            option_name = choices.get(name)
            if option_name is not None:
                ports.extend(entry.options[option_name].ports)
        else:
            ports.extend(entry.ports)
    return ports


def _auth_route(config: DockerDevkitConfig, choices: dict[str, str]) -> RouteConfig | None:
    auth = config.services.get(AUTH_GROUP_NAME)
    if not isinstance(auth, ServiceGroup):
        return None
    option_name = choices.get(AUTH_GROUP_NAME)
    if option_name is None:
        return None
    return auth.options[option_name].routes[0]


def _write_assembly_root(
    root: Path,
    config: DockerDevkitConfig,
    sources: list[str],
    routes: list[RouteConfig],
    ports: list[PortDeclaration],
    auth_route: RouteConfig | None,
    environment: dict[str, str],
) -> Path:
    document: dict[str, Any] = {}
    if config.project_name:
        document["name"] = config.project_name
    if sources:
        document["include"] = sources

    services: dict[str, Any] = {}
    if routes:
        services[DOOR_SERVICE_NAME] = _door_stanza(root, config, routes, auth_route, environment)
    for port in ports:
        entry: dict[str, Any] = {"target": port.target, "published": port.published, "protocol": port.protocol}
        if port.host_ip is not None:
            entry["host_ip"] = port.host_ip
        services.setdefault(port.service, {}).setdefault("ports", []).append(entry)
    if services:
        document["services"] = services
    if routes:
        # The Caddyfile rides as inline content with every $ doubled, so compose
        # interpolation leaves caddy's {$ENV} placeholders for the caddy runtime.
        document["configs"] = {DOOR_CONFIG_NAME: {"content": render_caddyfile(routes, auth_route).replace("$", "$$")}}

    return _write_temp_root(document)


def _door_stanza(
    root: Path,
    config: DockerDevkitConfig,
    routes: list[RouteConfig],
    auth_route: RouteConfig | None,
    environment: dict[str, str],
) -> dict[str, Any]:
    lock_path = root / LOCK_PATH
    if not lock_path.is_file() or "CADDY_IMAGE" not in parse_image_lock(lock_path).root:
        raise RuntimeError(
            f"routes are declared, so the door rides; {LOCK_PATH.as_posix()} lacks the CADDY_IMAGE entry — "
            "declare CADDY_IMAGE in base_images and run build --lock-only"
        )
    door_environment: dict[str, str] | None = None
    if auth_route is not None:
        public_url = environment.get("PUBLIC_URL", "").strip()
        if not public_url:
            raise RuntimeError(
                "an auth option is selected, so the door derives the forwarded scheme and port from PUBLIC_URL; "
                "PUBLIC_URL is missing from the environment"
            )
        scheme, port = parse_public_url(public_url)
        door_environment = {
            "FORWARDED_PROTO": scheme,
            "FORWARDED_PORT": port,
        }
    assert config.gateway is not None
    return door_service_stanza(config.gateway, door_environment)


def _write_minimal_root(config: DockerDevkitConfig) -> Path:
    assert config.project_name is not None
    return _write_temp_root({"name": config.project_name})


def _write_temp_root(document: dict[str, Any]) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return Path(handle.name)


def _validate_assembly(
    context: ComposeContext, routes: list[RouteConfig], ports: list[PortDeclaration], auth_route: RouteConfig | None
) -> None:
    # The drift-impossible teeth, against the assembly compose itself resolves (includes
    # included): every route upstream and every port's owning service must exist.
    if not routes and not ports:
        return
    resolved = {line.strip() for line in bash_output(f"{context.command('config', '--services')}").splitlines()}
    resolved.discard("")
    for route in routes:
        upstream = route.upstream.partition(":")[0]
        if upstream not in resolved:
            raise RuntimeError(
                f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
            )
    if auth_route is not None:
        upstream = auth_route.upstream.partition(":")[0]
        if upstream not in resolved:
            raise RuntimeError(f"the selected auth option's upstream {upstream} is absent from the assembly")
    for port in ports:
        if port.service not in resolved:
            raise RuntimeError(f"ports for service {port.service!r} — absent from the selected assembly")


def _print_assembly(config: DockerDevkitConfig, choices: dict[str, str], routes: list[RouteConfig]) -> None:
    riding = [name for name, entry in config.services.items() if not isinstance(entry, ServiceGroup)]
    if riding:
        print(f"riding: {', '.join(riding)}")
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            selected = choices.get(name)
            print(f"{name}: {selected if selected is not None else '(none selected)'}")
    if routes:
        print(f"door: {' '.join(route.path for route in routes)}")


def _refuse_service_collisions(root: Path, selected: list[Path]) -> None:
    file_services = [(path, _service_names(root, path)) for path in selected]
    for index, (path, services) in enumerate(file_services):
        for other_path, other_services in file_services[index + 1 :]:
            shared = sorted(services & other_services)
            if shared:
                raise RuntimeError(
                    f"the assembly includes {path.as_posix()} and {other_path.as_posix()}, "
                    f"which both define service(s) {shared}; select one"
                )


def _service_names(root: Path, path: Path) -> set[str]:
    # Name extraction only: the typed ComposeDocument lens carries bind laws generated
    # artifacts satisfy but authored compose files never promised to.
    document: Any = yaml.safe_load((root / path).read_text(encoding="utf-8"))
    services: dict[str, Any] = document.get("services") or {}
    return set(services)


def _generate_dev_overlay(root: Path, config: DockerDevkitConfig, local_outputs: list[Path]) -> Path | None:
    declared: set[str] = set()
    for path in local_outputs:
        declared |= _service_names(root, path)

    services: dict[str, dict[str, Any]] = {}
    for name, service in config.service_builds.items():
        if not service.dev_stage or name not in declared:
            continue
        if config.repository is None:
            raise RuntimeError("repository is required; dev image tags derive from it")
        services[name] = {
            "image": f"{config.repository}/{name}:dev",
            "volumes": [_dev_bind_volume(root)],
        }
    for bundle in compose_bundles(config):
        for workload in bundle.workloads:
            parsed = _workload_dev_toggles(root, config, workload)
            if parsed is None:
                continue
            service_name, toggles = parsed
            if service_name not in declared:
                continue
            if "environment" in services.get(service_name, {}):
                raise RuntimeError(
                    f"{workload} and another workload both declare {DEV_TOGGLES_ANNOTATION} for {service_name}"
                )
            services.setdefault(service_name, {})["environment"] = {
                key: f"${{{key}:-{value}}}" for key, value in toggles.items()
            }
    if not services:
        return None

    with NamedTemporaryFile(
        "w", suffix=".yaml", prefix="devkit-dev-overlay-", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(yaml.safe_dump({"services": services}, sort_keys=False, default_flow_style=False))
        overlay = Path(handle.name)
    return overlay


def _dev_bind_volume(root: Path) -> dict[str, Any]:
    return {
        "type": "bind",
        "source": root.resolve().as_posix(),
        "target": DEV_MOUNT_TARGET,
        "read_only": True,
    }


def _workload_dev_toggles(root: Path, config: DockerDevkitConfig, workload: str) -> tuple[str, dict[str, str]] | None:
    document: Any = yaml.safe_load((root / config.score_dir / workload).read_text(encoding="utf-8"))
    metadata: dict[str, Any] = document.get("metadata") or {}
    annotations: dict[str, Any] = metadata.get("annotations") or {}
    raw: Any = annotations.get(DEV_TOGGLES_ANNOTATION)
    if raw is None:
        return None
    if isinstance(raw, str):
        service_name = metadata.get("name")
        if not isinstance(service_name, str) or not service_name:
            raise RuntimeError(f"{workload}: metadata.name is required to key the dev overlay service")
        return service_name, _parse_dev_toggles(workload, raw)
    raise RuntimeError(f"{workload}: {DEV_TOGGLES_ANNOTATION} must be a comma-separated NAME=default string")


def _parse_dev_toggles(workload: str, raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        name, separator, default = item.strip().partition("=")
        if not separator:
            raise RuntimeError(
                f"{workload}: {DEV_TOGGLES_ANNOTATION} entry {item.strip()!r} misses its default; "
                "the ruled spelling is NAME=default pairs"
            )
        if ENV_NAME_PATTERN.fullmatch(name) is None:
            raise RuntimeError(f"{workload}: {DEV_TOGGLES_ANNOTATION} name {name!r} must be ALL_CAPS")
        if not default:
            raise RuntimeError(f"{workload}: {DEV_TOGGLES_ANNOTATION} default for {name} is empty")
        if name in pairs:
            raise RuntimeError(f"{workload}: {DEV_TOGGLES_ANNOTATION} names {name} twice")
        pairs[name] = default
    return pairs


def enumerate_profiles(compose_prefix: str) -> list[str]:
    output = bash_output(f"{compose_prefix} config --profiles")
    return [line.strip() for line in output.splitlines() if line.strip()]


def _mirror_probe_message(value: str) -> str | None:
    reference, _, digest = value.rpartition("@")
    try:
        bash_output(f"docker buildx imagetools inspect {shlex.quote(value)}")
    except CalledProcessError as error:
        stderr = (error.stderr or str(error)).strip()
        if _MANIFEST_ABSENT_PATTERN.search(stderr):
            return (
                f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored yet; "
                "the CI mirror job populates it when the declaring change runs"
            )
        return (
            f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe "
            "(docker buildx imagetools inspect) failed before it could determine whether the image "
            "exists, so this is not evidence the image is missing; check registry credentials, network, "
            f"and docker daemon availability. Probe error: {stderr}"
        )
    return None
