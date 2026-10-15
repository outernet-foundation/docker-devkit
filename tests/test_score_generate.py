from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit.score_generate import BindVolume, ComposeService, ScoreConfig, write_generated_file


def test_score_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ScoreConfig.model_validate({
            "workloads": ["api.yaml"],
            "project_name": "example",
            "cloud_storage_class": "cloud-volumes",
            "local_storage_class": "local-path",
            "publication": ["8000:api:8000"],
        })


def test_bind_volume_carries_required_source_and_target() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "bind", "source": "a", "target": "/b"}]})

    assert volume.volumes is not None
    assert isinstance(volume.volumes[0], BindVolume)
    assert volume.volumes[0].source == "a"
    assert volume.volumes[0].target == "/b"


def test_malformed_bind_fails_validation_instead_of_passing_through() -> None:
    with pytest.raises(ValidationError):
        ComposeService.model_validate({"volumes": [{"type": "bind", "target": "/b"}]})


def test_non_bind_volume_kind_passes_through_untyped() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "tmpfs", "target": "/cache"}]})

    assert volume.volumes is not None
    assert not isinstance(volume.volumes[0], BindVolume)


def test_write_normalises_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)

    write_generated_file(path, "content\n")

    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text(encoding="utf-8") == "content\n"
