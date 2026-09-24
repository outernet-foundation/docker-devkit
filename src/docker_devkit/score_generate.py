from __future__ import annotations

import os
import re
import shlex
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

import typer
import yaml
from bashrun.bash import bash
from pydantic import BaseModel, ConfigDict, Field

from .context_sha import compute_service_shas
from .documents import parse_bake
from .image_refs import TomlDocument, TomlValue

# Placeholders in the workload files are ALL-CAPS, so this cannot collide with Score's own
# lowercase dotted ${resources.db.host} references.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

Target = Literal["both", "compose", "k8s"]


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""
    name: str = ""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    metadata: ManifestMetadata | None = None


class ScoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str]
    project_name: str
    cloud_storage_class: str
    local_storage_class: str
    score_dir: Path = Path("stack/score")
    bake_file: Path = Path("workloads/images.yml")
    compose_output: Path = Path("stack/generated/compose/compose.yaml")
    k8s_output: Path = Path("stack/generated/k8s/manifests.yaml")
    # --local writes here instead of the committed k8s output; consumers gitignore this path so a
    # local-only storage class cannot reach the artifact their cluster deploys.
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    k8s_state: Path = Path("stack/generated/k8s/.score-k8s")
    storage_class_var: str = "SCORE_STORAGE_CLASS"
    compose_provisioners: list[str] = Field(default_factory=list)
    compose_patch_templates: list[str] = Field(default_factory=list)
    k8s_provisioners: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    score_k8s_version: str = "0.15.0"
    score_compose_version: str = "0.42.0"


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
    generate(load_score_config(Path.cwd()), target, local)


def load_score_config(root: Path) -> ScoreConfig:
    value: TomlValue = TomlDocument.model_validate(
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    ).root
    for component in ("tool", "docker-devkit", "generate-score"):
        if not isinstance(value, dict) or component not in value:
            raise RuntimeError(
                f"[tool.docker-devkit.generate-score] is missing at {component} in {root / 'pyproject.toml'}"
            )
        value = value[component]
    return ScoreConfig.model_validate(value)


def generate(config: ScoreConfig, target: Target = "both", local: bool = False) -> None:
    root = Path.cwd()
    # The same call up, build, and preflight make, so every image tag is a pure function of
    # committed source rather than a hand-typed value.
    os.environ.update(compute_service_shas(root, parse_bake(config.bake_file)))
    os.environ[config.storage_class_var] = config.local_storage_class if local else config.cloud_storage_class

    if target in ("both", "compose"):
        _generate_compose(root, config)

    if target in ("both", "k8s"):
        _generate_k8s(root, config, config.k8s_local_output if local else config.k8s_output)


def ensure_score_tools(config: ScoreConfig, bin_dir: Path) -> None:
    # Fetched as release binaries rather than `go install`: score-spec tags without a leading v
    # (0.15.0, not v0.15.0), which is not a resolvable Go module version.
    for tool, version in (("score-k8s", config.score_k8s_version), ("score-compose", config.score_compose_version)):
        archive = f"{tool}_{version}_linux_amd64.tar.gz"
        bash(f"curl -fsSLO https://github.com/score-spec/{tool}/releases/download/{version}/{archive}")
        bash(f"tar -xzf {archive} -C {bin_dir} {tool}")
        Path(archive).unlink()


def _generate_compose(root: Path, config: ScoreConfig) -> None:
    # score-compose keeps its state directory inside score_dir; the consumer gitignores it.
    _init_state(
        f"score-compose init --project {config.project_name} --no-sample",
        config.compose_provisioners,
        config.compose_patch_templates,
        source_dir=root / config.score_dir,
        working_dir=root / config.score_dir,
    )

    (root / config.compose_output).parent.mkdir(parents=True, exist_ok=True)
    publishes = "".join(f" --publish {publish}" for publish in config.publishes)
    with _rendered_workloads(root, config) as workloads:
        bash(
            f"score-compose generate {workloads} --output {(root / config.compose_output).as_posix()}{publishes}",
            cwd=root / config.score_dir,
        )


def _generate_k8s(root: Path, config: ScoreConfig, output: Path) -> None:
    # score-k8s writes its state directory to the .score-k8s of its cwd; running it from the
    # generated k8s home keeps the committed state file in the generated tier while the
    # provisioners still resolve from the authored score sources.
    state_dir = root / config.k8s_state
    _init_state(
        "score-k8s init --no-sample",
        config.k8s_provisioners,
        [],
        source_dir=root / config.score_dir,
        working_dir=state_dir.parent,
    )

    output_path = root / output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with _rendered_workloads(root, config) as workloads:
        bash(f"score-k8s generate {workloads} --output {output_path.as_posix()}", cwd=state_dir.parent)

    sort_documents(output_path)
    normalise_state_paths(config, state_dir / "state.yaml")


def _init_state(
    command: str,
    provisioners: list[str],
    patch_templates: list[str],
    *,
    source_dir: Path,
    working_dir: Path,
) -> None:
    # init is idempotent over an existing state directory: it rewrites the provisioner copies
    # without disturbing state. The state must survive, because score-k8s mints a random uid per
    # workload on first add and emits it as the app.kubernetes.io/instance label — discarding it
    # would change the generated manifests on every run.
    # Provisioner and template paths are passed relative to the working directory so no
    # absolute machine path can reach a committed state file.
    flags = "".join(
        f" --provisioners {shlex.quote(os.path.relpath(source_dir / name, working_dir))}" for name in provisioners
    )
    flags += "".join(
        f" --patch-templates {shlex.quote(os.path.relpath(source_dir / name, working_dir))}" for name in patch_templates
    )
    bash(f"{command}{flags}", cwd=working_dir)


@contextmanager
def _rendered_workloads(root: Path, config: ScoreConfig) -> Generator[str]:
    # Workload files are Score spec documents, not Go templates, so they cannot read the
    # environment the way the provisioner files can. Expanding ${API_SHA} into a rendered copy
    # keeps one mechanism across both halves.
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)

        for name in config.workloads:
            source = (root / config.score_dir / name).read_text(encoding="utf-8")
            (directory / name).write_text(expand_placeholders(source, name, config.bake_file), "utf-8")

        yield " ".join((directory / name).as_posix() for name in config.workloads)


def expand_placeholders(text: str, name: str, bake_file: Path) -> str:
    parts: list[str] = []
    index = 0
    for match in PLACEHOLDER.finditer(text):
        parts.append(text[index : match.start()])
        parts.append(_placeholder_value(match.group(1), name, bake_file))
        index = match.end()
    parts.append(text[index:])
    return "".join(parts)


def _placeholder_value(variable: str, name: str, bake_file: Path) -> str:
    value = os.environ.get(variable)
    if not value:
        raise SystemExit(f"{name}: ${{{variable}}} is not set. Is it a service in {bake_file}?")
    return value


def normalise_state_paths(config: ScoreConfig, path: Path) -> None:
    # score-k8s records the absolute path each workload was read from. Ours are rendered into a
    # per-run temporary directory, so the recorded path — and therefore the committed state file —
    # would differ on every run and on every machine. The field is provenance only; the workloads
    # are passed explicitly on each generate, so reducing it to the bare filename is safe and makes
    # the state reproducible.
    text = path.read_text(encoding="utf-8")
    for name in config.workloads:
        text = re.sub(rf"(?m)^(\s*file:\s*).*/{re.escape(name)}$", rf"\g<1>{name}", text)
    write_generated_file(path, text)


def sort_documents(path: Path) -> None:
    # score-k8s emits workloads in Go map order, which is randomised, so two runs otherwise
    # produce the same documents in a different sequence. Documents are parsed only to derive the
    # sort key and re-emitted as their original text, so Score's formatting is preserved.
    documents = [block for block in re.split(r"(?m)^---$\n", path.read_text(encoding="utf-8")) if block.strip()]
    ordered = [block for _, block in sorted(enumerate(documents), key=_document_sort_key)]
    write_generated_file(path, "".join(f"---\n{block}" for block in ordered))


def _document_sort_key(item: tuple[int, str]) -> tuple[str, str, str, int]:
    index, block = item
    document = ManifestDocument.model_validate(yaml.safe_load(block) or {})
    metadata = document.metadata or ManifestMetadata()
    return (document.kind, metadata.namespace, metadata.name, index)


def write_generated_file(path: Path, text: str) -> None:
    # newline="" suppresses the platform translation Python applies by default. Without it a
    # Windows run rewrites every line ending as CRLF while git stores LF, so the regenerated file
    # differs from the committed one on content that never changed and the staleness gate can
    # never pass.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    # score-k8s writes its state file executable. Git records the mode, so on Linux that alone
    # shows up as a diff and fails the staleness gate — invisibly on Windows, where core.fileMode
    # is false and the bit is never tracked. These are data files; normalise them to 0644.
    path.chmod(0o644)
