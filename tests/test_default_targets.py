from docker_devkit.build_docker import compute_default_targets
from docker_devkit.documents import BakeDocument

BAKE_DATA = {
    "services": {
        "api": {"build": {"dockerfile": "docker/api/Dockerfile", "tags": ["registry/api:${API_SHA}"]}},
        "postgres": {
            "build": {"dockerfile": "docker/postgres/Dockerfile", "tags": ["registry/postgres:${POSTGRES_SHA}"]}
        },
        "seaweedfs": {
            "build": {"dockerfile": "docker/seaweedfs/Dockerfile", "tags": ["registry/seaweedfs:${SEAWEEDFS_SHA}"]}
        },
        "neural-networks-base-cuda": {"build": {"dockerfile": "docker/neural-networks-base-cuda/Dockerfile"}},
        "neural-networks-base-rocm": {"build": {"dockerfile": "docker/neural-networks-base-rocm/Dockerfile"}},
        "localizer-cuda": {
            "build": {"dockerfile": "docker/localizer/Dockerfile", "tags": ["registry/localizer-cuda:${LOCALIZER_SHA}"]}
        },
        "reconstructor-cuda": {
            "build": {
                "dockerfile": "docker/reconstructor/Dockerfile",
                "tags": ["registry/reconstructor-cuda:${RECONSTRUCTOR_SHA}"],
            }
        },
        "localizer-rocm": {
            "build": {"dockerfile": "docker/localizer/Dockerfile", "tags": ["registry/localizer-rocm:${LOCALIZER_SHA}"]}
        },
        "reconstructor-rocm": {
            "build": {
                "dockerfile": "docker/reconstructor/Dockerfile",
                "tags": ["registry/reconstructor-rocm:${RECONSTRUCTOR_SHA}"],
            }
        },
        "zed-capture": {
            "build": {
                "dockerfile": "docker/zed-capture/Dockerfile",
                "tags": ["registry/zed-capture:${ZED_CAPTURE_SHA}"],
            }
        },
    },
    "x-cross-compile-targets": ["zed-capture"],
}

BAKE_DOCUMENT = BakeDocument.model_validate(BAKE_DATA)


def test_none_yields_common_partition():
    assert compute_default_targets(BAKE_DOCUMENT, "none") == ["api", "postgres", "seaweedfs"]


def test_gpu_only_yields_just_that_gpus_services():
    assert compute_default_targets(BAKE_DOCUMENT, "cuda", gpu_only=True) == ["localizer-cuda", "reconstructor-cuda"]
    assert compute_default_targets(BAKE_DOCUMENT, "rocm", gpu_only=True) == ["localizer-rocm", "reconstructor-rocm"]


def test_default_with_gpu_yields_common_plus_that_gpu():
    assert compute_default_targets(BAKE_DOCUMENT, "cuda") == [
        "api",
        "postgres",
        "seaweedfs",
        "localizer-cuda",
        "reconstructor-cuda",
    ]


def test_untagged_services_are_excluded():
    for targets in (
        compute_default_targets(BAKE_DOCUMENT, "none"),
        compute_default_targets(BAKE_DOCUMENT, "cuda"),
        compute_default_targets(BAKE_DOCUMENT, "cuda", gpu_only=True),
        compute_default_targets(BAKE_DOCUMENT, "rocm", gpu_only=True),
    ):
        assert "neural-networks-base-cuda" not in targets
        assert "neural-networks-base-rocm" not in targets


def test_cross_compile_services_always_excluded():
    assert "zed-capture" not in compute_default_targets(BAKE_DOCUMENT, "none")
    assert "zed-capture" not in compute_default_targets(BAKE_DOCUMENT, "cuda")
    assert "zed-capture" not in compute_default_targets(BAKE_DOCUMENT, "cuda", gpu_only=True)


def test_missing_cross_compile_key_is_tolerated():
    bake = BakeDocument.model_validate({
        "services": {"api": {"build": {"dockerfile": "docker/api/Dockerfile", "tags": ["registry/api:${API_SHA}"]}}}
    })
    assert compute_default_targets(bake, "none") == ["api"]
