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
    print(json.dumps(compute_matrix_entries(bake)))


# Emit one entry per variant × cohort cell, coalescing each cell's dependency-closure groups
# into a single leg; give the cross-compile cohort its own legs so QEMU-slow per-arch builds
# parallelize away from native ones.
def compute_matrix_entries(bake: BakeDocument) -> list[dict[str, str]]:
    cohort = cross_compile_cohort(bake)
    entries_by_cell: dict[tuple[str, bool], list[str]] = {}
    for group in dependency_groups(bake):
        explicit = sorted(service for service in group if bake.services[service].build.tags)
        if not explicit:
            continue
        # Decide the variant from tagged services alone; tagless services ride as dependencies.
        variants = {
            next((gpu for gpu in GPU_TYPES if service.endswith(f"-{gpu}")), PLAIN_VARIANT) for service in explicit
        }
        if len(variants) > 1:
            raise RuntimeError(
                f"dependency group spans gpu variants ({', '.join(sorted(explicit))}) — "
                "additional_contexts must stay within one device family"
            )
        variant = variants.pop()
        memberships = {service in cohort for service in explicit}
        if len(memberships) > 1:
            raise RuntimeError(
                f"dependency group spans cross-compile cohort membership ({', '.join(explicit)}) — "
                "additional_contexts must stay within one cohort"
            )
        entries_by_cell.setdefault((variant, memberships.pop()), []).extend(explicit)

    return [
        {"targets": " ".join(sorted(targets)), "variant": variant}
        for (variant, _), targets in sorted(entries_by_cell.items())
    ]


# Build the cohort from every service declaring `platforms:` plus the hand-listed
# x-cross-compile-targets members.
def cross_compile_cohort(bake: BakeDocument) -> set[str]:
    unknown = set(bake.cross_compile_targets) - set(bake.services)
    if unknown:
        raise RuntimeError(f"x-cross-compile-targets names unknown services: {', '.join(sorted(unknown))}")
    pinned = {service for service, config in bake.services.items() if config.build.platforms}
    return pinned | set(bake.cross_compile_targets)


def dependency_groups(bake: BakeDocument) -> list[list[str]]:
    parent = {service: service for service in bake.services}

    for name, service in bake.services.items():
        for context in service.build.additional_contexts.values():
            if not context.startswith(TARGET_CONTEXT_PREFIX):
                continue
            dependency = context.removeprefix(TARGET_CONTEXT_PREFIX)
            if dependency not in bake.services:
                raise RuntimeError(f"{name} references unknown build target {dependency!r} via additional_contexts")
            left, right = find(parent, name), find(parent, dependency)
            if left != right:
                parent[right] = left

    members: dict[str, list[str]] = {}
    for service in bake.services:
        members.setdefault(find(parent, service), []).append(service)
    return sorted(sorted(group) for group in members.values())


def find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root
