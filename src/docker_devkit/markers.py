from __future__ import annotations

from pathlib import Path
from typing import Any, TypeIs

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

# Authoring markers are single-key mappings so authored sources stay plain-YAML-parseable.
# Include markers insert a repo-root-relative file's bytes or parsed structure; lock markers
# substitute image-lock entries. Include resolution runs before lock resolution, and text
# content is never scanned for markers.
TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
LOCK_MARKER = "lock"
LOCK_IMAGE_MARKER = "lock-image"
INCLUDE_MARKERS = (TEXT_MARKER, YAML_MARKER)
LOCK_MARKERS = (LOCK_MARKER, LOCK_IMAGE_MARKER)
MARKERS = INCLUDE_MARKERS + LOCK_MARKERS
DIGEST_FIELDS = ("digest", "sha")


class LockImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    digest_field: str = "digest"

    @field_validator("digest_field")
    @classmethod
    def _require_known_digest_field(cls, value: str) -> str:
        if value not in DIGEST_FIELDS:
            raise ValueError(f"digest_field must be one of {DIGEST_FIELDS}")
        return value


def contains_marker(node: Any) -> bool:
    if _marker_entry(node, MARKERS) is not None:
        return True

    if is_mapping(node):
        return any(contains_marker(value) for value in node.values())

    if is_list(node):
        return any(contains_marker(item) for item in node)

    return False


def resolve_includes(node: Any, root: Path, source: str, ancestors: tuple[Path, ...] = ()) -> Any:
    entry = _marker_entry(node, INCLUDE_MARKERS)
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
        parsed = yaml.safe_load(text)
        if parsed is None:
            raise RuntimeError(f"{source}: include {value!r} is empty")
        return resolve_includes(parsed, root, source, (*ancestors, path))

    if is_mapping(node):
        return {key: resolve_includes(value, root, source, ancestors) for key, value in node.items()}

    if is_list(node):
        return [resolve_includes(item, root, source, ancestors) for item in node]

    return node


def resolve_lock_refs(node: Any, images: dict[str, str], source: str) -> Any:
    entry = _marker_entry(node, LOCK_MARKERS)
    if entry is not None:
        marker, value = entry
        if marker == LOCK_MARKER:
            if not isinstance(value, str):
                raise RuntimeError(f"{source}: lock takes a lock-entry name string, not {type(value).__name__}")
            if value not in images:
                raise RuntimeError(f"{source}: lock entry {value!r} does not exist in the image lock")
            return images[value]

        image_spec = _image_spec(value, source)
        if image_spec.key not in images:
            raise RuntimeError(f"{source}: lock entry {image_spec.key!r} does not exist in the image lock")
        reference = images[image_spec.key]
        # Every lock entry is digest-pinned at load (ImageLock), so the split always yields a digest.
        path_part, _, digest = reference.rpartition("@")

        fields: dict[str, str] = {}
        tagged = path_part.rpartition(":")
        if tagged[1] and "/" not in tagged[2]:
            fields["registry"], fields["repository"] = _split_path(tagged[0], reference, source)
            fields["tag"] = tagged[2]
        else:
            fields["registry"], fields["repository"] = _split_path(path_part, reference, source)

        if image_spec.digest_field == "sha":
            # Spell the digest bare; the grafana chart template re-adds the sha256: prefix.
            fields["sha"] = digest.removeprefix("sha256:")
        else:
            fields["digest"] = digest
        return fields

    if is_mapping(node):
        return {key: resolve_lock_refs(value, images, source) for key, value in node.items()}

    if is_list(node):
        return [resolve_lock_refs(item, images, source) for item in node]

    return node


def _image_spec(value: Any, source: str) -> LockImageSpec:
    if isinstance(value, str):
        return LockImageSpec(key=value)

    if is_mapping(value):
        return LockImageSpec.model_validate(value)

    raise RuntimeError(
        f"{source}: lock-image takes a lock-entry name or a key/digest_field mapping, not {type(value).__name__}"
    )


def _split_path(path: str, reference: str, source: str) -> tuple[str, str]:
    registry, separator, repository = path.rpartition("/")
    if not separator or not registry or not repository:
        raise RuntimeError(f"{source}: lock reference {reference!r} has no registry/repository split")
    return registry, repository


def is_mapping(node: Any) -> TypeIs[dict[Any, Any]]:
    return isinstance(node, dict)


def is_list(node: Any) -> TypeIs[list[Any]]:
    return isinstance(node, list)


def _marker_entry(node: Any, markers: tuple[str, ...]) -> tuple[str, Any] | None:
    if is_mapping(node) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in markers:
            return (key, value)

    return None
