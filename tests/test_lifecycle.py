from pathlib import Path

from docker_devkit.lifecycle import resolve_manifest


class TestResolveManifest:
    def test_resolves_images_manifest(self, tmp_path: Path):
        (tmp_path / "workloads").mkdir()
        (tmp_path / "workloads" / "images.yml").write_text("", encoding="utf-8")

        assert resolve_manifest(tmp_path) == tmp_path / "workloads" / "images.yml"

    def test_returns_none_without_manifest(self, tmp_path: Path):
        assert resolve_manifest(tmp_path) is None
