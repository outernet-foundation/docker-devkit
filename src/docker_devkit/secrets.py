from __future__ import annotations

import re
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .markers import is_mapping, is_list

# Mint keys ride the externally managed k8s Secret named by k8s_secret; the declaration
# model lives here and its enforcement stage in score_generate.

# Declaration keys are ALL_CAPS.
DECLARED_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


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


class SecretsDeclaration(RootModel[dict[str, SecretKey]]):
    @model_validator(mode="before")
    @classmethod
    def _coerce_string_specs(cls, value: Any, info: ValidationInfo) -> Any:
        context: dict[str, Any] = info.context or {}
        source = context.get("source", "the secrets declaration")
        if not is_mapping(value) or not value:
            raise ValueError(f"{source}: the secrets declaration is a non-empty KEY: description mapping")
        return {name: ({"description": spec} if isinstance(spec, str) else spec) for name, spec in value.items()}

    @model_validator(mode="after")
    def _require_all_caps_keys(self, info: ValidationInfo) -> Self:
        context: dict[str, Any] = info.context or {}
        source = context.get("source", "the secrets declaration")
        for name in self.root:
            if not DECLARED_KEY.match(name):
                raise ValueError(f"{source}: {name!r} is not an ALL_CAPS key")
        return self


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

    elif is_list(node):
        for item in node:
            collect_secret_keys(item, secret_name, consumed)
