from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, NamedTuple

import typer
import yaml
from bashrun.bash import bash, bash_output

from .config import LOCK_PATH, compose_bundles, load_config
from .crane import install_crane
from .documents import ComposeDocument, compute_service_shas, parse_image_lock
from .image_refs import resolve_remote_digest

_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(:[?\-+][^}]*)?\}")
STUB_VALUE = "publish-stub"


class PublishLeg(NamedTuple):
    artifact: str
    files: list[str]


class PublishedLeg(NamedTuple):
    artifact: str
    sha_reference: str
    branch_reference: str
    digest: str


app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def publish_compose(
    bundle: Annotated[
        str | None,
        typer.Option(
            "--bundle",
            help="Publish this bundle leg (one leg per bundle x gpu variant); a bare invocation publishes every leg.",
        ),
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
    bundles = compose_bundles(devkit) if devkit is not None else []
    if not bundles:
        if plan:
            print(json.dumps([]))
            return
        raise RuntimeError(
            "publishing happens iff local service entries are declared in docker-devkit.yaml; "
            "one artifact per workload-bearing entry or option, named under root repository"
        )
    config = devkit
    assert config is not None

    if plan:
        legs = [{"bundle": entry.name} for entry in bundles]
        print(json.dumps(legs))
        return

    names = [entry.name for entry in bundles]
    if bundle is not None and bundle not in names:
        raise typer.BadParameter(f"unknown --bundle {bundle!r}; declared bundles: {names}")
    selected = [
        PublishLeg(entry.name, [entry.output.as_posix()]) for entry in bundles if bundle is None or entry.name == bundle
    ]

    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        raise SystemExit("REGISTRY_USERNAME and REGISTRY_TOKEN must be set for registry login")
    if config.repository is None:
        raise RuntimeError("bundles are declared; root repository names the artifact registry (ghcr.io/org/repo)")
    registry_host = config.repository.partition("/")[0]
    print(f"Login to {registry_host}")
    bash(f"docker login {shlex.quote(registry_host)} -u {shlex.quote(username)} --password-stdin", stdin_text=token)
    install_crane()

    sha = bash_output("git rev-parse HEAD").strip()
    branch_tag = (branch or bash_output("git rev-parse --abbrev-ref HEAD").strip()).replace("/", "-")

    substitutions = {
        **compute_service_shas(root, config),
        **parse_image_lock(root / LOCK_PATH).root,
    }

    published: list[PublishedLeg] = []
    for leg in selected:
        with TemporaryDirectory(prefix="compose-bundle-") as baked_directory_string:
            baked_directory = Path(baked_directory_string)
            baked_paths = bake_sources(baked_directory, leg.files, substitutions)

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
            # One publish per leg: compose stamps wall-clock created per invocation, so a
            # second publish of identical files yields a different digest — the branch tag
            # aliases the published manifest instead of re-publishing it.
            sha_reference = f"{config.repository}/{leg.artifact}:{sha}"
            print(f"Publish {sha_reference}")
            bash(f"docker compose {compose_files} publish {shlex.quote(sha_reference)} --yes")
            branch_reference = f"{config.repository}/{leg.artifact}:{branch_tag}"
            print(f"Tag {branch_reference}")
            bash(f"crane tag {shlex.quote(sha_reference)} {shlex.quote(branch_tag)}")
            digest = bash_output(f"crane digest {shlex.quote(sha_reference)}").strip()
            print(f"Digest {sha_reference} {digest}")
            print(f"Digest {branch_reference} {digest}")
            published.append(PublishedLeg(leg.artifact, sha_reference, branch_reference, digest))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path is not None and published:
        lines = [
            "## Published compose bundles",
            "",
            "| artifact | sha tag | branch tag | digest |",
            "| --- | --- | --- | --- |",
        ]
        for entry in published:
            lines.append(
                f"| {entry.artifact} | `{entry.sha_reference}` | `{entry.branch_reference}` | `{entry.digest}` |"
            )
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")


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
                    # Authored content rides through verbatim: single-$ holes are the consumer's
                    # include-time interpolation surface ($ doubling is the file-sourced path only).
                    inlined[config_name] = document["configs"][config_name]
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
