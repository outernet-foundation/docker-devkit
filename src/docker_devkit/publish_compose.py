from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any

import typer
import yaml
from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict

from .config import load_config
from .documents import compute_service_shas, parse_bake
from .image_refs import resolve_remote_digest
from .lifecycle import GPU_PLACEHOLDER, LOCK_PATH, parse_env_file, require_manifest

_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(:[?\-+][^}]*)?\}")
STUB_VALUE = "publish-stub"


class ComposeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str | None = None


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    configs: dict[str, ComposeConfig] | None = None


app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def publish_compose(
    variant: Annotated[
        str | None,
        typer.Option("--variant", help="Publish this variant leg (must be declared in the publish-compose section)."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch tag for the second publish; defaults to the checked-out branch."),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Print the publish legs as a bare JSON array (empty when no section); no publish."),
    ] = False,
) -> None:
    root = Path.cwd()
    devkit = load_config(root)
    if devkit is None or devkit.publish_compose is None:
        if plan:
            print(json.dumps([]))
            return
        raise RuntimeError("a publish-compose section (repository, artifact, files) is required in docker-devkit.yaml")
    config = devkit.publish_compose

    if plan:
        legs: list[dict[str, str]] = [{"variant": declared} for declared in config.variants]
        print(json.dumps(legs or [{}]))
        return

    if config.variants and variant is None:
        raise typer.BadParameter(f"--variant is required; declared variants: {config.variants}")
    if variant is not None and variant not in config.variants:
        raise typer.BadParameter(f"unknown --variant {variant!r}; declared variants: {config.variants}")
    for entry in config.files:
        if GPU_PLACEHOLDER in entry and variant is None:
            raise RuntimeError(f"bundle file {entry!r} carries {GPU_PLACEHOLDER} but no --variant was given")

    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        raise SystemExit("REGISTRY_USERNAME and REGISTRY_TOKEN must be set for registry login")
    registry_host = config.repository.partition("/")[0]
    print(f"Login to {registry_host}")
    bash(f"docker login {shlex.quote(registry_host)} -u {shlex.quote(username)} --password-stdin", stdin_text=token)

    sha = bash_output("git rev-parse HEAD").strip()
    branch_tag = (branch or bash_output("git rev-parse --abbrev-ref HEAD").strip()).replace("/", "-")

    substitutions = {
        **compute_service_shas(root, parse_bake(require_manifest(root))),
        **parse_env_file(root / LOCK_PATH),
    }
    source_names = [entry.format(gpu=variant or "") for entry in config.files]

    with TemporaryDirectory(prefix="compose-bundle-") as baked_directory_string:
        baked_directory = Path(baked_directory_string)
        baked_paths = bake_sources(baked_directory, source_names, substitutions)

        # Publish validates ${VAR} holes against the environment but embeds the file
        # uninterpolated (verified against a live registry — no env value enters the
        # artifact), so stubs exist only to satisfy that validation; they must be
        # non-empty because compose's ${VAR:?err} rejects unset OR empty.
        unresolved = {
            match.group(1)
            for baked_path in baked_paths
            for match in _VAR_PATTERN.finditer(baked_path.read_text(encoding="utf-8"))
        } - substitutions.keys()
        for name in unresolved:
            os.environ.setdefault(name, STUB_VALUE)

        finalize_baked(baked_paths, config.repository)

        compose_files = " ".join(f"-f {shlex.quote(str(baked_path))}" for baked_path in baked_paths)
        artifact = config.artifact if variant is None else f"{config.artifact}-{variant}"
        for tag in (sha, branch_tag):
            reference = f"{config.repository}/{artifact}:{tag}"
            print(f"Publish {reference}")
            bash(f"docker compose {compose_files} publish {shlex.quote(reference)} --yes")


def bake_sources(baked_directory: Path, source_names: list[str], substitutions: dict[str, str]) -> list[Path]:
    print("Bake compose YAML")
    baked_paths: list[Path] = []
    for source_name in source_names:
        source_path = Path(source_name)
        baked_text = _VAR_PATTERN.sub(
            lambda match: substitutions.get(match.group(1), match.group(0)),
            source_path.read_text(encoding="utf-8"),
        )
        baked_path = baked_directory / source_path
        baked_path.parent.mkdir(parents=True, exist_ok=True)
        baked_path.write_text(baked_text, encoding="utf-8")
        baked_paths.append(baked_path)
    return baked_paths


def finalize_baked(baked_paths: list[Path], repository: str) -> None:
    print("Inline configs content")
    # The published artifact is YAML-only: file-sourced configs dangle in the include consumer's
    # materialization cache, so they ride as inline content.
    own_image_pattern = re.compile(rf"{re.escape(repository)}/[a-z0-9][a-z0-9-]*:[^\s\"'@$]+")
    digests: dict[str, str] = {}
    for baked_path in baked_paths:
        document = yaml.safe_load(baked_path.read_text(encoding="utf-8"))
        parsed = ComposeDocument.model_validate(document)
        if parsed.configs is not None:
            inlined: dict[str, dict[str, Any]] = {}
            for config_name, compose_config in parsed.configs.items():
                if compose_config.file is None:
                    continue
                entry = compose_config.model_dump(exclude_defaults=True)
                entry.pop("file", None)
                # Literal $ doubles so consumer interpolation leaves env expansion to the container
                entry["content"] = Path(compose_config.file).read_text(encoding="utf-8").replace("$", "$$")
                inlined[config_name] = entry
            document["configs"] = inlined
            baked_path.write_text(
                yaml.dump(document, default_flow_style=False, sort_keys=False, width=1_000_000),
                encoding="utf-8",
            )

        # Refs under the bundle repository are this repo's own built images; publish them digest-pinned.
        text = baked_path.read_text(encoding="utf-8")
        parts: list[str] = []
        cursor = 0
        for match in own_image_pattern.finditer(text):
            parts.append(text[cursor : match.start()])
            reference = match.group(0)
            if reference not in digests:
                digests[reference] = resolve_remote_digest(reference)
            parts.append(f"{reference.rsplit(':', 1)[0]}@{digests[reference]}")
            cursor = match.end()
        parts.append(text[cursor:])
        baked_path.write_text("".join(parts), encoding="utf-8")
