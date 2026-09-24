from pathlib import Path

import pytest
from pydantic import ValidationError
from docker_devkit.lifecycle import (
    LifecycleConfig,
    enforce_bake_declaration,
    expand_compose_files,
    load_lifecycle_config,
)


def _write_pyproject(root: Path, lifecycle_toml: str) -> None:
    content = f"[project]\nname = 'stack'\n\n{lifecycle_toml}"
    (root / "pyproject.toml").write_text(content, encoding="utf-8")


class TestLoadLifecycleConfig:
    def test_loads_declared_table(self, tmp_path: Path):
        _write_pyproject(
            tmp_path,
            "[tool.docker-devkit.lifecycle]\n"
            'files = ["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"]\n'
            'dev_file = "compose.dev.yml"\n',
        )

        config = load_lifecycle_config(tmp_path)

        assert config == LifecycleConfig(
            files=["compose.yml", "compose.postgres.yml", "compose.{gpu}.yml"],
            dev_file="compose.dev.yml",
        )

    def test_bare_table_defaults_to_empty(self, tmp_path: Path):
        _write_pyproject(tmp_path, "[tool.docker-devkit.lifecycle]\n")

        config = load_lifecycle_config(tmp_path)

        assert config == LifecycleConfig()

    def test_absent_table_returns_none(self, tmp_path: Path):
        _write_pyproject(tmp_path, "[tool.docker-devkit.generate-score]\nworkloads = []\n")

        assert load_lifecycle_config(tmp_path) is None

    def test_absent_tool_section_returns_none(self, tmp_path: Path):
        _write_pyproject(tmp_path, "")

        assert load_lifecycle_config(tmp_path) is None

    def test_missing_pyproject_returns_none(self, tmp_path: Path):
        assert load_lifecycle_config(tmp_path) is None

    def test_rejects_unknown_keys(self, tmp_path: Path):
        _write_pyproject(tmp_path, "[tool.docker-devkit.lifecycle]\nfiles = []\nbogus = true\n")

        with pytest.raises(ValidationError):
            load_lifecycle_config(tmp_path)

    def test_rejects_non_string_entries(self, tmp_path: Path):
        _write_pyproject(tmp_path, "[tool.docker-devkit.lifecycle]\nfiles = [1]\n")

        with pytest.raises(ValidationError):
            load_lifecycle_config(tmp_path)


class TestExpandComposeFiles:
    def test_expands_gpu_placeholder(self):
        config = LifecycleConfig(files=["compose.yml", "compose.{gpu}.yml"])

        assert expand_compose_files(config, "cuda") == [Path("compose.yml"), Path("compose.cuda.yml")]

    def test_drops_gpu_entry_when_none(self):
        config = LifecycleConfig(files=["compose.yml", "compose.{gpu}.yml"])

        assert expand_compose_files(config, "none") == [Path("compose.yml")]

    def test_keeps_concrete_files_when_none(self):
        config = LifecycleConfig(files=["compose.yml", "compose.cpu.yml"])

        assert expand_compose_files(config, "none") == [Path("compose.yml"), Path("compose.cpu.yml")]

    def test_appends_dev_file_when_include_dev(self):
        config = LifecycleConfig(files=["compose.yml"], dev_file="compose.dev.yml")

        assert expand_compose_files(config, "cuda", include_dev=True) == [
            Path("compose.yml"),
            Path("compose.dev.yml"),
        ]

    def test_omits_dev_file_by_default(self):
        config = LifecycleConfig(files=["compose.yml"], dev_file="compose.dev.yml")

        assert expand_compose_files(config, "cuda") == [Path("compose.yml")]

    def test_empty_files_stay_empty_without_dev(self):
        assert expand_compose_files(LifecycleConfig(), "cuda") == []

    def test_no_table_defaults_to_single_compose_file(self):
        assert expand_compose_files(None, "cuda") == [Path("compose.yml")]
        assert expand_compose_files(None, "cuda", include_dev=True) == [Path("compose.yml")]


class TestEnforceBakeDeclaration:
    def test_accepts_bake_with_declaration(self, tmp_path: Path):
        (tmp_path / "compose.bake.yml").write_text("", encoding="utf-8")

        enforce_bake_declaration(tmp_path, LifecycleConfig())

    def test_accepts_missing_bake_without_declaration(self, tmp_path: Path):
        enforce_bake_declaration(tmp_path, None)

    def test_rejects_bake_without_declaration(self, tmp_path: Path):
        (tmp_path / "compose.bake.yml").write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError, match="lifecycle"):
            enforce_bake_declaration(tmp_path, None)
