from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

import typer
import yaml
from bashrun.bash import bash
from pydantic import BaseModel, ConfigDict

from .config import ScoreConfig, load_config
from .context_sha import compute_service_shas
from .documents import parse_bake

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


class WorkloadState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instance_suffix: str = ""


class ScoreState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workloads: dict[str, WorkloadState] = {}


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
    config = load_config(root)
    if config is None or config.generate_score is None:
        raise RuntimeError("a generate-score section is required in docker-devkit.yaml")
    return config.generate_score


def generate(config: ScoreConfig, target: Target = "both", local: bool = False) -> None:
    _require_score_tools(config, target)

    root = Path.cwd()
    os.environ.update(compute_service_shas(root, parse_bake(config.bake_file)))
    os.environ[config.storage_class_var] = config.local_storage_class if local else config.cloud_storage_class

    if target in ("both", "compose"):
        _generate_compose(root, config)

    if target in ("both", "k8s"):
        _generate_k8s(root, config, config.k8s_local_output if local else config.k8s_output)


def _require_score_tools(config: ScoreConfig, target: Target) -> None:
    wanted: list[tuple[str, str]] = []
    if target in ("both", "compose"):
        wanted.append(("score-compose", config.score_compose_version))
    if target in ("both", "k8s"):
        wanted.append(("score-k8s", config.score_k8s_version))

    missing = [(tool, version) for tool, version in wanted if shutil.which(tool) is None]
    if not missing:
        return

    lines = [
        f"  {tool} {version}: https://github.com/score-spec/{tool}/releases/tag/{version}" for tool, version in missing
    ]
    raise RuntimeError(
        "generate-score needs these on PATH, at these exact versions (output differs between releases):\n"
        + "\n".join(lines)
    )


def ensure_score_tools(config: ScoreConfig, bin_dir: Path) -> None:
    for tool, version in (("score-k8s", config.score_k8s_version), ("score-compose", config.score_compose_version)):
        archive = f"{tool}_{version}_linux_amd64.tar.gz"
        bash(f"curl -fsSLO https://github.com/score-spec/{tool}/releases/download/{version}/{archive}")
        bash(f"tar -xzf {archive} -C {bin_dir} {tool}")
        Path(archive).unlink()


def _generate_compose(root: Path, config: ScoreConfig) -> None:
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

    rmtree(root / config.score_dir / ".score-compose", ignore_errors=True)


def _generate_k8s(root: Path, config: ScoreConfig, output: Path) -> None:
    state_dir = root / config.k8s_state
    output_path = root / output
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _init_state(
        "score-k8s init --no-sample",
        config.k8s_provisioners,
        [],
        source_dir=root / config.score_dir,
        working_dir=state_dir.parent,
    )

    with _rendered_workloads(root, config) as workloads:
        bash(f"score-k8s generate {workloads} --output {output_path.as_posix()}", cwd=state_dir.parent)

    sort_documents(output_path)
    normalise_state_paths(config, state_dir / "state.yaml")
    normalise_instance_uids(state_dir / "state.yaml", output_path)
    rmtree(state_dir, ignore_errors=True)


def _init_state(
    command: str,
    provisioners: list[str],
    patch_templates: list[str],
    *,
    source_dir: Path,
    working_dir: Path,
) -> None:
    def relative(name: str) -> str:
        return shlex.quote(Path(os.path.relpath(source_dir / name, working_dir)).as_posix())

    flags = "".join(f" --provisioners {relative(name)}" for name in provisioners)
    flags += "".join(f" --patch-templates {relative(name)}" for name in patch_templates)
    bash(f"{command}{flags}", cwd=working_dir)


@contextmanager
def _rendered_workloads(root: Path, config: ScoreConfig) -> Generator[str]:
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


def normalise_instance_uids(state_path: Path, manifest_path: Path) -> None:
    state = ScoreState.model_validate(yaml.safe_load(state_path.read_text(encoding="utf-8")) or {})

    state_text = state_path.read_text(encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")

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
    write_generated_file(manifest_path, manifest_text)


def normalise_state_paths(config: ScoreConfig, path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name in config.workloads:
        text = re.sub(rf"(?m)^(\s*file:\s*).*/{re.escape(name)}$", rf"\g<1>{name}", text)
    write_generated_file(path, text)


def sort_documents(path: Path) -> None:
    documents = [block for block in re.split(r"(?m)^---$\n", path.read_text(encoding="utf-8")) if block.strip()]
    ordered = [block for _, block in sorted(enumerate(documents), key=_document_sort_key)]
    write_generated_file(path, "".join(f"---\n{block}" for block in ordered))


def _document_sort_key(item: tuple[int, str]) -> tuple[str, str, str, int]:
    index, block = item
    document = ManifestDocument.model_validate(yaml.safe_load(block) or {})
    metadata = document.metadata or ManifestMetadata()
    return (document.kind, metadata.namespace, metadata.name, index)


def write_generated_file(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    path.chmod(0o644)
