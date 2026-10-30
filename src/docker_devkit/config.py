from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self, get_args

from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from strictyaml import load as load_strict_yaml

DEFAULT_CONFIG_PATH = Path("docker-devkit.yaml")
DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

# The lock pairs with the root config at the repo root: build keys declared
# (base_images or charts) => the committed lock is required.
LOCK_PATH = Path("docker-devkit.lock")
LEFTOVER_MANIFEST_PATH = Path("workloads/images.yml")
LEFTOVER_LOCK_PATH = Path("workloads/images.lock")

GpuFamily = Literal["cuda", "rocm"]

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

# path:tag or path@digest — upstream-native, never a bare path or a ${…} hole.
DECLARED_REFERENCE_PATTERN = re.compile(
    r"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)

CHART_REFERENCE_PREFIX = "oci://"

SCORE_DIR = Path("stack/score")
K8S_OUTPUT = Path("stack/generated/k8s/manifests.yaml")
K8S_LOCAL_OUTPUT = Path("stack/generated/k8s/manifests.local.yaml")
SCORE_COMPOSE_VERSION = "0.42.0"
SCORE_K8S_VERSION = "0.15.0"
HELM_VERSION = "3.22.1"

SECRETS_FILE = Path("stack/secrets.yaml")
SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

BUNDLE_OUTPUT_DIR = Path("stack/generated/compose")

DOCKERFILE_NAME = "Dockerfile"
FAN_ARG = "TORCH_DEVICE"
FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
BARE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# A route path is a /-rooted segment chain, optionally wildcarded by a trailing /*; the
# root catch-all is exactly /*.
ROUTE_PATH_PATTERN = re.compile(r"^(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)$")
# An upstream is a compose service name and a literal port.
ROUTE_UPSTREAM_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+$")
# A remote include is an OCI reference pinned by digest.
REMOTE_REF_PATTERN = re.compile(r"^oci://\S+@sha256:[a-f0-9]{64}$")
# A compose env hole: ${NAME}, ${NAME:?err}, ${NAME:-default}.
ENV_HOLE_PATTERN = re.compile(r"^\$\{[A-Z][A-Z0-9_]*(?::[^{}]*)?\}$")
PORT_PATTERN = re.compile(r"^[0-9]+$")

AUTH_GROUP_NAME = "auth"
PORT_PROTOCOLS = ("tcp", "udp")


def service_sha_env(name: str) -> str:
    return name.upper().replace("-", "_") + "_SHA"


def chart_lock_key(name: str) -> str:
    return name.upper().replace("-", "_") + "_CHART"


def bundle_output(name: str) -> Path:
    return BUNDLE_OUTPUT_DIR / f"compose.{name}.yaml"


def chart_values_path(score_dir: Path, chart: str) -> Path:
    return score_dir / "k8s" / "values" / f"{chart}.yaml"


class MirrorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str

    @field_validator("prefix")
    @classmethod
    def _require_bare_prefix(cls, prefix: str) -> str:
        if not prefix or prefix.endswith("/"):
            raise ValueError("mirror prefix must be non-empty and carry no trailing slash")
        return prefix


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    upstream: str
    strip_prefix: bool = False
    raw: str | None = None

    @field_validator("path")
    @classmethod
    def _require_route_path(cls, path: str) -> str:
        if ROUTE_PATH_PATTERN.fullmatch(path) is None:
            raise ValueError(f"route path {path!r} must be a /-rooted segment chain, optionally wildcarded as …/*")
        return path

    @field_validator("upstream")
    @classmethod
    def _require_upstream(cls, upstream: str) -> str:
        if ROUTE_UPSTREAM_PATTERN.fullmatch(upstream) is None:
            raise ValueError(f"route upstream {upstream!r} must be a service name and a literal port (grafana:3000)")
        return upstream

    @model_validator(mode="after")
    def _strip_prefix_needs_a_wildcard(self) -> Self:
        if self.strip_prefix and not self.path.endswith("/*"):
            raise ValueError(f"route {self.path}: strip_prefix strips a wildcard prefix; the path must end in /*")
        return self


class PortDeclaration(BaseModel):
    # A port is either the mapping form or the string form published/target[/protocol],
    # which names the entry's or option's namesake service as its owner (the
    # _coerce_services pre-pass injects the service before validation).
    model_config = ConfigDict(extra="forbid")

    service: str
    published: str
    target: str
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None

    @field_validator("published", "target")
    @classmethod
    def _numeric_or_hole(cls, value: str, info: Any) -> str:
        if PORT_PATTERN.fullmatch(value) is None and ENV_HOLE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"ports {info.field_name} {value!r} must be a literal port number or a ${{…}} env hole "
                "(published and target may both be holes)"
            )
        return value


class ServiceEntry(BaseModel):
    # An option (inside a group's options:) or a bare riding entry. The source is either a
    # remote include (ref:) or a producer-local workload list (workloads:/vars:); routes and
    # ports attach to either shape.
    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.ref is not None

    @field_validator("ref")
    @classmethod
    def _require_digest_pinned_ref(cls, ref: str | None) -> str | None:
        if ref is not None and REMOTE_REF_PATTERN.fullmatch(ref) is None:
            raise ValueError(f"ref {ref!r} must be a digest-pinned OCI include (oci://…@sha256:…)")
        return ref

    @field_validator("workloads")
    @classmethod
    def _require_workloads(cls, workloads: list[str]) -> list[str]:
        if not workloads:
            raise ValueError("a local entry carries at least one workload; it renders and publishes an artifact")
        for name in workloads:
            if not name:
                raise ValueError("workload names must be non-empty")
        return workloads

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if self.ref is not None and self.workloads:
            raise ValueError("an entry is either ref: or workloads: — exactly one source, never both")
        if self.ref is None and not self.workloads:
            raise ValueError("an entry is either ref: or workloads: — exactly one source, never neither")
        if self.ref is not None and self.vars:
            raise ValueError("vars fill local workload renderings; a ref carries its producer's vars")
        return self


class ServiceGroup(BaseModel):
    # A selectable entry: mutually exclusive options picked through BUNDLES=, with
    # group-level routes and ports riding whichever option is selected.
    model_config = ConfigDict(extra="forbid")

    options: dict[str, ServiceEntry]
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)


class K8sConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)

    @property
    def rendering(self) -> bool:
        return bool(self.workloads or self.charts)

    @field_validator("charts")
    @classmethod
    def _require_upstream_native_refs(cls, charts: dict[str, str]) -> dict[str, str]:
        for name, reference in charts.items():
            if not name:
                raise ValueError("chart names must be non-empty")
            if not reference.startswith(CHART_REFERENCE_PREFIX):
                raise ValueError(
                    f"charts.{name}: {reference!r} must be an upstream-native OCI reference "
                    f"({CHART_REFERENCE_PREFIX}host/path:tag or {CHART_REFERENCE_PREFIX}host/path@digest)"
                )
            remainder = reference.removeprefix(CHART_REFERENCE_PREFIX)
            match = DECLARED_REFERENCE_PATTERN.fullmatch(remainder)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(
                    f"charts.{name}: {reference!r} must carry a tag or a digest; a bare path or a ${{…}} hole refuses"
                )
        return charts

    @model_validator(mode="after")
    def _require_a_rendering(self) -> Self:
        if not self.rendering:
            raise ValueError("the k8s section declares neither workloads nor charts; remove it")
        return self


class ServiceBuild(BaseModel):
    # Derived by load_config from the Dockerfile tree; never authored.
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    variants: list[GpuFamily] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)
    dev_stage: bool = False

    @property
    def target_variants(self) -> list[tuple[str, GpuFamily | None]]:
        variants: list[GpuFamily | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]

    @property
    def targets(self) -> list[str]:
        return [target for target, _variant in self.target_variants]


class DockerDevkitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires: str
    gateway: str | None = None
    repository: str | None = None
    base_images: dict[str, str] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)
    project_name: str | None = None
    services: dict[str, ServiceGroup | ServiceEntry] = Field(default_factory=dict)
    k8s: K8sConfig | None = None
    mirror: MirrorConfig | None = None

    score_dir: Path = SCORE_DIR
    k8s_output: Path = K8S_OUTPUT
    k8s_local_output: Path = K8S_LOCAL_OUTPUT
    score_compose_version: str = SCORE_COMPOSE_VERSION
    score_k8s_version: str = SCORE_K8S_VERSION
    helm_version: str = HELM_VERSION

    # Derived by load_config from the Dockerfile tree (derive_services); never authored.
    service_builds: dict[str, ServiceBuild] = Field(default_factory=dict, exclude=True)

    @field_validator("requires")
    @classmethod
    def _validate_requires(cls, value: str) -> str:
        if not value:
            raise ValueError("requires must be a non-empty PEP 440 specifier; use '>=0.0' for no constraint")
        SpecifierSet(value)
        return value

    @field_validator("gateway")
    @classmethod
    def _require_port_or_hole(cls, gateway: str | None) -> str | None:
        if gateway is None:
            return gateway
        if PORT_PATTERN.fullmatch(gateway) is None and ENV_HOLE_PATTERN.fullmatch(gateway) is None:
            raise ValueError(f"gateway {gateway!r} must be a literal port number or a ${{…}} env hole")
        return gateway

    @field_validator("repository")
    @classmethod
    def _require_registry_repository(cls, repository: str | None) -> str | None:
        if repository is None:
            return repository
        host, separator, path = repository.partition("/")
        if not separator or not host or not path or repository.endswith("/"):
            raise ValueError(
                "repository must be registry-host/repository-path with no trailing slash (ghcr.io/org/repo)"
            )
        return repository

    @field_validator("base_images")
    @classmethod
    def _require_tag_or_digest(cls, base_images: dict[str, str]) -> dict[str, str]:
        for name, reference in base_images.items():
            if ENV_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(f"base_images key {name!r} must be an ALL_CAPS env name; it becomes a build arg")
            match = DECLARED_REFERENCE_PATTERN.fullmatch(reference)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"base_images.{name}: {reference!r} must be 'path:tag' or 'path@digest'")
        return base_images

    @field_validator("platforms")
    @classmethod
    def _require_arch_pairs(cls, platforms: list[str]) -> list[str]:
        for platform in platforms:
            if PLATFORM_PATTERN.fullmatch(platform) is None:
                raise ValueError(f"platforms entry {platform!r} must be an os/arch pair like linux/arm64")
        return platforms

    @field_validator("services", mode="before")
    @classmethod
    def _coerce_services(cls, services: Any) -> Any:
        # String-form ports name their owner by context (the entry or option they sit on);
        # group-level ports must spell the mapping form because a group has no namesake service.
        raw: Any = services
        if not isinstance(services, dict):
            return services
        entries: dict[str, Any] = {name: entry for name, entry in raw.items()}
        for name, raw_entry in entries.items():
            entry: dict[str, Any] = raw_entry
            if "options" in entry:
                options: dict[str, Any] = entry["options"] or {}
                if not options:
                    raise ValueError(f"services.{name}: a group's options: is a non-empty map of alternatives")
                for option_name, raw_option in options.items():
                    option: dict[str, Any] = raw_option
                    _coerce_port_strings(f"services.{name}.options.{option_name}", option, option_name, bare_legal=True)
                _coerce_port_strings(f"services.{name}", entry, name, bare_legal=False)
            else:
                if "ref" in entry and "workloads" in entry:
                    raise ValueError(f"services.{name}: carries both ref: and workloads: — exactly one source")
                if "ref" not in entry and "workloads" not in entry:
                    raise ValueError(
                        f"services.{name}: carries neither ref: nor workloads: — an entry is one of them "
                        "(or a group via options:)"
                    )
                _coerce_port_strings(f"services.{name}", entry, name, bare_legal=True)
        return entries

    @model_validator(mode="after")
    def _service_laws(self) -> Self:
        # The flat vocabulary (entry and option names share one namespace), the auth shape,
        # door-wide path uniqueness, the gateway pairing, and the identity pairings.
        entry_names = set(self.services)
        option_owner: dict[str, str] = {}
        route_owner: dict[str, str] = {}
        any_route = False
        any_local = False

        def collect(routes: list[RouteConfig], where: str) -> None:
            nonlocal any_route
            for route in routes:
                any_route = True
                if route.path in route_owner:
                    raise ValueError(
                        f"{where}: route path {route.path} is already declared at {route_owner[route.path]}; "
                        "paths are door-wide unique"
                    )
                route_owner[route.path] = where

        for name, entry in self.services.items():
            if isinstance(entry, ServiceGroup):
                for option_name, option in entry.options.items():
                    if option_name in entry_names or option_name in option_owner:
                        raise ValueError(
                            f"services.{name}.options.{option_name}: entry and option names share one vocabulary; "
                            f"{option_name} is already taken"
                        )
                    option_owner[option_name] = name
                    any_local = any_local or not option.remote
                    collect(option.routes, f"services.{name}.options.{option_name}")
                collect(entry.routes, f"services.{name}")
            else:
                any_local = any_local or not entry.remote
                collect(entry.routes, f"services.{name}")

        if AUTH_GROUP_NAME in self.services:
            auth = self.services[AUTH_GROUP_NAME]
            if not isinstance(auth, ServiceGroup):
                raise ValueError(f"services.{AUTH_GROUP_NAME}: auth is a reserved group name — shape it via options:")
            for option_name, option in auth.options.items():
                if len(option.routes) != 1:
                    raise ValueError(
                        f"services.{AUTH_GROUP_NAME}.options.{option_name}: an auth option declares exactly one "
                        "route; its upstream is the auth entry point the door forwards headers for"
                    )

        if any_route and self.gateway is None:
            raise ValueError("routes are declared; gateway (the door's host port) is required")
        if not any_route and self.gateway is not None:
            raise ValueError("gateway is declared but no route exists anywhere; the door would never emit")

        if self.services:
            if not self.project_name:
                raise ValueError("services are declared; project_name is required (the compose project name)")
            if any_local and not self.repository:
                raise ValueError(
                    "local entries (workloads:) are declared; repository is required — published artifacts "
                    "derive their names from it"
                )
        return self


def _coerce_port_strings(where: str, entry: dict[str, Any], owning_name: str, *, bare_legal: bool) -> None:
    ports: list[Any] = entry.get("ports") or []
    coerced: list[Any] = []
    for item in ports:
        if isinstance(item, dict):
            coerced.append(item)
            continue
        if not isinstance(item, str):
            raise TypeError(f"{where}.ports: each entry is a published/target[/protocol] string or a mapping")
        if not bare_legal:
            raise ValueError(
                f"{where}.ports: a group has no namesake service — spell the mapping form (service, published, target)"
            )
        parts = item.split("/")
        if len(parts) == 2:
            published, target, protocol = parts[0], parts[1], "tcp"
        elif len(parts) == 3:
            published, target, protocol = parts
        else:
            raise ValueError(f"{where}.ports entry {item!r} is not a published/target[/protocol] string")
        coerced.append({"service": owning_name, "published": published, "target": target, "protocol": protocol})
    entry["ports"] = coerced


class ComposeBundle(NamedTuple):
    name: str
    output: Path
    workloads: list[str]
    vars: dict[str, str]


def compose_bundles(config: DockerDevkitConfig) -> list[ComposeBundle]:
    # The local renderables: every workload-bearing option and bare entry, in declaration
    # order. Remote refs never render here; consumers include them by digest.
    bundles: list[ComposeBundle] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            for option_name, option in entry.options.items():
                if option.remote:
                    continue
                bundles.append(
                    ComposeBundle(option_name, bundle_output(option_name), list(option.workloads), dict(option.vars))
                )
        elif not entry.remote:
            bundles.append(ComposeBundle(name, bundle_output(name), list(entry.workloads), dict(entry.vars)))
    return bundles


def derive_services(root: Path) -> dict[str, ServiceBuild]:
    # Every Dockerfile-bearing directory one level below a top-level directory is a
    # service; the graph (context edges, gpu fans, SHA args) derives from the
    # Dockerfiles themselves.
    service_dirs: dict[str, Path] = {}
    tops: set[str] = set()
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / DOCKERFILE_NAME).is_file():
                service_dirs[child.name] = child
                tops.add(top.name)
    if len(tops) > 1:
        raise RuntimeError(f"Dockerfile service directories span multiple top-level directories: {sorted(tops)}")

    services: dict[str, ServiceBuild] = {}
    own_fans: dict[str, bool] = {}
    for name, directory in service_dirs.items():
        dockerfile = directory / DOCKERFILE_NAME
        from_images: list[str] = []
        stages: set[str] = set()
        args: list[str] = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            from_match = FROM_LINE_PATTERN.match(line.strip())
            if from_match is not None:
                from_images.append(from_match.group(1))
                if from_match.group(2) is not None:
                    stages.add(from_match.group(2))
                continue
            arg_match = ARG_LINE_PATTERN.match(line.strip())
            if arg_match is not None:
                args.append(arg_match.group(1))
        contexts: list[str] = []
        for image in from_images:
            if BARE_IDENTIFIER_PATTERN.fullmatch(image) is None or image in stages:
                continue
            if image not in service_dirs:
                raise RuntimeError(
                    f"{dockerfile.relative_to(root).as_posix()}: FROM {image} names no service directory; "
                    "external bases must be registry references or base_images build args"
                )
            contexts.append(image)
        services[name] = ServiceBuild(
            name=name,
            dockerfile=dockerfile.relative_to(root).as_posix(),
            contexts=sorted(set(contexts)),
            sha_args=sorted({arg for arg in args if arg.endswith("_SHA")}),
            # A genuine dev stage is one of several stages; a single-stage AS dev label
            # names the published image and is not a dev shape.
            dev_stage="dev" in stages and len(stages) > 1,
        )
        own_fans[name] = FAN_ARG in args

    computable = {service_sha_env(name) for name in services}
    for service in services.values():
        unknown = [arg for arg in service.sha_args if arg not in computable]
        if unknown:
            raise RuntimeError(
                f"workloads/{service.name}/{DOCKERFILE_NAME}: ARG {', '.join(unknown)} names no derived service; "
                "the stamp receiver must match a service directory"
            )

    resolved_variants: dict[str, list[GpuFamily]] = {}

    def resolve_variants(name: str, seen: tuple[str, ...]) -> list[GpuFamily]:
        if name in resolved_variants:
            return resolved_variants[name]
        if name in seen:
            raise RuntimeError(f"FROM context cycle: {' -> '.join((*seen, name))}")
        parent_sets = [resolve_variants(parent, (*seen, name)) for parent in services[name].contexts]
        distinct = {tuple(variants) for variants in parent_sets}
        if len(distinct) > 1:
            parents = ", ".join(services[name].contexts)
            raise RuntimeError(
                f"workloads/{name}/{DOCKERFILE_NAME}: parents ({parents}) disagree on gpu variants; "
                "a dependent inherits its base's set exactly"
            )
        if parent_sets:
            if own_fans[name]:
                raise RuntimeError(
                    f"workloads/{name}/{DOCKERFILE_NAME}: declares ARG {FAN_ARG} over a fanned base; "
                    "a dependent inherits its base's variant set"
                )
            variants = parent_sets[0]
        else:
            variants = list(get_args(GpuFamily)) if own_fans[name] else []
        resolved_variants[name] = variants
        return variants

    for name, service in services.items():
        service.variants = resolve_variants(name, ())
    return services


def load_config(root: Path) -> DockerDevkitConfig | None:
    path = root / DEFAULT_CONFIG_PATH
    if not path.exists():
        return None
    config = DockerDevkitConfig.model_validate(load_strict_yaml(path.read_text(encoding="utf-8")).data)
    installed = importlib.metadata.version("docker-devkit")
    if installed != DEV_SENTINEL and not SpecifierSet(config.requires).contains(installed, prereleases=True):
        raise SystemExit(f"docker-devkit {installed} does not satisfy requires={config.requires!r}")
    _finalize(config, root)
    return config


def _finalize(config: DockerDevkitConfig, root: Path) -> None:
    # Filesystem-gated laws: the leftover-manifest guard, the service derivation, and
    # the pairing laws that need the derived service set.
    if (root / LEFTOVER_MANIFEST_PATH).exists():
        raise RuntimeError(
            f"{LEFTOVER_MANIFEST_PATH.as_posix()} exists; the manifest dissolved — its declarations live in "
            f"{DEFAULT_CONFIG_PATH.as_posix()}'s root base_images/repository keys. Delete the leftover."
        )
    if (root / LEFTOVER_LOCK_PATH).exists():
        raise RuntimeError(
            f"{LEFTOVER_LOCK_PATH.as_posix()} exists; the lock lives at {LOCK_PATH.as_posix()} in the repo root. "
            "Move the file (git mv)."
        )

    service_builds = derive_services(root)
    if service_builds and config.repository is None:
        listed = ", ".join(sorted(service_builds))
        raise RuntimeError(
            f"Dockerfile service directories exist ({listed}); repository is required — it derives every image tag"
        )
    config.service_builds = service_builds

    declared_charts = config.k8s.charts if config.k8s is not None else {}
    if (config.base_images or declared_charts) and not (root / LOCK_PATH).is_file():
        raise RuntimeError(
            f"{LOCK_PATH.as_posix()}: base_images or k8s.charts are declared, so the committed image lock is "
            "required beside them (build writes it)"
        )
    for chart in declared_charts:
        values = root / chart_values_path(config.score_dir, chart)
        if not values.is_file():
            raise RuntimeError(f"{values.as_posix()}: the chart's values file is required at the derived path")
