import sys
from pathlib import Path

import click
import pytest

from uvrev import constants
from uvrev.cli import env_option_callback, get_base_dir
from uvrev.types import UVRevProject


def create_fake_env(path: Path, is_package: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    proj = UVRevProject(path)
    proj.lock_path.touch()
    if not is_package:
        proj.pyproject_path.write_text("[tool.uv]\npackage = false\n")
    else:
        proj.pyproject_path.touch()


class TestEnvOptionCallback:
    """Tests for get_project function."""

    # Various naming conventions
    @pytest.mark.parametrize(
        "project_name",
        [
            "simple",
            "with-dashes",
            "with_underscores",
            "MixedCase",
        ],
    )
    def test_various_names(self, project_name: str, fake_basedir: Path) -> None:
        """Handles various naming conventions."""
        create_fake_env(fake_basedir / project_name)
        project = env_option_callback(None, None, project_name)

        assert project.project_path.name == project_name

    def test_selected_missing(self, fake_basedir: Path) -> None:
        """Raise exception if default env is missing"""
        with pytest.raises(click.BadParameter, match=".*foobar.*does not exist"):
            env_option_callback(None, None, "foobar")

    def test_default_missing(self, fake_basedir: Path) -> None:
        """Raise exception if default env is missing"""
        with pytest.raises(click.BadParameter, match=".*base.*does not exist"):
            env_option_callback(None, None, None)

    def test_default_is_existing(self, fake_basedir: Path) -> None:
        """Give default correctly, if existing"""
        create_fake_env(fake_basedir / constants.DEFAULT_ENV)
        project = env_option_callback(None, None, None)
        assert project.project_path.name == constants.DEFAULT_ENV

    def test_default_use_venv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        create_fake_env(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
        project = env_option_callback(None, None, None)
        assert project.project_path == tmp_path

    def test_default_ignore_own_venv(
        self, monkeypatch: pytest.MonkeyPatch, fake_basedir: Path
    ) -> None:
        """
        If the VIRTUAL_ENV is identical to the currently active env, ignore it
        """
        create_fake_env(fake_basedir / constants.DEFAULT_ENV)
        monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
        project = env_option_callback(None, None, None)
        assert project.project_path.name == constants.DEFAULT_ENV

    def test_ignore_if_env_is_package(
        self, fake_basedir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        If project is a packages it is not most likely not an uvrev env but a normal uv project
        """
        active_env = tmp_path / "active"
        create_fake_env(active_env, is_package=True)
        create_fake_env(fake_basedir / "base", is_package=False)
        monkeypatch.setenv("VIRTUAL_ENV", str(active_env / ".venv"))

        project = env_option_callback(None, None, None)
        assert project.project_path.name == constants.DEFAULT_ENV


class TestGetBaseDir:
    def test_env_undefined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If UVREV_BASE is not defined, use user_data_path"""
        monkeypatch.delenv("UVREV_BASE", raising=False)

        result = get_base_dir()

        assert result.relative_to(Path.home())
        assert "uvrev" in result.name
        assert result.is_dir()
        assert result.is_absolute()

    def test_env_defined(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """UVREV_BASE defines the base_dir"""
        target = tmp_path / "base_dir"
        monkeypatch.setenv("UVREV_BASE", str(target))

        result = get_base_dir()

        assert result == target
        assert result.is_dir()
        assert result.is_absolute()
