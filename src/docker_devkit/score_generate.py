from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, NamedTuple, Self

import typer
import yaml
from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .config import ScoreConfig, SecretsConfig, load_config
from .documents import compute_service_shas, parse_bake
from .markers import contains_marker, resolve_includes, resolve_lock_refs
from .mirror import mirrored_ref
from .provisioners import compiled_provisioners, dump_compose
from .secrets import SecretKey, collect_secret_keys, is_mapping, validate_consumed

# Match only ALL-CAPS placeholders so Score's lowercase dotted references never collide.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Treat sources under this prefix as relative to score-compose's state directory.
STATE_PREFIX = ".score-compose/"

# Declaration keys are ALL_CAPS; count a brace hole as consumed when an odd number of dollars
# precedes it, since even runs are $$-escaped literals.
DECLARED_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
COMPOSE_HOLE = re.compile(r"(\$+)\{([A-Z][A-Z0-9_]*)(?::[^{}]*)?\}")

Target = Literal["both", "compose", "k8s"]


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""
    name: str = ""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    metadata: ManifestMetadata | None = None


class WorkloadState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instance_suffix: str = ""


class ScoreState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workloads: dict[str, WorkloadState] = {}


class BindVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["bind"]
    source: str
    target: str


class OtherVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str

    # Reject malformed binds so they fail validation instead of passing through as non-binds.
    @model_validator(mode="after")
    def _reject_bind(self) -> Self:
        if self.type == "bind":
            raise ValueError("a bind volume is missing its source or target")
        return self


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] | None = None


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
) -> None:
    root = Path.cwd()
    devkit = load_config(root)
    if devkit is None or devkit.generate_score is None:
        raise RuntimeError("a generate-score section is required in docker-devkit.yaml")
    config = devkit.generate_score
    # Refuse to run without the exact pinned tools, since output differs between releases.
    missing = [item for item in tool_requirements(config, target) if shutil.which(item.binary) is None]
    if missing:
        lines = [f"  {item.binary} {item.version}: {item.release}" for item in missing]
        raise RuntimeError(
            "generate-score needs these on PATH, at these exact versions (output differs between releases):\n"
            + "\n".join(lines)
        )

    # Resolve image tags from the committed tree, exactly as up and build do.
    os.environ.update(compute_service_shas(root, parse_bake(config.bake_file)))
    os.environ[config.storage_class_var] = config.local_storage_class if local else config.cloud_storage_class
    # The lock beside the manifest is the single digest source for authored lock markers.
    lock_path = root / config.bake_file.with_suffix(".lock")
    if not lock_path.is_file():
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock is required for generation (build writes it)")
    images: dict[str, str] = {}
    for number, line in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, reference = stripped.partition("=")
        if not separator or not name.strip() or not reference.strip():
            raise RuntimeError(f"{lock_path.as_posix()}:{number}: lock lines are NAME=reference entries")
        if name in images:
            raise RuntimeError(f"{lock_path.as_posix()}:{number}: duplicate lock entry {name}")
        images[name] = reference
    if not images:
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock carries no entries")
    k8s_output = config.k8s_local_output if local else config.k8s_output

    if target in ("both", "compose"):
        generate_compose(root, config, images)

    if target in ("both", "k8s"):
        mirror_prefix = devkit.mirror.prefix if devkit.mirror is not None else None
        generate_k8s(root, config, k8s_output, mirror_prefix, images)

    if config.secrets is not None:
        enforce_secret_contract(root, config, target, k8s_output, config.secrets)


def tool_requirements(config: ScoreConfig, target: Target) -> list[ToolRequirement]:
    score_tools: list[tuple[str, str]] = []
    if target in ("both", "compose"):
        score_tools.append(("score-compose", config.score_compose_version))
    if target in ("both", "k8s"):
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
    if config.charts and target in ("both", "k8s"):
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


def generate_compose(root: Path, config: ScoreConfig, images: dict[str, str]) -> None:
    score_dir = root / config.score_dir

    with compiled_provisioners(score_dir, config.compose_provisioners, root, images) as provisioners:
        _init_state(
            f"score-compose init --project {config.project_name} --no-sample",
            provisioners,
            [score_dir / name for name in config.compose_patch_templates],
            working_dir=score_dir,
        )

        (root / config.compose_output).parent.mkdir(parents=True, exist_ok=True)
        publishes = "".join(f" --publish {publish}" for publish in config.publishes)
        with _rendered_workloads(root, config) as workloads:
            bash(
                f"score-compose generate {workloads} --output {(root / config.compose_output).as_posix()}{publishes}",
                cwd=score_dir,
            )

    # Inline every bind as configs.content with $ doubled, leaving zero host-filesystem references.
    document = yaml.safe_load((root / config.compose_output).read_text(encoding="utf-8"))
    parsed = ComposeDocument.model_validate(document)
    contents: dict[str, dict[str, str]] = {}
    resolved: dict[str, Path] = {}
    for name, service in (parsed.services or {}).items():
        binds = [volume for volume in service.volumes or [] if isinstance(volume, BindVolume)]
        if not binds:
            continue
        references: list[dict[str, str]] = []
        for bind in binds:
            # Resolve state-dir sources against score_dir, other relative sources against the repo root.
            if bind.source.startswith(STATE_PREFIX):
                path = score_dir / bind.source
            elif Path(bind.source).is_absolute():
                raise SystemExit(f"absolute bind source {bind.source} cannot be inlined hermetically")
            else:
                path = root / bind.source
            if resolved.setdefault(path.name, path) != path:
                raise SystemExit(f"bind sources {resolved[path.name]} and {path} collide on config name {path.name}")
            contents[path.name] = {"content": path.read_text(encoding="utf-8").replace("$", "$$")}
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
        write_generated_file(root / config.compose_output, dump_compose(document))

    # The compose state dir is per-run temp; deleting it keeps the next run provisioning from scratch.
    shutil.rmtree(score_dir / ".score-compose", ignore_errors=True)


def generate_k8s(
    root: Path, config: ScoreConfig, output: Path, mirror_prefix: str | None, images: dict[str, str]
) -> None:
    # Run score-k8s beside the configured state dir; it writes .score-k8s under its cwd.
    state_dir = root / config.k8s_state
    output_path = root / output
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with compiled_provisioners(root / config.score_dir, config.k8s_provisioners, root, images) as provisioners:
        _init_state(
            "score-k8s init --no-sample",
            provisioners,
            [],
            working_dir=state_dir.parent,
        )

        with _rendered_workloads(root, config) as workloads:
            bash(f"score-k8s generate {workloads} --output {output_path.as_posix()}", cwd=state_dir.parent)

    # Render charts through helm template into the same sorted documents, pulling each
    # digest-pinned upstream ref from the org mirror; helm is a renderer, never a deployer.
    if config.charts:
        if mirror_prefix is None:
            raise RuntimeError(
                "charts are org-mirrored; a mirror section with 'prefix' is required in docker-devkit.yaml"
            )
        chart_blocks: list[str] = []
        with TemporaryDirectory(prefix="resolved-values-") as values_directory:
            values_dir = Path(values_directory)
            for chart in config.charts:
                reference = mirrored_ref(chart.ref.removeprefix("oci://"), mirror_prefix)
                # Pass a marker-free values file through untouched; resolve markers into a tempdir copy.
                values_arguments: list[str] = []
                for path in chart.values:
                    source = root / path
                    document = yaml.safe_load(source.read_text(encoding="utf-8"))
                    if not contains_marker(document):
                        values_arguments.append(source.as_posix())
                        continue
                    resolved = resolve_lock_refs(
                        resolve_includes(document, root, path.as_posix()), images, path.as_posix()
                    )
                    target = values_dir / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_generated_file(target, dump_compose(resolved))
                    values_arguments.append(target.as_posix())
                values = "".join(f" --values {argument}" for argument in values_arguments)
                namespace = f" --namespace {chart.namespace}" if chart.namespace else ""
                rendered = bash_output(f"helm template {chart.name} oci://{reference}{values}{namespace}")
                chart_blocks.extend(block for block in re.split(r"(?m)^---$\n", rendered) if block.strip())
        write_generated_file(
            output_path,
            output_path.read_text(encoding="utf-8") + "".join(f"---\n{block}" for block in chart_blocks),
        )

    # Sort documents by kind, namespace, and name, since score-k8s emits in randomised map order.
    documents = [block for block in re.split(r"(?m)^---$\n", output_path.read_text(encoding="utf-8")) if block.strip()]
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
    for name in config.workloads:
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


def enforce_secret_contract(
    root: Path, config: ScoreConfig, target: Target, k8s_output: Path, secrets: SecretsConfig
) -> None:
    # Validate each rendering this run generated against the declaration, and derive the mint
    # list from the declaration alone.
    declaration_path = root / secrets.file
    if not declaration_path.is_file():
        raise RuntimeError(f"{declaration_path.as_posix()}: the secrets declaration file does not exist")

    declaration_document = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    if not is_mapping(declaration_document) or not declaration_document:
        raise RuntimeError(
            f"{declaration_path.as_posix()}: the secrets declaration is a non-empty KEY: description mapping"
        )

    declared: dict[str, SecretKey] = {}
    for name, spec in declaration_document.items():
        if not isinstance(name, str) or not DECLARED_KEY.match(name):
            raise RuntimeError(f"{declaration_path.as_posix()}: {name!r} is not an ALL_CAPS key")
        if isinstance(spec, str):
            spec = {"description": spec}
        try:
            declared[name] = SecretKey.model_validate(spec)
        except ValidationError as error:
            raise RuntimeError(f"{declaration_path.as_posix()}: key {name} rejected: {error}") from error

    if target in ("both", "compose"):
        artifact = root / config.compose_output
        consumed: set[str] = set()
        for match in COMPOSE_HOLE.finditer(artifact.read_text(encoding="utf-8")):
            if len(match.group(1)) % 2 == 1:
                consumed.add(match.group(2))
        validate_consumed(consumed, declared, "compose", secrets.file.as_posix())

    if target in ("both", "k8s"):
        artifact = root / k8s_output
        consumed = set()
        for block in re.split(r"(?m)^---$\n", artifact.read_text(encoding="utf-8")):
            if block.strip():
                collect_secret_keys(yaml.safe_load(block), secrets.k8s_secret, consumed)
        validate_consumed(consumed, declared, "k8s", secrets.file.as_posix())

    output = root / secrets.output
    output.parent.mkdir(parents=True, exist_ok=True)
    write_generated_file(
        output,
        "# Generated by generate-score from the secrets declaration\n"
        + "".join(f"{name}\n" for name in sorted(name for name, key in declared.items() if key.mint)),
    )


def ensure_score_tools(config: ScoreConfig, bin_dir: Path, target: Target = "both") -> None:
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
def _rendered_workloads(root: Path, config: ScoreConfig) -> Generator[str]:
    # Expand ${ALL_CAPS} placeholders into tempdir copies, since Score documents cannot read the environment.
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)

        for name in config.workloads:
            source = (root / config.score_dir / name).read_text(encoding="utf-8")
            parts: list[str] = []
            index = 0
            for match in PLACEHOLDER.finditer(source):
                parts.append(source[index : match.start()])
                variable = match.group(1)
                value = os.environ.get(variable)
                if not value:
                    raise SystemExit(f"{name}: ${{{variable}}} is not set. Is it a service in {config.bake_file}?")
                parts.append(value)
                index = match.end()
            parts.append(source[index:])
            (directory / name).write_text("".join(parts), "utf-8")

        yield " ".join((directory / name).as_posix() for name in config.workloads)


def write_generated_file(path: Path, text: str) -> None:
    # Write with newline="" so a Windows run does not rewrite line endings as CRLF.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    # Normalise to 0644, since score-k8s writes its state file executable.
    path.chmod(0o644)
