import json
from pathlib import Path

import pytest

from docker_devkit.documents import BakeDocument
from docker_devkit.matrix import compute_matrix_entries


def service(dockerfile: str, *, tags: bool = True, contexts: dict[str, str] | None = None) -> dict[str, object]:
    build: dict[str, object] = {"dockerfile": dockerfile}
    if tags:
        build["tags"] = [f"registry/{dockerfile}:latest"]
    if contexts is not None:
        build["additional_contexts"] = contexts
    return {"build": build}


TAGGED_BASES = BakeDocument.model_validate({
    "services": {
        "api": service("docker/api/Dockerfile"),
        "postgres": service("docker/postgres/Dockerfile"),
        "neural-networks-base-cuda": service("docker/neural-networks-base/Dockerfile"),
        "reconstructor-cuda": service(
            "docker/reconstructor/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
        "localizer-cuda": service(
            "docker/localizer/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
    }
})

TAGLESS_BASES = BakeDocument.model_validate({
    "services": {
        "api": service("docker/api/Dockerfile"),
        "neural-networks-base-cuda": service("docker/neural-networks-base/Dockerfile", tags=False),
        "reconstructor-cuda": service(
            "docker/reconstructor/Dockerfile", contexts={"neural-networks-base": "target:neural-networks-base-cuda"}
        ),
    },
})


def test_tagged_bases_group_with_their_dependents() -> None:
    entries = compute_matrix_entries(TAGGED_BASES)

    assert entries == [
        {"targets": "api postgres", "variant": "common"},
        {"targets": "localizer-cuda neural-networks-base-cuda reconstructor-cuda", "variant": "cuda"},
    ]


def test_tagless_bases_ride_as_dependencies_not_targets() -> None:
    entries = compute_matrix_entries(TAGLESS_BASES)

    assert entries == [
        {"targets": "api", "variant": "common"},
        {"targets": "reconstructor-cuda", "variant": "cuda"},
    ]


def test_single_service_repo_emits_matrix_of_one() -> None:
    bake = BakeDocument.model_validate({"services": {"livekit-token": service("workloads/livekit-token/Dockerfile")}})

    assert compute_matrix_entries(bake) == [{"targets": "livekit-token", "variant": "common"}]


def test_cross_compile_targets_are_excluded() -> None:
    bake = BakeDocument.model_validate({
        "services": {
            "api": service("docker/api/Dockerfile"),
            "zed-capture": service("docker/zed-capture/Dockerfile"),
        },
        "x-cross-compile-targets": ["zed-capture"],
    })

    assert compute_matrix_entries(bake) == [{"targets": "api", "variant": "common"}]


def test_non_target_additional_contexts_do_not_group() -> None:
    bake = BakeDocument.model_validate({
        "services": {
            "api": service("docker/api/Dockerfile", contexts={"shared": "docker/shared"}),
            "postgres": service("docker/postgres/Dockerfile"),
        }
    })

    assert compute_matrix_entries(bake) == [{"targets": "api postgres", "variant": "common"}]


def test_group_spanning_variants_fails_loudly() -> None:
    bake = BakeDocument.model_validate({
        "services": {
            "api": service("docker/api/Dockerfile"),
            "reconstructor-cuda": service("docker/reconstructor/Dockerfile", contexts={"api": "target:api"}),
        }
    })

    with pytest.raises(RuntimeError, match="spans gpu variants"):
        compute_matrix_entries(bake)


def test_unknown_target_reference_fails_loudly() -> None:
    bake = BakeDocument.model_validate({
        "services": {
            "api": service("docker/api/Dockerfile", contexts={"base": "target:missing-base"}),
        }
    })

    with pytest.raises(RuntimeError, match="unknown build target"):
        compute_matrix_entries(bake)


def test_matrix_main_prints_github_output_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "workloads").mkdir()
    (tmp_path / "workloads" / "images.yml").write_text(
        "services:\n  livekit-token:\n    build:\n      dockerfile: workloads/livekit-token/Dockerfile\n"
        "      tags:\n        - ghcr.io/example/livekit-token:latest\n"
    )
    monkeypatch.chdir(tmp_path)

    from docker_devkit.matrix import matrix_main

    matrix_main()

    payload = json.loads(capsys.readouterr().out.splitlines()[0][len("matrix=") :])
    assert payload == {"include": [{"targets": "livekit-token", "variant": "common"}]}
