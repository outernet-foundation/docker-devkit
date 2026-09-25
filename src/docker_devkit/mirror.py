from __future__ import annotations

import os
import shlex
import tempfile
import tomllib
from pathlib import Path

import typer
from bashrun.bash import bash, bash_check
from pydantic import BaseModel, ConfigDict, field_validator

from .image_refs import ImageReference, declared_references

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

CRANE_VERSION = "v0.22.1"


class MirrorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str

    @field_validator("prefix")
    @classmethod
    def _require_bare_prefix(cls, prefix: str) -> str:
        if not prefix or prefix.endswith("/"):
            raise ValueError("mirror prefix must be non-empty and carry no trailing slash")
        return prefix


@app.command()
def mirror() -> None:
    root = Path.cwd()
    config = load_mirror_config(root)
    if config is None:
        raise RuntimeError("[tool.docker-devkit.mirror] with 'prefix' is required to mirror")

    targets = mirror_targets(root, config.prefix)
    print(f"Install crane {CRANE_VERSION}")
    sudo = "sudo " if os.geteuid() != 0 else ""
    archive = "go-containerregistry_Linux_x86_64.tar.gz"
    bash(f"curl -fsSLO https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive}")
    bash(f"{sudo}tar -xzf {archive} -C /usr/local/bin crane")
    Path(archive).unlink()
    for target in targets:
        upstream = upstream_ref(target.reference, config.prefix)
        print(f"Mirror {upstream} -> {target.reference}")
        bash(f"crane copy {upstream} {target.reference}")
    print(f"Mirrored images: {len(targets)}")


def load_mirror_config(root: Path) -> MirrorConfig | None:
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get("docker-devkit", {}).get("mirror")
    if section is None:
        return None
    return MirrorConfig.model_validate(section)


def is_mirrored(reference: str, prefix: str) -> bool:
    return reference.startswith(f"{prefix}/")


def upstream_ref(reference: str, prefix: str) -> str:
    return reference[len(prefix) + 1 :]


def reference_exists(reference: str) -> bool:
    return bash_check(f"docker buildx imagetools inspect {shlex.quote(reference)}")


def mirror_targets(root: Path, prefix: str) -> list[ImageReference]:
    return sorted(
        (occurrence for occurrence in declared_references(root) if is_mirrored(occurrence.reference, prefix)),
        key=lambda occurrence: occurrence.reference,
    )


def upstream_fallback(lock_data: dict[str, str], prefix: str, *, allow_upstream_fallback: bool) -> dict[str, str]:
    missing = {
        name: value for name, value in lock_data.items() if is_mirrored(value, prefix) and not reference_exists(value)
    }
    if not missing:
        return {}
    if not allow_upstream_fallback:
        value = next(iter(missing.values()))
        reference, _, digest = value.rpartition("@")
        raise RuntimeError(
            f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored yet. "
            "If this is a new upstream dependency that has not been through CI, pass --allow-upstream-fallback "
            "to pull the same digest from upstream for this run."
        )
    return {name: upstream_ref(value, prefix) for name, value in missing.items()}


def write_fallback_overlay(substitutions: dict[str, str]) -> Path:
    overlay = Path(tempfile.gettempdir()) / f"docker-devkit-upstream-fallback-{os.getpid()}.env"
    overlay.write_text(
        "\n".join(f"{name}={value}" for name, value in sorted(substitutions.items())) + "\n",
        encoding="utf-8",
    )
    return overlay


def main() -> None:
    app()


if __name__ == "__main__":
    main()
