from typing import Any

from stack_lifecycle.build_docker import compute_default_targets

BAKE_DATA: dict[str, Any] = {
    "services": {
        "api": {"build": {"tags": ["registry/api:${API_SHA}"]}},
        "postgres": {"build": {"tags": ["registry/postgres:${POSTGRES_SHA}"]}},
        "seaweedfs": {"build": {"tags": ["registry/seaweedfs:${SEAWEEDFS_SHA}"]}},
        "neural-networks-base-cuda": {"build": {}},
        "neural-networks-base-rocm": {"build": {}},
        "localizer-cuda": {"build": {"tags": ["registry/localizer-cuda:${LOCALIZER_SHA}"]}},
        "reconstructor-cuda": {"build": {"tags": ["registry/reconstructor-cuda:${RECONSTRUCTOR_SHA}"]}},
        "localizer-rocm": {"build": {"tags": ["registry/localizer-rocm:${LOCALIZER_SHA}"]}},
        "reconstructor-rocm": {"build": {"tags": ["registry/reconstructor-rocm:${RECONSTRUCTOR_SHA}"]}},
        "zed-capture": {"build": {"tags": ["registry/zed-capture:${ZED_CAPTURE_SHA}"]}},
    },
    "x-cross-compile-targets": ["zed-capture"],
}


def test_none_yields_common_partition():
    assert compute_default_targets(BAKE_DATA, "none") == ["api", "postgres", "seaweedfs"]


def test_gpu_only_yields_just_that_gpus_services():
    assert compute_default_targets(BAKE_DATA, "cuda", gpu_only=True) == ["localizer-cuda", "reconstructor-cuda"]
    assert compute_default_targets(BAKE_DATA, "rocm", gpu_only=True) == ["localizer-rocm", "reconstructor-rocm"]


def test_default_with_gpu_yields_common_plus_that_gpu():
    assert compute_default_targets(BAKE_DATA, "cuda") == [
        "api",
        "postgres",
        "seaweedfs",
        "localizer-cuda",
        "reconstructor-cuda",
    ]


def test_untagged_services_are_excluded():
    for targets in (
        compute_default_targets(BAKE_DATA, "none"),
        compute_default_targets(BAKE_DATA, "cuda"),
        compute_default_targets(BAKE_DATA, "cuda", gpu_only=True),
        compute_default_targets(BAKE_DATA, "rocm", gpu_only=True),
    ):
        assert "neural-networks-base-cuda" not in targets
        assert "neural-networks-base-rocm" not in targets


def test_cross_compile_services_always_excluded():
    assert "zed-capture" not in compute_default_targets(BAKE_DATA, "none")
    assert "zed-capture" not in compute_default_targets(BAKE_DATA, "cuda")
    assert "zed-capture" not in compute_default_targets(BAKE_DATA, "cuda", gpu_only=True)


def test_missing_cross_compile_key_is_tolerated():
    bake_data = {"services": {"api": {"build": {"tags": ["registry/api:${API_SHA}"]}}}}
    assert compute_default_targets(bake_data, "none") == ["api"]
