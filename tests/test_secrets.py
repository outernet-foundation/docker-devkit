from pathlib import Path

import pytest
import yaml

from docker_devkit.secrets import (
    SecretKey,
    compose_consumed_keys,
    k8s_consumed_keys,
    load_declaration,
    mint_text,
    validate_consumed,
)

COMPOSE_ARTIFACT = """services:
  api:
    image: ghcr.io/example/api:tree-abc
    environment:
      DATABASE_OWNER_PASSWORD: ${DATABASE_OWNER_PASSWORD:?err}
      GRAFANA_PORT: ${GRAFANA_PORT:-3000}
  loki-config:
    content: |
      s3:
        access_key_id: $${S3_ACCESS_KEY}
        secret_key_id: $$$${S3_SECRET_KEY}
  weird:
    x: $$$${ALSO_ESCAPED}
"""

K8S_MANIFESTS = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: PUBLIC_URL
          valueFrom:
            secretKeyRef:
              name: placeframe-secrets
              key: PUBLIC_URL
        - name: OTHER
          valueFrom:
            secretKeyRef:
              name: some-other-secret
              key: PUBLIC_URL
      - name: sidecar
        envFrom:
        - secretRef:
            name: placeframe-secrets
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: plain
data:
  key: ${NOT_A_SECRET}
"""


def test_declaration_loads_string_shorthand_and_full_specs(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_text(
        "GRAFANA_PORT: Host port grafana is published on.\n"
        "POSTGRES_ADMIN_PASSWORD:\n  description: Postgres superuser password.\n  mint: true\n",
        encoding="utf-8",
    )

    declared = load_declaration(path)

    assert declared["GRAFANA_PORT"] == SecretKey(description="Host port grafana is published on.")
    assert declared["POSTGRES_ADMIN_PASSWORD"].mint is True
    assert declared["GRAFANA_PORT"].mint is False


def test_declaration_requires_the_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        load_declaration(tmp_path / "secrets.yaml")


def test_declaration_rejects_non_mapping_documents(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_text("- S3_ACCESS_KEY\n- S3_SECRET_KEY\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-empty KEY: description"):
        load_declaration(path)


def test_declaration_rejects_non_all_caps_keys(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_text("postgres_password: nope\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not an ALL_CAPS key"):
        load_declaration(path)


def test_declaration_rejects_empty_descriptions(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_text("S3_ACCESS_KEY: {description: '', mint: true}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"key S3_ACCESS_KEY rejected"):
        load_declaration(path)


def test_declaration_rejects_unknown_spec_keys(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_text("S3_ACCESS_KEY: {description: x, rotation: weekly}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="key S3_ACCESS_KEY rejected"):
        load_declaration(path)


def test_compose_collector_reads_brace_holes_with_error_and_default_forms() -> None:
    consumed = compose_consumed_keys(COMPOSE_ARTIFACT)

    assert "DATABASE_OWNER_PASSWORD" in consumed
    assert "GRAFANA_PORT" in consumed


def test_compose_collector_honours_dollar_parity_for_escaped_content() -> None:
    consumed = compose_consumed_keys(COMPOSE_ARTIFACT)

    assert "S3_ACCESS_KEY" not in consumed
    assert "S3_SECRET_KEY" not in consumed
    assert "ALSO_ESCAPED" not in consumed


def test_compose_collector_ignores_lowercase_and_plain_values() -> None:
    consumed = compose_consumed_keys("a: ${lowercase_var}\nb: plain value\nc: $BRACELESS\n")

    assert consumed == set()


def test_k8s_collector_reads_keys_of_the_declared_secret_only() -> None:
    consumed = k8s_consumed_keys(K8S_MANIFESTS, "placeframe-secrets")

    assert consumed == {"PUBLIC_URL"}


def test_k8s_collector_treats_wholesale_secret_refs_as_keyless() -> None:
    consumed = k8s_consumed_keys(K8S_MANIFESTS, "placeframe-secrets")

    assert "S3_ACCESS_KEY" not in consumed


def test_k8s_collector_handles_yaml_documents_and_plain_blocks() -> None:
    documents = yaml.safe_dump({"env": [{"valueFrom": {"secretKeyRef": {"name": "the-secret", "key": "A_KEY"}}}]})

    assert k8s_consumed_keys(documents, "the-secret") == {"A_KEY"}
    assert k8s_consumed_keys("not yaml at all", "the-secret") == set()


def test_validate_consumed_passes_on_subsets() -> None:
    declared = {"PUBLIC_URL": SecretKey(description="x", mint=True), "GRAFANA_PORT": SecretKey(description="y")}

    validate_consumed({"PUBLIC_URL"}, declared, "k8s", "secrets.yaml")


def test_validate_consumed_names_rendering_and_undeclared_keys() -> None:
    with pytest.raises(RuntimeError, match=r"compose rendering consumes keys missing.*GRAFANA_PORT, LOKI_PORT"):
        validate_consumed(
            {"GRAFANA_PORT", "LOKI_PORT"}, {"S3_ACCESS_KEY": SecretKey(description="x")}, "compose", "secrets.yaml"
        )


def test_mint_text_lists_mint_keys_sorted_with_provenance() -> None:
    declared = {
        "PUBLIC_URL": SecretKey(description="x", mint=True),
        "GRAFANA_PORT": SecretKey(description="y"),
        "A_PASSWORD": SecretKey(description="z", mint=True),
    }

    assert mint_text(declared) == "# Generated by generate-score from the secrets declaration\nA_PASSWORD\nPUBLIC_URL\n"
