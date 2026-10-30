from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import load_config

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

PLAIN_VARIANT = "common"


@app.command()
def matrix_main() -> None:
    config = load_config(Path.cwd())
    services = config.service_builds if config is not None else {}

    # Group services into dependency closures over the derived context edges; load-time
    # coherence guarantees one variant set per closure, so every member's targets land in
    # the same variant cell and one leg builds a whole closure group in a single bake
    # invocation.
    parent = {service: service for service in services}

    for name, service in services.items():
        for dependency in service.contexts:
            left, right = _find(parent, name), _find(parent, dependency)
            if left != right:
                parent[right] = left

    members: dict[str, list[str]] = {}
    for service in services:
        members.setdefault(_find(parent, service), []).append(service)

    entries_by_variant: dict[str, list[str]] = {}
    for group in sorted(sorted(member_list) for member_list in members.values()):
        for name in group:
            for target, variant in services[name].target_variants:
                entries_by_variant.setdefault(variant or PLAIN_VARIANT, []).append(target)

    print(
        json.dumps([
            {"targets": " ".join(sorted(targets)), "variant": variant}
            for variant, targets in sorted(entries_by_variant.items())
        ])
    )


def _find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root
