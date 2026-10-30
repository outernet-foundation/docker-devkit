from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit.markers import contains_marker, resolve_includes, resolve_lock_refs

RIVER = """// relay config
loki.write "default" {
  url = sys.env("LOKI_WRITE_ENDPOINT")
}
"""

DATASOURCES = """apiVersion: 1
datasources:
  - name: Loki
    url: http://loki:3100
"""

DIGEST = "sha256:" + "a" * 64
ALLOY = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/alloy:v1.9.0@" + DIGEST
LOKI = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@" + DIGEST


def write_repo(tree: dict[str, str], root: Path) -> None:
    for name, content in tree.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_text_include_inserts_file_bytes_verbatim(tmp_path: Path) -> None:
    write_repo({"workloads/alloy/config.alloy": RIVER}, tmp_path)

    resolved = resolve_includes({"config.alloy": {"include": "workloads/alloy/config.alloy"}}, tmp_path, "x.yaml")

    assert resolved == {"config.alloy": RIVER}


def test_text_include_leaves_dollar_holes_untouched(tmp_path: Path) -> None:
    write_repo({"config.yaml": "key: ${S3_ACCESS_KEY}\n"}, tmp_path)

    resolved = resolve_includes({"include": "config.yaml"}, tmp_path, "x.yaml")

    assert resolved == "key: ${S3_ACCESS_KEY}\n"


def test_yaml_include_inserts_the_parsed_structure(tmp_path: Path) -> None:
    write_repo({"workloads/grafana/provisioning/datasources/datasources.yaml": DATASOURCES}, tmp_path)

    resolved = resolve_includes(
        {
            "datasources": {
                "datasources.yaml": {"include-yaml": "workloads/grafana/provisioning/datasources/datasources.yaml"}
            }
        },
        tmp_path,
        "values.yaml",
    )

    assert resolved["datasources"]["datasources.yaml"] == yaml.safe_load(DATASOURCES)


def test_markers_resolve_inside_lists_and_nested_mappings(tmp_path: Path) -> None:
    write_repo({"a.txt": "alpha\n", "b.txt": "beta\n"}, tmp_path)

    resolved = resolve_includes({"items": [{"include": "a.txt"}, {"deep": {"include": "b.txt"}}]}, tmp_path, "x.yaml")

    assert resolved == {"items": ["alpha\n", {"deep": "beta\n"}]}


def test_marker_with_extra_keys_is_plain_data(tmp_path: Path) -> None:
    resolved = resolve_includes({"include": "a.txt", "other": 1}, tmp_path, "x.yaml")

    assert resolved == {"include": "a.txt", "other": 1}


def test_missing_include_is_a_loud_error_naming_the_source(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"values\.yaml.*'workloads/alloy/config.alloy' does not exist"):
        resolve_includes({"include": "workloads/alloy/config.alloy"}, tmp_path, "values.yaml")


def test_absolute_include_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="anchor at the repo root"):
        resolve_includes({"include": "/etc/passwd"}, tmp_path, "x.yaml")


def test_parent_escaping_include_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="anchor at the repo root"):
        resolve_includes({"include": "../outside.txt"}, tmp_path, "x.yaml")


def test_non_string_marker_value_is_a_loud_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="path string"):
        resolve_includes({"include": ["a", "b"]}, tmp_path, "x.yaml")


def test_invalid_yaml_include_is_a_loud_error(tmp_path: Path) -> None:
    write_repo({"broken.yaml": "key: [unclosed\n"}, tmp_path)

    with pytest.raises(yaml.YAMLError):
        resolve_includes({"include-yaml": "broken.yaml"}, tmp_path, "x.yaml")


def test_empty_yaml_include_is_a_loud_error(tmp_path: Path) -> None:
    write_repo({"empty.yaml": ""}, tmp_path)

    with pytest.raises(RuntimeError, match="is empty"):
        resolve_includes({"include-yaml": "empty.yaml"}, tmp_path, "x.yaml")


def test_yaml_include_cycles_are_detected(tmp_path: Path) -> None:
    write_repo({"a.yaml": "a: {include-yaml: b.yaml}\n", "b.yaml": "b: {include-yaml: a.yaml}\n"}, tmp_path)

    with pytest.raises(RuntimeError, match="cycle"):
        resolve_includes({"include-yaml": "a.yaml"}, tmp_path, "x.yaml")


def test_yaml_includes_resolve_recursively(tmp_path: Path) -> None:
    write_repo({"outer.yaml": "inner: {include-yaml: inner.yaml}\n", "inner.yaml": "value: 1\n"}, tmp_path)

    resolved = resolve_includes({"include-yaml": "outer.yaml"}, tmp_path, "x.yaml")

    assert resolved == {"inner": {"value": 1}}


def test_text_include_content_is_never_scanned_for_markers(tmp_path: Path) -> None:
    write_repo({"a.txt": "include: b.txt\n"}, tmp_path)

    resolved = resolve_includes({"include": "a.txt"}, tmp_path, "x.yaml")

    assert resolved == "include: b.txt\n"


def test_non_marker_trees_pass_through_unchanged(tmp_path: Path) -> None:
    tree: Any = {"plain": "value", "list": [1, 2, {"nested": True}]}

    assert resolve_includes(tree, tmp_path, "x.yaml") == tree


def test_lock_marker_substitutes_the_full_reference() -> None:
    resolved = resolve_lock_refs({"image": {"lock": "ALLOY_IMAGE"}}, {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")

    assert resolved == {"image": ALLOY}


def test_lock_marker_resolves_inside_lists_and_nested_mappings() -> None:
    source: Any = {"manifests": [{"containers": [{"image": {"lock": "ALLOY_IMAGE"}}]}]}

    resolved = resolve_lock_refs(source, {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")

    assert resolved["manifests"][0]["containers"][0]["image"] == ALLOY


def test_unknown_lock_entry_is_a_loud_error_naming_the_source() -> None:
    with pytest.raises(RuntimeError, match=r"k8s\.yaml.*'KEYCLOAK_IMAGE' does not exist"):
        resolve_lock_refs({"image": {"lock": "KEYCLOAK_IMAGE"}}, {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")


def test_non_string_lock_marker_value_is_a_loud_error() -> None:
    with pytest.raises(RuntimeError, match="name string"):
        resolve_lock_refs({"image": {"lock": ["ALLOY"]}}, {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")


def test_marker_with_extra_keys_is_plain_data_under_lock_resolution() -> None:
    node: Any = {"lock": "ALLOY_IMAGE", "tag": "v1"}

    resolved = resolve_lock_refs(node, {"ALLOY_IMAGE": ALLOY, "tag": "v1"}, "k8s.yaml")

    assert resolved == node


def test_lock_image_marker_splits_chart_fields() -> None:
    resolved = resolve_lock_refs({"loki": {"image": {"lock-image": "LOKI_IMAGE"}}}, {"LOKI_IMAGE": LOKI}, "loki.yaml")

    assert resolved["loki"]["image"] == {
        "registry": "ghcr.io/outernet-foundation/mirror/docker.io/grafana",
        "repository": "loki",
        "tag": "3.5.0",
        "digest": DIGEST,
    }


def test_lock_image_marker_splits_field_order_matches_chart_convention() -> None:
    resolved = resolve_lock_refs({"image": {"lock-image": "LOKI_IMAGE"}}, {"LOKI_IMAGE": LOKI}, "loki.yaml")

    assert list(resolved["image"]) == ["registry", "repository", "tag", "digest"]


def test_lock_image_marker_supports_the_grafana_sha_spelling() -> None:
    resolved = resolve_lock_refs(
        {"image": {"lock-image": {"key": "LOKI_IMAGE", "digest_field": "sha"}}}, {"LOKI_IMAGE": LOKI}, "grafana.yaml"
    )

    assert resolved["image"]["sha"] == DIGEST.removeprefix("sha256:")
    assert "digest" not in resolved["image"]


def test_lock_image_marker_rejects_unknown_digest_fields() -> None:
    with pytest.raises(ValidationError, match=r"(?s).*digest_field"):
        resolve_lock_refs(
            {"image": {"lock-image": {"key": "LOKI_IMAGE", "digest_field": "checksum"}}},
            {"LOKI_IMAGE": LOKI},
            "grafana.yaml",
        )


def test_lock_image_marker_rejects_extra_spec_keys() -> None:
    with pytest.raises(ValidationError):
        resolve_lock_refs(
            {"image": {"lock-image": {"key": "LOKI_IMAGE", "pull": "always"}}}, {"LOKI_IMAGE": LOKI}, "grafana.yaml"
        )


def test_lock_image_marker_rejects_non_name_non_mapping_values() -> None:
    with pytest.raises(RuntimeError, match="name or a key/digest_field mapping"):
        resolve_lock_refs({"image": {"lock-image": ["LOKI_IMAGE"]}}, {"LOKI_IMAGE": LOKI}, "grafana.yaml")


def test_lock_image_marker_splits_untagged_references_without_a_tag_field() -> None:
    untagged = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki@" + DIGEST

    resolved = resolve_lock_refs({"image": {"lock-image": "LOKI_IMAGE"}}, {"LOKI_IMAGE": untagged}, "loki.yaml")

    assert resolved["image"] == {
        "registry": "ghcr.io/outernet-foundation/mirror/docker.io/grafana",
        "repository": "loki",
        "digest": DIGEST,
    }


def test_lock_marker_leaves_plain_strings_untouched() -> None:
    node: Any = {"image": "ghcr.io/example/app:v1"}

    assert resolve_lock_refs(node, {}, "k8s.yaml") == node


def test_contains_marker_finds_nested_markers_of_every_kind() -> None:
    assert contains_marker({"datasources": {"datasources.yaml": {"include-yaml": "d.yaml"}}})
    assert contains_marker([{"include": "a.txt"}])
    assert contains_marker({"manifests": [{"image": {"lock": "ALLOY_IMAGE"}}]})
    assert contains_marker({"loki": {"image": {"lock-image": "LOKI_IMAGE"}}})
    assert not contains_marker({"include": "a.txt", "other": 1})
    assert not contains_marker({"lock": "ALLOY_IMAGE", "other": 1})
    assert not contains_marker({"plain": "value"})
    assert not contains_marker({"image": "ghcr.io/example/app:v1"})
