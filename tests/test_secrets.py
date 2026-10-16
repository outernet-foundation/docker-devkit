import pytest
from pydantic import ValidationError

from docker_devkit.secrets import SecretKey, validate_consumed


def test_secret_key_rejects_empty_descriptions() -> None:
    with pytest.raises(ValidationError, match="one-line description"):
        SecretKey.model_validate({"description": "   "})


def test_secret_key_rejects_unknown_spec_keys() -> None:
    with pytest.raises(ValidationError):
        SecretKey.model_validate({"description": "x", "rotation": "weekly"})


def test_validate_consumed_passes_on_subsets() -> None:
    declared = {"PUBLIC_URL": SecretKey(description="x", mint=True), "GRAFANA_PORT": SecretKey(description="y")}

    validate_consumed({"PUBLIC_URL"}, declared, "k8s", "secrets.yaml")


def test_validate_consumed_names_rendering_and_undeclared_keys() -> None:
    with pytest.raises(RuntimeError, match=r"compose rendering consumes keys missing.*GRAFANA_PORT, LOKI_PORT"):
        validate_consumed(
            {"GRAFANA_PORT", "LOKI_PORT"}, {"S3_ACCESS_KEY": SecretKey(description="x")}, "compose", "secrets.yaml"
        )
