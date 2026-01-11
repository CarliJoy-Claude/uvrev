import sys
from pathlib import Path

import click
import pytest

from uvrev import constants
from uvrev.cli import env_option_callback
from uvrev.types import UVRevProject


def create_fake_env(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    proj = UVRevProject(path)
    proj.lock_path.touch()
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
