from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError, field_validator

from .includes import resolve_includes
from .locks import resolve_lock_refs

# Compile these fields from authored mappings into the YAML strings score's Go templates
# consume; authored manifests stay a real list.
STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")


class ComposeDumper(yaml.SafeDumper):
    pass


def _represent_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Force literal block style for multiline strings, building the node directly to satisfy strict typing.
    return yaml.ScalarNode("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ComposeDumper.add_representer(str, _represent_multiline)


class AuthoredProvisioner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    type: str
    description: str = ""
    state: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    volumes: dict[str, Any] | None = None
    services: dict[str, Any] | None = None
    manifests: list[Any] | None = None

    @field_validator("state", "outputs", "volumes", "services", mode="before")
    @classmethod
    def _reject_string_mappings(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        raise ValueError("field is already in score's compiled string form; author it as a mapping")

    @field_validator("manifests", mode="before")
    @classmethod
    def _reject_string_manifests(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        raise ValueError("field is already in score's compiled string form; author it as a list")


class AuthoredProvisioners(RootModel[list[AuthoredProvisioner]]):
    pass


@contextmanager
def compiled_provisioners(
    source_dir: Path, names: list[str], root: Path, images: dict[str, str]
) -> Generator[list[Path]]:
    # Compile into a per-run tempdir under source_dir: the string form never touches the
    # committed tree, and --provisioners paths stay repo-relative.
    if not names:
        yield []
        return

    with TemporaryDirectory(prefix="compiled-provisioners-", dir=source_dir) as temporary_directory:
        directory = Path(temporary_directory)
        paths: list[Path] = []
        for name in names:
            compiled = compile_provisioner_document((source_dir / name).read_text(encoding="utf-8"), name, root, images)
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(compiled, encoding="utf-8", newline="")
            paths.append(target)
        yield paths


def compile_provisioner_document(text: str, source_name: str, root: Path, images: dict[str, str]) -> str:
    document = resolve_lock_refs(resolve_includes(yaml.safe_load(text), root, source_name), images, source_name)
    try:
        authored = AuthoredProvisioners.model_validate(document)
    except ValidationError as error:
        raise RuntimeError(f"{source_name}: authored provisioner rejected: {error}") from error

    if not authored.root:
        raise RuntimeError(
            f"{source_name}: an authored provisioner file is a non-empty YAML list of provisioner entries"
        )

    # Compile from the raw authored dicts so authored key order controls dump order.
    return dump_compose([_compile_entry(entry) for entry in document])


def _compile_entry(entry: dict[str, Any]) -> dict[str, Any]:
    compiled: dict[str, Any] = {}
    for key, value in entry.items():
        if key in STRING_FORM_FIELDS:
            if value is not None:
                compiled[key] = dump_compose(value)
        else:
            compiled[key] = value

    return compiled


def dump_compose(value: Any) -> str:
    return yaml.dump(
        value,
        Dumper=ComposeDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
        allow_unicode=True,
    )
