from __future__ import annotations

from pathlib import Path
from typing import Any, TypeIs

import yaml

# Resolve single-key include markers: "include" inserts the file's bytes verbatim as a string
# scalar, "include-yaml" inserts parsed structure. Never transform content; recurse only into
# include-yaml trees, with cycle detection.
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
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"{source}: include path {value!r} must anchor at the repo root")
        path = root / relative
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
