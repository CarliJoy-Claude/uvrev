import os
import shutil
from collections.abc import Generator
from pathlib import Path
from textwrap import dedent
from typing import Protocol

import pytest
from click.testing import CliRunner, Result

import uvrev.cli
from uvrev import constants, operations, settings
from uvrev.types import UVRevProject


@pytest.fixture
def fake_basedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base_dir = tmp_path / "fake_basedir"
    monkeypatch.setenv("UVREV_BASE", str(base_dir))
    return base_dir


@pytest.fixture(autouse=True)
def fix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Fix environemnt to deliver the same test results everywhere
    """
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("UV_FROZEN", raising=False)
    monkeypatch.setenv("UV_LINK_MODE", "copy")


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Never use the settings of the user running the tests

    Returns:
        Path of the (not yet existing) settings file
    """
    config_file = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(settings, "default_config_file", lambda: config_file)
    for name in os.environ:
        if name.startswith("UVREV_") and name != "UVREV_BASE":
            monkeypatch.delenv(name)
    return config_file


@pytest.fixture(scope="session", autouse=True)
def configure_git(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    """
    Configure git to run tests everywhere identical and without user interaction
    """
    git_conf_dir = tmp_path_factory.mktemp("git")
    git_conf = git_conf_dir / "config"
    config = dedent(
        """
        [user]
        name = Testuser
        email = test@example.com

        [init]
        defaultBranch = main

        [commit]
        gpgsign = false
        """
    )
    git_conf.write_text(config)
    mp = pytest.MonkeyPatch()
    mp.setenv("GIT_CONFIG_NOSYSTEM", "true")
    mp.setenv("GIT_CONFIG_GLOBAL", str(git_conf))
    yield
    mp.undo()


@pytest.fixture
def data_dir() -> Path:
    return Path(__file__).parent / "data"


class Invoker(Protocol):
    def __call__(self, *args: str) -> Result: ...


@pytest.fixture
def invoke() -> Invoker:
    """Create a CLI test runner."""
    runner = CliRunner()

    def invoke(*args: str) -> Result:
        return runner.invoke(uvrev.cli.cli, args=args)

    return invoke


@pytest.fixture
def sample_project(fake_basedir: Path, data_dir: Path) -> UVRevProject:
    """Create a project with a lock file that is not yet installed"""
    proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV)
    operations.create_env(fake_basedir, constants.DEFAULT_ENV, constants.DEFAULT_PYTHON)
    shutil.copytree(data_dir / "sample", proj.project_path, dirs_exist_ok=True)
    operations.create_revision(proj, "Add sampleproject")
    return proj


def writable_paths(venv: Path) -> tuple[Path, ...]:
    """All files and directories of the venv with any write permission"""
    paths = (venv, *venv.rglob("*")) if venv.is_dir() else ()
    return tuple(
        path for path in paths if not path.is_symlink() and path.lstat().st_mode & 0o222
    )
