from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal, NamedTuple

import typer
import yaml
from bashrun.bash import bash, bash_output

from .config import (
    LOCK_PATH,
    SECRETS_FILE,
    SECRETS_OUTPUT,
    DockerDevkitConfig,
    chart_lock_key,
    chart_values_path,
    compose_bundles,
    load_config,
)
from .documents import (
    BindVolume,
    ComposeDocument,
    ManifestDocument,
    ManifestMetadata,
    ScoreState,
    compute_service_shas,
    parse_image_lock,
)
from .markers import contains_marker, is_list, is_mapping, resolve_includes, resolve_lock_refs
from .provisioners import compiled_provisioners, dump_compose
from .secrets import SecretsDeclaration, collect_secret_keys, validate_consumed

# Match only ALL-CAPS placeholders so Score's lowercase dotted references never collide.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Treat sources under this prefix as relative to score-compose's state directory.
STATE_PREFIX = ".score-compose/"

# score-compose names services {workload}-{container}; when the halves match, the name is doubled.
DOUBLED_SERVICE_NAME = re.compile(r"^(.+)-\1$")

# Count a brace hole as consumed when an odd number of dollars precedes it, since even runs
# are $$-escaped literals.
COMPOSE_HOLE = re.compile(r"(\$+)\{([A-Z][A-Z0-9_]*)(?::[^{}]*)?\}")

# Built-in patch templates ship as package data under this directory and register onto the
# devkit's own score init invocations; consumer repos never author or declare one.
PATCH_TEMPLATES_DIR = "patch_templates"
GATE_PIN_TEMPLATE = "wait-for-resources-image.tpl"
GATE_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_POD_TEMPLATE = "gpu-pod.tpl"
SCALE_TO_ZERO_TEMPLATE = "scale-to-zero.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}

# The deployment-surface annotations the built-in templates read; their refusal laws live
# in devkit code because templates are data and cannot refuse.
PROBE_ADAPTER_ANNOTATION = "outernet.internal/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "outernet.internal/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
PUBLISH_ANNOTATION = "outernet.internal/publish"
CONSUMER_INTERPOLATED_ANNOTATION = "outernet.internal/consumer-interpolated"

Target = Literal["both", "compose", "k8s"]

# The storage classes are org-global infra facts, one k8s consumer; score-k8s defines the
# variable name the workload files interpolate.
STORAGE_CLASS_VAR = "SCORE_STORAGE_CLASS"
CLOUD_STORAGE_CLASS = "hcloud-volumes"
LOCAL_STORAGE_CLASS = "local-path"


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    release: str
    archive: str
    extract: str


app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def cli(
    target: Annotated[Target, typer.Option(help="Which Score artifact to generate.")] = "both",
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Generate for a local cluster: local storage class, written to the configured local output path.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Regenerate and exit 1 when the committed artifacts are stale."),
    ] = False,
) -> None:
    if check and local:
        raise RuntimeError(
            "--check verifies the committed artifacts; it cannot run against the gitignored --local output"
        )
    root = Path.cwd()
    devkit = load_config(root)
    if devkit is None or (not compose_bundles(devkit) and devkit.k8s is None):
        if not check:
            raise RuntimeError(
                "docker-devkit.yaml declares no local service entries and no k8s section; nothing to generate"
            )
        print("no local service entries and no k8s section; nothing to check")
        return
    config = devkit
    k8s_rendering = config.k8s is not None

    if not k8s_rendering and target == "k8s":
        raise typer.BadParameter("--target k8s: this config declares no k8s section (compose-only)")
    if not k8s_rendering and local:
        raise typer.BadParameter("--local writes the k8s artifact; this config declares no k8s section")

    if check:
        # Fetch the pinned tools rather than trusting whatever PATH holds, so the gate always
        # regenerates with the exact versions the consumer pinned.
        with TemporaryDirectory(prefix="score-tools-") as temporary_directory:
            bin_dir = Path(temporary_directory) / "bin"
            bin_dir.mkdir()
            ensure_score_tools(config, bin_dir, target)
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
            _generate_artifacts(root, config, target, False)
            outputs = [bundle.output for bundle in compose_bundles(config)]
            if k8s_rendering:
                outputs.append(config.k8s_output)
            if (root / SECRETS_FILE).is_file():
                outputs.append(SECRETS_OUTPUT)
            pathspec = " ".join((root / output).as_posix() for output in outputs)
            if bash_output(f"git status --porcelain -- {pathspec}").strip():
                bash(f"git diff -- {pathspec}")
                raise SystemExit(
                    "score artifacts are stale. Run 'generate-score' and commit the result as 'Run generate-score'."
                )
        return

    # Refuse to run without the exact pinned tools, since output differs between releases.
    missing = [item for item in tool_requirements(config, target) if shutil.which(item.binary) is None]
    if missing:
        lines = [f"  {item.binary} {item.version}: {item.release}" for item in missing]
        raise RuntimeError(
            "generate-score needs these on PATH, at these exact versions (output differs between releases):\n"
            + "\n".join(lines)
        )
    _generate_artifacts(root, config, target, local)


def _generate_artifacts(root: Path, config: DockerDevkitConfig, target: Target, local: bool) -> None:
    # Resolve image tags from the committed tree, exactly as up and build do; the artifacts
    # embed tree tags, so the build keys are required.
    if not (config.repository or config.base_images):
        raise RuntimeError(
            "the build keys (repository or base_images) are required in docker-devkit.yaml — "
            "score artifacts embed the tree tags they derive"
        )
    os.environ.update(compute_service_shas(root, config))
    if config.k8s is not None:
        os.environ[STORAGE_CLASS_VAR] = LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS
    # The lock beside the manifest is the single digest source for authored lock markers.
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock is required for generation (build writes it)")
    images = parse_image_lock(lock_path).root
    if not images:
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock carries no entries")
    k8s_output = config.k8s_local_output if local else config.k8s_output

    if target in ("both", "compose"):
        generate_compose(root, config, images)

    if target in ("both", "k8s") and config.k8s is not None:
        generate_k8s(root, config, k8s_output, images)

    if (root / SECRETS_FILE).is_file():
        enforce_secret_contract(root, config, target, k8s_output)


def tool_requirements(config: DockerDevkitConfig, target: Target) -> list[ToolRequirement]:
    score_tools: list[tuple[str, str]] = []
    if target in ("both", "compose"):
        score_tools.append(("score-compose", config.score_compose_version))
    if target in ("both", "k8s") and config.k8s is not None:
        score_tools.append(("score-k8s", config.score_k8s_version))
    requirements = [
        ToolRequirement(
            binary=binary,
            version=version,
            release=f"https://github.com/score-spec/{binary}/releases/tag/{version}",
            archive=f"https://github.com/score-spec/{binary}/releases/download/{version}/{binary}_{version}_linux_amd64.tar.gz",
            extract=binary,
        )
        for binary, version in score_tools
    ]
    # Strip one path component for helm, whose archive nests its binary under linux-amd64/.
    if config.k8s is not None and config.k8s.charts and target in ("both", "k8s"):
        version = config.helm_version
        requirements.append(
            ToolRequirement(
                binary="helm",
                version=version,
                release=f"https://github.com/helm/helm/releases/tag/v{version}",
                archive=f"https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz",
                extract="--strip-components=1 linux-amd64/helm",
            )
        )
    return requirements


def generate_compose(root: Path, config: DockerDevkitConfig, images: dict[str, str]) -> None:
    bundles = compose_bundles(config)
    if not bundles:
        raise RuntimeError(
            "the compose target requires local service entries (workloads:) under docker-devkit.yaml's services key"
        )
    score_dir = root / config.score_dir
    score_state = score_dir / ".score-compose"

    with _builtin_patch_templates() as builtin_templates:
        for bundle in bundles:
            interpolated_targets: set[str] = set()
            for name in bundle.workloads:
                interpolated_targets |= _validate_workload_surface(name, _load_yaml_file(score_dir / name))
            provisioner_names = closure_provisioners(score_dir, "compose", bundle.workloads)
            # The state dir is re-created per bundle so one bundle's provisioners and patch
            # templates never leak into another's output.
            with compiled_provisioners(score_dir, provisioner_names, root, images) as provisioners:
                # The wait-for-resources gate exists exactly when provisioner-added services do,
                # and the built-in gate-pin template rewrites its image through the lock — so a
                # gate-forming run without the lock entry refuses here instead of shipping a
                # bare `alpine` in the artifact (templates are data and cannot refuse).
                if provisioners.declares_services and "ALPINE_IMAGE" not in images:
                    raise RuntimeError(
                        f"bundle {bundle.name!r}: its provisioners declare services, so score-compose "
                        f"synthesizes a wait-for-resources gate; {LOCK_PATH.as_posix()} lacks the "
                        "ALPINE_IMAGE entry the gate pin needs (build writes it)"
                    )
                templates = [
                    builtin_templates / HEALTHCHECK_TEMPLATE,
                    builtin_templates / RESTART_TEMPLATE,
                ]
                if provisioners.declares_services:
                    templates.append(builtin_templates / GATE_PIN_TEMPLATE)
                gpu_template = GPU_COMPOSE_TEMPLATES.get(bundle.vars.get("GPU_VARIANT", ""))
                if gpu_template is not None:
                    templates.append(builtin_templates / gpu_template)
                shutil.rmtree(score_state, ignore_errors=True)
                _init_state(
                    f"score-compose init --project {config.project_name} --no-sample",
                    provisioners.paths,
                    templates,
                    working_dir=score_dir,
                )

                output = root / bundle.output
                output.parent.mkdir(parents=True, exist_ok=True)
                with _rendered_workloads(root, config, bundle.workloads, bundle.vars, images) as workloads:
                    bash(
                        f"score-compose generate {workloads} --output {output.as_posix()}",
                        cwd=score_dir,
                    )

                # Collapse doubled {workload}-{container} service names (api-api -> api) so bundle
                # artifacts carry the fleet-wide vocabulary; references never use the doubled form,
                # and a collapse onto an existing service name is a loud collision.
                document = yaml.safe_load(output.read_text(encoding="utf-8"))
                services: dict[str, Any] = document.get("services") or {}
                collapsed: dict[str, Any] = {}
                for name, service in services.items():
                    match = DOUBLED_SERVICE_NAME.fullmatch(name)
                    target = match.group(1) if match is not None else name
                    if target != name and target in services:
                        raise SystemExit(f"service rename {name} -> {target} collides with an existing service")
                    collapsed[target] = service
                document["services"] = collapsed

                # score-compose synthesizes the gate under one fixed name per rendering, and
                # compose includes merge same-name services silently — so each entry's
                # artifact names its own gate and the depends_on references follow.
                gate = f"{GATE_SERVICE}-{bundle.name}"
                if GATE_SERVICE in collapsed:
                    if gate in collapsed:
                        raise SystemExit(f"gate rename {GATE_SERVICE} -> {gate} collides with an existing service")
                    collapsed[gate] = collapsed.pop(GATE_SERVICE)
                    for service in collapsed.values():
                        depends_on: dict[str, Any] | list[str] | None = service.get("depends_on")
                        if isinstance(depends_on, dict) and GATE_SERVICE in depends_on:
                            depends_on[gate] = depends_on.pop(GATE_SERVICE)
                        elif isinstance(depends_on, list) and GATE_SERVICE in depends_on:
                            service["depends_on"] = [
                                gate if dependency == GATE_SERVICE else dependency for dependency in depends_on
                            ]

                # Inline every bind as configs.content with $ doubled, leaving zero host-filesystem references.
                parsed = ComposeDocument.model_validate(document)
                contents: dict[str, dict[str, str]] = {}
                for name, service in (parsed.services or {}).items():
                    binds = [volume for volume in service.volumes or [] if isinstance(volume, BindVolume)]
                    if not binds:
                        continue
                    references: list[dict[str, str]] = []
                    for bind in binds:
                        # Resolve state-dir sources against score_dir, other sources against the repo root.
                        path = score_dir / bind.source if bind.source.startswith(STATE_PREFIX) else root / bind.source
                        text = path.read_text(encoding="utf-8")
                        if bind.target not in interpolated_targets:
                            # App-owned text doubles $ so compose interpolation leaves it alone; a
                            # file named by the consumer-interpolation annotation rides verbatim,
                            # its single-$ holes becoming the include consumer's interpolation
                            # surface.
                            text = text.replace("$", "$$")
                        contents[path.name] = {"content": text}
                        references.append({"source": path.name, "target": bind.target})
                    raw = document["services"][name]
                    raw["configs"] = [*(raw.get("configs") or []), *references]
                    remaining = [volume for volume in raw["volumes"] if volume.get("type") != "bind"]
                    if remaining:
                        raw["volumes"] = remaining
                    else:
                        del raw["volumes"]

                if contents:
                    document["configs"] = {**(document.get("configs") or {}), **contents}
                write_generated_file(output, dump_compose(document))

    # The compose state dir is per-run temp; deleting it keeps the next run provisioning from scratch.
    shutil.rmtree(score_state, ignore_errors=True)


@contextmanager
def _builtin_patch_templates() -> Generator[Path]:
    # importlib.resources is the only resolver that finds the assets in wheel, sdist, and
    # editable installs alike.
    with as_file(files("docker_devkit") / PATCH_TEMPLATES_DIR) as directory:
        yield directory


def _validate_workload_surface(name: str, document: Any) -> set[str]:
    # The deployment-surface refusal laws the shipped templates cannot carry, checked
    # against the authored workload document. Returns the container file paths whose
    # binds ride un-doubled (the consumer-interpolation annotation's names).
    metadata: dict[str, Any] = document.get("metadata") or {}
    annotations: dict[str, Any] = metadata.get("annotations") or {}
    containers: dict[str, Any] = document.get("containers") or {}
    adapter = annotations.get(PROBE_ADAPTER_ANNOTATION)
    if adapter is not None and adapter not in PROBE_ADAPTERS:
        raise SystemExit(
            f"{name}: {PROBE_ADAPTER_ANNOTATION} is {adapter!r}; the supported adapters are {list(PROBE_ADAPTERS)}"
        )
    restart = annotations.get(RESTART_ANNOTATION)
    if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
        raise SystemExit(
            f"{name}: {RESTART_ANNOTATION} is {restart!r}; the compose restart policies are "
            f"{list(COMPOSE_RESTART_POLICIES)}"
        )
    if annotations.get(PUBLISH_ANNOTATION) is not None:
        raise SystemExit(
            f"{name}: {PUBLISH_ANNOTATION} died — host publishes are consumer config now, "
            "declared as ports under docker-devkit.yaml's services key"
        )
    interpolated: set[str] = set()
    consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
    if consumer_interpolated is not None:
        if not isinstance(consumer_interpolated, str) or not consumer_interpolated.strip():
            raise SystemExit(
                f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} must be a non-empty comma-separated "
                "list of the workload's files: paths"
            )
        declared_files: set[str] = set()
        for container in containers.values():
            files: dict[str, Any] = container.get("files") or {}
            declared_files.update(files)
        for entry in consumer_interpolated.split(","):
            if entry != entry.strip() or not entry:
                raise SystemExit(
                    f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} is empty or whitespace-padded"
                )
            if entry not in declared_files:
                raise SystemExit(
                    f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} names {entry!r}, which no container's files: declares"
                )
            interpolated.add(entry)
    if adapter is None:
        # score-compose drops httpGet probes, so compose healthchecks exist only through
        # the adapter's conversion — a probe without one is the silent-drop failure mode.
        for container in containers.values():
            for probe in ("readinessProbe", "livenessProbe"):
                probe_block: dict[str, Any] = container.get(probe) or {}
                if probe_block.get("httpGet") is not None:
                    raise SystemExit(
                        f"{name}: a {probe} httpGet exists but {PROBE_ADAPTER_ANNOTATION} is not "
                        "declared; score-compose drops httpGet probes, so the compose healthcheck "
                        "needs the adapter"
                    )
    return interpolated


def closure_provisioners(score_dir: Path, rendering: str, workloads: list[str]) -> list[str]:
    # A rendering registers exactly the provisioner files whose types its workloads
    # reference; orphan files (a type nothing references) silently no-ship.
    referenced: set[str] = set()
    for name in workloads:
        document: Any = _load_yaml_file(score_dir / name)
        resources = document.get("resources") if is_mapping(document) else None
        if is_mapping(resources):
            for resource in resources.values():
                if is_mapping(resource) and isinstance(resource.get("type"), str):
                    referenced.add(resource["type"])
    selected: list[str] = []
    directory = score_dir / rendering
    if not directory.is_dir():
        return selected
    for path in sorted(directory.glob("*.yaml")):
        entries: Any = _load_yaml_file(path)
        if not is_list(entries):
            continue
        if any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
            selected.append(f"{rendering}/{path.name}")
    return selected


def generate_k8s(root: Path, config: DockerDevkitConfig, output: Path, images: dict[str, str]) -> None:
    # Run score-k8s in the output's directory; it writes its .score-k8s state under its cwd.
    k8s = config.k8s
    if k8s is None:
        raise RuntimeError("generate_k8s requires a k8s section in docker-devkit.yaml")
    output_path = root / output
    state_dir = output_path.parent / ".score-k8s"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    score_dir = root / config.score_dir

    with (
        _builtin_patch_templates() as builtin_templates,
        compiled_provisioners(
            score_dir, closure_provisioners(score_dir, "k8s", k8s.workloads), root, images
        ) as provisioners,
    ):
        _init_state(
            "score-k8s init --no-sample",
            provisioners.paths,
            [
                builtin_templates / GPU_POD_TEMPLATE,
                builtin_templates / SCALE_TO_ZERO_TEMPLATE,
            ],
            working_dir=output_path.parent,
        )

        with _rendered_workloads(root, config, k8s.workloads, k8s.vars, images) as workloads:
            bash(f"score-k8s generate {workloads} --output {output_path.as_posix()}", cwd=output_path.parent)

    # Render charts through helm template into the same sorted documents; the lock is the
    # digest source, so helm renders the org-mirrored digest consumers pull — helm is a
    # renderer, never a deployer.
    if k8s.charts:
        chart_blocks: list[str] = []
        with TemporaryDirectory(prefix="resolved-values-") as values_directory:
            values_dir = Path(values_directory)
            for chart_name in k8s.charts:
                key = chart_lock_key(chart_name)
                entry = images.get(key)
                if entry is None:
                    raise RuntimeError(
                        f"chart {chart_name!r}: {LOCK_PATH.as_posix()} lacks the {key} entry; "
                        "build --lock-only resolves and writes it"
                    )
                # Pass a marker-free values file through untouched; resolve markers into a tempdir copy.
                values_arguments: list[str] = []
                path = root / chart_values_path(config.score_dir, chart_name)
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not contains_marker(document):
                    values_arguments.append(path.as_posix())
                else:
                    resolved = resolve_lock_refs(
                        resolve_includes(document, root, path.as_posix()), images, path.as_posix()
                    )
                    target = values_dir / chart_values_path(config.score_dir, chart_name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_generated_file(target, dump_compose(resolved))
                    values_arguments.append(target.as_posix())
                values = "".join(f" --values {argument}" for argument in values_arguments)
                rendered = bash_output(f"helm template {chart_name} oci://{entry}{values}")
                chart_blocks.extend(_split_documents(rendered))
        write_generated_file(
            output_path,
            output_path.read_text(encoding="utf-8") + "".join(f"---\n{block}" for block in chart_blocks),
        )

    # Sort documents by kind, namespace, and name, since score-k8s emits in randomised map order.
    documents = _split_documents(output_path.read_text(encoding="utf-8"))
    decorated: list[tuple[str, str, str, int, str]] = []
    for index, block in enumerate(documents):
        manifest = ManifestDocument.model_validate(yaml.safe_load(block) or {})
        metadata = manifest.metadata or ManifestMetadata()
        decorated.append((manifest.kind, metadata.namespace, metadata.name, index, block))
    decorated.sort()
    write_generated_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    # Reduce recorded workload paths to bare filenames so the state file reproduces.
    state_path = state_dir / "state.yaml"
    state_text = state_path.read_text(encoding="utf-8")
    for name in k8s.workloads:
        state_text = re.sub(rf"(?m)^(\s*file:\s*).*/{re.escape(name)}$", rf"\g<1>{name}", state_text)
    write_generated_file(state_path, state_text)

    # Rewrite each random instance_suffix to a name-derived hash so output reproduces from fresh state.
    state = ScoreState.model_validate(yaml.safe_load(state_text) or {})
    manifest_text = output_path.read_text(encoding="utf-8")
    for name, entry in state.workloads.items():
        current = entry.instance_suffix
        if not current:
            continue
        wanted = "-" + sha256(name.encode()).hexdigest()[:10]
        if current == wanted:
            continue
        state_text = state_text.replace(f"instance_suffix: {current}", f"instance_suffix: {wanted}")
        manifest_text = manifest_text.replace(f"{name}{current}", f"{name}{wanted}")
    write_generated_file(state_path, state_text)
    write_generated_file(output_path, manifest_text)

    shutil.rmtree(state_dir, ignore_errors=True)


def enforce_secret_contract(root: Path, config: DockerDevkitConfig, target: Target, k8s_output: Path) -> None:
    # Validate each rendering this run generated against the declaration, and derive the mint
    # list from the declaration alone. The contract activates by the declaration file's
    # presence; the cluster Secret name derives from the compose project name.
    declaration_path = root / SECRETS_FILE
    if not declaration_path.is_file():
        raise RuntimeError(f"{declaration_path.as_posix()}: the secrets declaration file does not exist")
    k8s_secret = f"{config.project_name}-secrets"

    declared = SecretsDeclaration.model_validate(
        yaml.safe_load(declaration_path.read_text(encoding="utf-8")),
        context={"source": declaration_path.as_posix()},
    ).root

    if target in ("both", "compose"):
        consumed: set[str] = set()
        for bundle in compose_bundles(config):
            artifact = root / bundle.output
            for match in COMPOSE_HOLE.finditer(artifact.read_text(encoding="utf-8")):
                if len(match.group(1)) % 2 == 1:
                    consumed.add(match.group(2))
        validate_consumed(consumed, declared, "compose", SECRETS_FILE.as_posix())

    if target in ("both", "k8s") and config.k8s is not None:
        artifact = root / k8s_output
        consumed = set()
        for block in _split_documents(artifact.read_text(encoding="utf-8")):
            collect_secret_keys(yaml.safe_load(block), k8s_secret, consumed)
        validate_consumed(consumed, declared, "k8s", SECRETS_FILE.as_posix())

    output = root / SECRETS_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    write_generated_file(
        output,
        "# Generated by generate-score from the secrets declaration\n"
        + "".join(f"{name}\n" for name in sorted(name for name, key in declared.items() if key.mint)),
    )


def ensure_score_tools(config: DockerDevkitConfig, bin_dir: Path, target: Target = "both") -> None:
    # Fetch release binaries rather than go install, since score-spec tags are not valid Go module versions.
    for requirement in tool_requirements(config, target):
        archive = Path(requirement.archive).name
        bash(f"curl -fsSLO {requirement.archive}")
        bash(f"tar -xzf {archive} -C {bin_dir} {requirement.extract}")
        Path(archive).unlink()


def _init_state(
    command: str,
    provisioners: list[Path],
    patch_templates: list[Path],
    *,
    working_dir: Path,
) -> None:
    # Init idempotently with POSIX relative paths, so no machine path or separator reaches a state file.
    flags = "".join(
        f" --provisioners {shlex.quote(Path(os.path.relpath(provisioner, working_dir)).as_posix())}"
        for provisioner in provisioners
    )
    flags += "".join(
        f" --patch-templates {shlex.quote(Path(os.path.relpath(patch_template, working_dir)).as_posix())}"
        for patch_template in patch_templates
    )
    bash(f"{command}{flags}", cwd=working_dir)


@contextmanager
def _rendered_workloads(
    root: Path,
    config: DockerDevkitConfig,
    workloads: list[str],
    rendering_vars: dict[str, str],
    images: dict[str, str],
) -> Generator[str]:
    # Expand ${ALL_CAPS} placeholders into tempdir copies, since Score documents cannot read the
    # environment. Holes fill from the rendering's vars map first (bundle vars for compose,
    # k8s.vars for k8s), then the environment (the tree-tag SHAs); ${VAR:?err}/${VAR:-}
    # runtime holes never match the bare-placeholder pattern, so they ride through to compose
    # interpolation untouched. Lock markers resolve first and structurally — a workload's
    # image field is metadata score never substitutes outputs into, so a third-party image
    # rides as {lock: NAME} and lands digest-pinned in both renderings.
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)

        for name in workloads:
            source_path = root / config.score_dir / name
            document = resolve_lock_refs(_load_yaml_file(source_path), images, f"workload {name}")
            source = yaml.safe_dump(document, sort_keys=False, width=1000)
            parts: list[str] = []
            index = 0
            for match in PLACEHOLDER.finditer(source):
                parts.append(source[index : match.start()])
                variable = match.group(1)
                value = rendering_vars.get(variable) or os.environ.get(variable)
                if not value:
                    raise SystemExit(
                        f"{name}: ${{{variable}}} is set in neither the rendering's vars map nor the "
                        "environment (service SHAs derive from the build keys)"
                    )
                parts.append(value)
                index = match.end()
            parts.append(source[index:])
            (directory / name).write_text("".join(parts), "utf-8")

        yield " ".join((directory / name).as_posix() for name in workloads)


def write_generated_file(path: Path, text: str) -> None:
    # Write with newline="" so a Windows run does not rewrite line endings as CRLF.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    # Normalise to 0644, since score-k8s writes its state file executable.
    path.chmod(0o644)


def _load_yaml_file(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _split_documents(text: str) -> list[str]:
    return [block for block in re.split(r"(?m)^---$\n", text) if block.strip()]
