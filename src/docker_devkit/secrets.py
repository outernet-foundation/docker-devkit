from __future__ import annotations

from typing import Any, TypeIs

from pydantic import BaseModel, ConfigDict, field_validator

# Mint keys ride the externally managed k8s Secret named by k8s_secret; declaration loading
# and consumption scanning live in score_generate's contract-enforcement stage.


class SecretKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    mint: bool = False

    @field_validator("description")
    @classmethod
    def _require_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("every declared key needs a one-line description")
        return value


def validate_consumed(consumed: set[str], declared: dict[str, SecretKey], rendering: str, source: str) -> None:
    undeclared = sorted(consumed - declared.keys())
    if undeclared:
        raise RuntimeError(
            f"{source}: the {rendering} rendering consumes keys missing from the secrets declaration: "
            + ", ".join(undeclared)
        )


def collect_secret_keys(node: Any, secret_name: str, consumed: set[str]) -> None:
    if is_mapping(node):
        reference = node.get("secretKeyRef")
        if is_mapping(reference) and reference.get("name") == secret_name:
            key = reference.get("key")
            if isinstance(key, str):
                consumed.add(key)

        for value in node.values():
            collect_secret_keys(value, secret_name, consumed)

    elif _is_list(node):
        for item in node:
            collect_secret_keys(item, secret_name, consumed)


def is_mapping(node: Any) -> TypeIs[dict[Any, Any]]:
    return isinstance(node, dict)


def _is_list(node: Any) -> TypeIs[list[Any]]:
    return isinstance(node, list)
