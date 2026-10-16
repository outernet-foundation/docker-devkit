from contextlib import chdir
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from docker_devkit.provisioners import (
    AuthoredProvisioner,
    compiled_provisioners,
    compile_provisioner_document,
)

COMPOSE_ENTRY = """
- uri: template://example/database
  type: example-database
  description: Example database.
  state:
    serviceName: db
    port: 5432
  outputs:
    host: "{{ .State.serviceName }}"
    port: 5432
    password: "${DB_PASSWORD:?err}"
  volumes:
    db-data:
      driver: local
  services:
    "{{ .State.serviceName }}":
      image: example/db:{{ env "DB_SHA" }}
      environment:
        RESTART: "no"
        MODE: strict
"""

K8S_ENTRY = """
- uri: template://example/database-k8s
  type: example-database
  description: Example database (Kubernetes).
  state:
    service: db
  manifests:
    - apiVersion: v1
      kind: Service
      metadata:
        name: "{{ .State.service }}"
      spec:
        ports:
        - port: 5432
    - apiVersion: v1
      kind: ConfigMap
      metadata:
        name: "{{ .State.service }}-config"
      data:
        config.text: |
          first line
          second line
"""


def parse_entries(text: str) -> Any:
    return yaml.safe_load(text)


def test_uri_and_type_are_required() -> None:
    with pytest.raises(ValidationError):
        AuthoredProvisioner.model_validate({"uri": "template://example/x"})

    with pytest.raises(ValidationError):
        AuthoredProvisioner.model_validate({"type": "example-x"})


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="init"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "init": "echo hello",
        })


def test_string_form_state_is_a_loud_error() -> None:
    with pytest.raises(ValidationError, match=r"string form.*author it as a mapping"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "state": "serviceName: db\n",
        })


def test_string_form_services_is_a_loud_error() -> None:
    with pytest.raises(ValidationError, match="string form"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "services": "db:\n  image: example/db\n",
        })


def test_string_form_manifests_names_the_list_shape() -> None:
    with pytest.raises(ValidationError, match="author it as a list"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "manifests": "- apiVersion: v1\n",
        })


def test_compile_stringifies_every_mapping_field() -> None:
    compiled = parse_entries(compile_provisioner_document(COMPOSE_ENTRY, "database.yaml"))

    assert len(compiled) == 1
    entry = compiled[0]
    for field in ("state", "outputs", "volumes", "services"):
        assert isinstance(entry[field], str), field


def test_compiled_fields_round_trip_to_the_authored_mappings() -> None:
    authored = parse_entries(COMPOSE_ENTRY)[0]
    compiled = parse_entries(compile_provisioner_document(COMPOSE_ENTRY, "database.yaml"))[0]

    for field in ("state", "outputs", "volumes", "services"):
        assert yaml.safe_load(compiled[field]) == authored[field]


def test_templates_and_env_holes_survive_verbatim() -> None:
    compiled = parse_entries(compile_provisioner_document(COMPOSE_ENTRY, "database.yaml"))[0]

    assert "{{ .State.serviceName }}" in compiled["services"]
    assert "password: ${DB_PASSWORD:?err}" in compiled["outputs"]
    assert 'example/db:{{ env "DB_SHA" }}' in compiled["services"]


def test_scalar_kinds_round_trip() -> None:
    compiled = parse_entries(compile_provisioner_document(COMPOSE_ENTRY, "database.yaml"))[0]

    state = yaml.safe_load(compiled["state"])
    assert state["port"] == 5432

    services = yaml.safe_load(compiled["services"])
    environment = services["{{ .State.serviceName }}"]["environment"]
    assert environment["RESTART"] == "no"
    assert environment["MODE"] == "strict"


def test_authored_key_order_controls_dump_order() -> None:
    authored = """
- uri: template://example/x
  type: example-x
  outputs:
    second: two
    first: one
  state:
    later: b
    earlier: a
"""
    compiled = compile_provisioner_document(authored, "x.yaml")
    entry = parse_entries(compiled)[0]

    assert list(entry) == ["uri", "type", "outputs", "state"]
    assert list(yaml.safe_load(entry["outputs"])) == ["second", "first"]
    assert list(yaml.safe_load(entry["state"])) == ["later", "earlier"]


def test_compile_manifests_as_a_string_list() -> None:
    authored = parse_entries(K8S_ENTRY)[0]
    compiled = parse_entries(compile_provisioner_document(K8S_ENTRY, "database-k8s.yaml"))[0]

    assert isinstance(compiled["manifests"], str)
    assert yaml.safe_load(compiled["manifests"]) == authored["manifests"]


def test_multiline_strings_keep_exact_content() -> None:
    authored = parse_entries(K8S_ENTRY)[0]
    compiled = parse_entries(compile_provisioner_document(K8S_ENTRY, "database-k8s.yaml"))[0]

    config_map = yaml.safe_load(compiled["manifests"])[1]
    assert config_map["data"]["config.text"] == authored["manifests"][1]["data"]["config.text"]


def test_multiline_values_dump_as_literal_blocks() -> None:
    compiled = compile_provisioner_document(K8S_ENTRY, "database-k8s.yaml")

    assert "config.text: |" in compiled


def test_absent_optional_fields_stay_absent() -> None:
    compiled = parse_entries(
        compile_provisioner_document(
            """
- uri: template://example/x
  type: example-x
  description: Nothing else.
""",
            "x.yaml",
        )
    )[0]

    assert set(compiled) == {"uri", "type", "description"}


def test_none_valued_fields_are_dropped() -> None:
    compiled = parse_entries(
        compile_provisioner_document(
            """
- uri: template://example/x
  type: example-x
  state:
""",
            "x.yaml",
        )
    )[0]

    assert set(compiled) == {"uri", "type"}


def test_non_list_document_is_rejected() -> None:
    with pytest.raises(RuntimeError, match=r"Input should be a valid list"):
        compile_provisioner_document("uri: template://example/x\ntype: example-x\n", "x.yaml")


def test_empty_file_is_rejected() -> None:
    with pytest.raises(RuntimeError, match=r"Input should be a valid list"):
        compile_provisioner_document("", "x.yaml")


def test_empty_document_list_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="non-empty YAML list"):
        compile_provisioner_document("[]\n", "x.yaml")


def test_validation_errors_carry_the_source_name() -> None:
    with pytest.raises(RuntimeError, match=r"database\.yaml"):
        compile_provisioner_document(
            """
- uri: template://example/database
  type: example-database
  services: |
    db:
      image: example/db
""",
            "database.yaml",
        )


def test_compiled_provisioners_write_tempdir_copies_under_the_source_dir(tmp_path: Path) -> None:
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "database.yaml").write_text(COMPOSE_ENTRY, encoding="utf-8")

    with compiled_provisioners(tmp_path, ["compose/database.yaml"]) as paths:
        assert len(paths) == 1
        compiled_path = paths[0]
        assert tmp_path in compiled_path.parents
        assert compiled_path.name == "database.yaml"
        entries = parse_entries(compiled_path.read_text(encoding="utf-8"))
        assert isinstance(entries[0]["state"], str)

    assert not compiled_path.exists()
    assert list(tmp_path.glob("compiled-provisioners-*")) == []


def test_compiled_provisioners_yield_nothing_without_names(tmp_path: Path) -> None:
    with compiled_provisioners(tmp_path, []) as paths:
        assert paths == []

    assert list(tmp_path.iterdir()) == []


def test_compiled_provisioners_reject_string_form_before_any_writes(tmp_path: Path) -> None:
    (tmp_path / "legacy.yaml").write_text(
        """
- uri: template://example/x
  type: example-x
  state: |
    key: value
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"legacy\.yaml"), compiled_provisioners(tmp_path, ["legacy.yaml"]):
        pass

    assert list(tmp_path.glob("compiled-provisioners-*")) == []


def test_compiled_output_is_independent_of_working_directory(tmp_path: Path) -> None:
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "database.yaml").write_text(COMPOSE_ENTRY, encoding="utf-8")

    expected = compile_provisioner_document(COMPOSE_ENTRY, "database.yaml")

    with chdir(Path("/")):
        with compiled_provisioners(tmp_path, ["compose/database.yaml"]) as paths:
            compiled = paths[0].read_text(encoding="utf-8")

    assert compiled == expected
