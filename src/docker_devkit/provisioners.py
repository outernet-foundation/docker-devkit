from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .markers import resolve_includes, resolve_lock_refs

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

    @field_validator(*STRING_FORM_FIELDS, mode="before")
    @classmethod
    def _reject_string_form(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str):
            return value

        shape = "list" if info.field_name == "manifests" else "mapping"
        context: dict[str, Any] = info.context or {}
        raise ValueError(
            f"{context.get('name', 'the provisioner file')}: "
            f"{info.field_name} is already in score's compiled string form; author it as a {shape}"
        )


class AuthoredProvisioners(RootModel[list[AuthoredProvisioner]]):
    @model_validator(mode="after")
    def _require_entries(self, info: ValidationInfo) -> Self:
        if not self.root:
            context: dict[str, Any] = info.context or {}
            raise ValueError(
                f"{context.get('name', 'the provisioner file')}: "
                "an authored provisioner file is a non-empty YAML list of provisioner entries"
            )
        return self


class CompiledProvisioners(NamedTuple):
    # declares_services is the phantom-service condition: score-compose synthesizes its
    # wait-for-resources gate exactly when provisioner-added services exist, and the
    # devkit's built-in gate-pin template registers on it.
    paths: list[Path]
    declares_services: bool


@contextmanager
def compiled_provisioners(
    source_dir: Path, names: list[str], root: Path, images: dict[str, str]
) -> Generator[CompiledProvisioners]:
    # Compile into a per-run tempdir under source_dir: the string form never touches the
    # committed tree, and --provisioners paths stay repo-relative.
    if not names:
        yield CompiledProvisioners(paths=[], declares_services=False)
        return

    with TemporaryDirectory(prefix="compiled-provisioners-", dir=source_dir) as temporary_directory:
        directory = Path(temporary_directory)
        paths: list[Path] = []
        declares_services = False
        for name in names:
            document = resolve_lock_refs(
                resolve_includes(yaml.safe_load((source_dir / name).read_text(encoding="utf-8")), root, name),
                images,
                name,
            )
            AuthoredProvisioners.model_validate(document, context={"name": name})
            declares_services = declares_services or any(entry.get("services") for entry in document)

            # Compile from the raw authored dicts so authored key order controls dump order.
            compiled_entries: list[dict[str, Any]] = []
            for entry in document:
                compiled: dict[str, Any] = {}
                for key, value in entry.items():
                    if key in STRING_FORM_FIELDS:
                        if value is not None:
                            compiled[key] = dump_compose(value)
                    else:
                        compiled[key] = value
                compiled_entries.append(compiled)
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dump_compose(compiled_entries), encoding="utf-8", newline="")
            paths.append(target)
        yield CompiledProvisioners(paths=paths, declares_services=declares_services)


def dump_compose(value: Any) -> str:
    return yaml.dump(
        value,
        Dumper=ComposeDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
        allow_unicode=True,
    )
