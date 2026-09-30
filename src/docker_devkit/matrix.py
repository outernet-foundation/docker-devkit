from __future__ import annotations

import json
from pathlib import Path

import typer

from .detect_gpu import GPU_TYPES
from .documents import BakeDocument, parse_bake
from .lifecycle import require_manifest

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

TARGET_CONTEXT_PREFIX = "target:"
PLAIN_VARIANT = "common"


@app.command()
def matrix_main() -> None:
    bake = parse_bake(require_manifest(Path.cwd()))
    print(f"matrix={json.dumps({'include': compute_matrix_entries(bake)})}")


# One entry per variant: targets are the dependency-closure groups of that variant's
# explicitly-buildable services, coalesced into a single leg — the shape a single
# gpu-resolved build invocation would produce, with dependents grouped with the base
# their additional_contexts `target:` references pull in.
def compute_matrix_entries(bake: BakeDocument) -> list[dict[str, str]]:
    entries_by_variant: dict[str, list[str]] = {}
    for group in dependency_groups(bake):
        explicit = sorted(
            service
            for service in group
            if bake.services[service].build.tags and service not in set(bake.cross_compile_targets)
        )
        if not explicit:
            continue
        variant = group_variant(bake, explicit)
        entries_by_variant.setdefault(variant, []).extend(explicit)

    return [
        {"targets": " ".join(sorted(entries_by_variant[variant])), "variant": variant}
        for variant in sorted(entries_by_variant)
    ]


def dependency_groups(bake: BakeDocument) -> list[list[str]]:
    parent = {service: service for service in bake.services}

    for name, service in bake.services.items():
        for context in service.build.additional_contexts.values():
            if not context.startswith(TARGET_CONTEXT_PREFIX):
                continue
            dependency = context.removeprefix(TARGET_CONTEXT_PREFIX)
            if dependency not in bake.services:
                raise RuntimeError(f"{name} references unknown build target {dependency!r} via additional_contexts")
            union(parent, find(parent, name), find(parent, dependency))

    members: dict[str, list[str]] = {}
    for service in bake.services:
        members.setdefault(find(parent, service), []).append(service)
    return sorted(sorted(group) for group in members.values())


# Tagless services never become explicit targets (bake rejects a tagless target under
# --push) — they ride along as in-invocation dependencies of the group that needs them,
# so the variant is decided by the tagged services alone.
def group_variant(bake: BakeDocument, explicit: list[str]) -> str:
    variants = {service_variant(service) for service in explicit}
    if len(variants) > 1:
        raise RuntimeError(
            f"dependency group spans gpu variants ({', '.join(sorted(explicit))}) — "
            "additional_contexts must stay within one device family"
        )
    return variants.pop()


def service_variant(service: str) -> str:
    for gpu in GPU_TYPES:
        if service.endswith(f"-{gpu}"):
            return gpu
    return PLAIN_VARIANT


def find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root


def union(parent: dict[str, str], left: str, right: str) -> None:
    if left != right:
        parent[right] = left
