from __future__ import annotations

from pathlib import Path
from typing import Any, TypeIs

import yaml

# Include markers are single-key mappings so authored sources stay plain-YAML-parseable:
# "include" inserts the file's bytes verbatim as a string scalar, "include-yaml" parses the
# file and inserts the structure (charts like grafana's demand mapping-shaped values).
# Neither mode transforms content — `${VAR}` holes pass through untouched, and templating
# belongs to score's Go templates, never here. Included content is never scanned for further
# markers except inside include-yaml trees, where recursion resolves them with cycle
# detection; text includes are bytes and stay bytes.
TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
MARKERS = (TEXT_MARKER, YAML_MARKER)


def contains_include(node: Any) -> bool:
    if _marker_entry(node) is not None:
        return True

    if _is_mapping(node):
        return any(contains_include(value) for value in node.values())

    if _is_list(node):
        return any(contains_include(item) for item in node)

    return False


def resolve_includes(node: Any, root: Path, source: str, ancestors: tuple[Path, ...] = ()) -> Any:
    entry = _marker_entry(node)
    if entry is not None:
        key, value = entry
        if not isinstance(value, str):
            raise RuntimeError(f"{source}: {key} takes a repo-root-relative path string, not {type(value).__name__}")
        path = _resolve_path(value, root, source)
        if path in ancestors:
            raise RuntimeError(f"{source}: include {value!r} participates in a cycle")
        if not path.is_file():
            raise RuntimeError(f"{source}: include {value!r} does not exist at the repo root")
        text = path.read_text(encoding="utf-8")
        if key == TEXT_MARKER:
            return text
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise RuntimeError(f"{source}: include {value!r} is not valid YAML: {error}") from error
        if parsed is None:
            raise RuntimeError(f"{source}: include {value!r} is empty")
        return resolve_includes(parsed, root, source, (*ancestors, path))

    if _is_mapping(node):
        return {key: resolve_includes(value, root, source, ancestors) for key, value in node.items()}

    if _is_list(node):
        return [resolve_includes(item, root, source, ancestors) for item in node]

    return node


def _is_mapping(node: Any) -> TypeIs[dict[Any, Any]]:
    return isinstance(node, dict)


def _is_list(node: Any) -> TypeIs[list[Any]]:
    return isinstance(node, list)


def _marker_entry(node: Any) -> tuple[str, Any] | None:
    if _is_mapping(node) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in MARKERS:
            return (key, value)

    return None


def _resolve_path(reference: str, root: Path, source: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"{source}: include path {reference!r} must anchor at the repo root")

    return root / relative
