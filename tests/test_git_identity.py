"""The revisions are committed with uvrev's own git identity, without signing"""

import getpass
import socket
from pathlib import Path

import pytest
from conftest import Invoker

from uvrev import constants
from uvrev.commands import git
from uvrev.environment import git_identity
from uvrev.settings import Settings
from uvrev.types import UVRevProject


def config(project: UVRevProject, key: str) -> str:
    return git("config", "--local", key, project=project, capture_output=True).strip()


@pytest.fixture
def no_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user without git identity that signs everything, e.g. with a missing key"""
    git_config = tmp_path / "gitconfig"
    git_config.write_text(
        "[commit]\ngpgsign = true\n[tag]\ngpgsign = true\nforceSignAnnotated = true\n"
        "[gpg]\nprogram = uvrev-test-missing-gpg\n[user]\nuseConfigOnly = true\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    for var in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(var, raising=False)


def test_identity() -> None:
    assert git_identity() == f"{getpass.getuser()}@{socket.gethostname()}"


def test_identity_unknown_user() -> None:
    """Without a name for the uid, e.g. in a container with an arbitrary uid"""

    def no_user() -> str:
        raise OSError("No username set in the environment")

    assert git_identity(no_user, "host") == "uvrev@host"


def test_create_and_change(
    invoke: Invoker, fake_basedir: Path, no_identity: None
) -> None:
    result = invoke("env", "create")

    assert result.exit_code == 0, result.output
    project = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
    assert config(project, "user.name") == "uvrev"
    assert config(project, "user.email") == git_identity()
    assert config(project, "commit.gpgsign") == "false"
    assert config(project, "tag.gpgsign") == "false"

    # later revisions are committed and tagged without identity or key as well
    result = invoke("add", "peppercorn")

    assert result.exit_code == 0, result.output
    author = git(
        "log", "-1", "--format=%an <%ae>", project=project, capture_output=True
    )
    assert author.strip() == f"uvrev <{git_identity()}>"
    tagger = git(
        "for-each-ref",
        "--format=%(taggername) %(taggeremail)",
        "refs/tags/rev_1",
        project=project,
        capture_output=True,
    )
    assert tagger.strip() == f"uvrev <{git_identity()}>"


def test_fix(invoke: Invoker, fake_basedir: Path) -> None:
    """env fix configures environments created by an older uvrev"""
    assert invoke("env", "create").exit_code == 0
    project = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
    for key in ("user.name", "user.email", "commit.gpgsign", "tag.gpgsign"):
        git("config", "--local", "--unset", key, project=project)

    assert invoke("env", "fix").exit_code == 0

    assert config(project, "user.name") == "uvrev"
    assert config(project, "commit.gpgsign") == "false"
