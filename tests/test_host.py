import platform
import shutil
from pathlib import Path
from typing import get_args

import pytest

from docker_devkit.host import GPU_TYPES, Gpu, detect_gpu, host_platform, parse_env_file


def _nvidia_smi_present(name: str) -> str | None:
    return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None


def _nothing_present(_name: str) -> str | None:
    return None


def _always_true(_command: str) -> bool:
    return True


def test_gpu_types_derives_the_concrete_families() -> None:
    assert GPU_TYPES == ("cuda", "rocm")


def test_gpu_literal_derives_flat_over_the_families() -> None:
    # typer cannot annotate union types, so Gpu must flatten to one Literal.
    assert get_args(Gpu) == ("auto", "none", *GPU_TYPES)


class TestParseEnvFile:
    def test_parses_assignments_skipping_comments_and_blanks(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# comment\n\nBUNDLES=core-cuda,gateway-open\nGATEWAY_PORT='8443'\nRELOAD=\"true\"\nnot-an-assignment\n",
            encoding="utf-8",
        )

        assert parse_env_file(env) == {
            "BUNDLES": "core-cuda,gateway-open",
            "GATEWAY_PORT": "8443",
            "RELOAD": "true",
        }


class TestHostPlatform:
    def test_amd64_spellings_map_to_linux_amd64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("x86_64", "AMD64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert host_platform() == "linux/amd64"

    def test_arm64_spellings_map_to_linux_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("aarch64", "ARM64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert host_platform() == "linux/arm64"

    def test_other_architectures_refuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "riscv64")

        with pytest.raises(RuntimeError, match="Unsupported host architecture"):
            host_platform()


class TestDetectGpu:
    def test_nvidia_smi_yields_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", _nvidia_smi_present)
        monkeypatch.setattr("docker_devkit.host.bash_check", _always_true)

        gpu: Gpu = detect_gpu()

        assert gpu == "cuda"

    def test_no_gpu_host_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", _nothing_present)

        with pytest.raises(RuntimeError, match="Could not detect GPU"):
            detect_gpu()
