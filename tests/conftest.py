from collections.abc import Generator
from pathlib import Path
from textwrap import dedent

import pytest


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
