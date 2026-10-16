from pathlib import Path
from typing import Any

import pytest
import yaml

from docker_devkit.includes import contains_include, resolve_includes

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

    with pytest.raises(RuntimeError, match="not valid YAML"):
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


def test_contains_include_finds_nested_markers(tmp_path: Path) -> None:
    assert contains_include({"datasources": {"datasources.yaml": {"include-yaml": "d.yaml"}}})
    assert contains_include([{"include": "a.txt"}])
    assert not contains_include({"include": "a.txt", "other": 1})
    assert not contains_include({"plain": "value"})
