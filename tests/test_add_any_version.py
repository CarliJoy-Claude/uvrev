"""uvrev add NAME means "any version": it replaces an existing constraint"""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from conftest import Invoker

from uvrev import constants
from uvrev.commands import git, uv
from uvrev.io import read_pyproject, write_pyproject
from uvrev.parser import any_version, parse_pyproject_deps
from uvrev.settings import Settings
from uvrev.types import UVRevProject


@pytest.fixture
def project(invoke: Invoker, fake_basedir: Path) -> UVRevProject:
    assert invoke("env", "create").exit_code == 0
    return UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())


def dependencies(project: UVRevProject) -> list[str]:
    data: Any = read_pyproject(project)
    return list(data["project"]["dependencies"])


def report(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.stdout)
    return data


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("six", "six>=0"),
        ("Six", "Six>=0"),
        ("httpie[socks]", "httpie[socks]>=0"),
        ("six; python_version < '3.13'", "six>=0; python_version < '3.13'"),
        ("six ;python_version < '3.13'", "six>=0; python_version < '3.13'"),
        # unchanged: specifier, URL, VCS, paths and unparsable input
        ("six==1.15.0", "six==1.15.0"),
        ("six>=0", "six>=0"),
        ("six @ https://example.com/six.whl", "six @ https://example.com/six.whl"),
        ("https://example.com/six-1.0-py3-none-any.whl",) * 2,
        ("git+https://example.com/six.git",) * 2,
        ("./six",) * 2,
        ("six-1.0-py3-none-any.whl",) * 2,
        ("six-1.0.tar.gz",) * 2,
        ("not a requirement",) * 2,
    ],
)
def test_any_version(requirement: str, expected: str) -> None:
    assert any_version(requirement) == expected


def test_pin_then_bare_drops_pin(invoke: Invoker, project: UVRevProject) -> None:
    assert report(invoke("add", "six==1.15.0", "--json"))["revision"] == 1
    assert dependencies(project) == ["six==1.15.0"]

    result = invoke("add", "six", "--json")

    data = report(result)
    assert "uv add --raw 'six>=0'" in result.stderr
    assert dependencies(project) == ["six>=0"]
    assert data["revision"] == 2
    assert data["dependency_changes"]["updated"] == ["six"]
    # ">=0" is shown as no constraint
    assert data["versions"]["dependencies"] == {"six": {"old": "==1.15.0", "new": ""}}
    # the locked version is kept until an upgrade
    assert data["all_changed_packages"] == []
    message = git("log", "-1", "--format=%B", project=project, capture_output=True)
    assert "^six==1.15.0 → six\n" in message

    data = report(invoke("upgrade", "six", "--json"))
    assert data["changes"]["updated"] == ["six"]


def test_repeated_is_noop(invoke: Invoker, project: UVRevProject) -> None:
    assert report(invoke("add", "six", "--json"))["revision"] == 1

    data = report(invoke("add", "six", "--json"))

    assert data["revision"] == data["previous_revision"] == 1
    assert data["all_changed_dependencies"] == []


def test_extras_and_markers_kept(invoke: Invoker, project: UVRevProject) -> None:
    data = report(
        invoke("add", "httpie[socks]", "six; python_version > '3.0'", "--json")
    )

    assert data["dependency_changes"]["added"] == ["httpie", "six"]
    assert data["versions"]["dependencies"]["httpie"] == {"old": None, "new": ""}
    httpie, six = dependencies(project)
    assert httpie == "httpie[socks]>=0"
    assert six.replace(" ", "").startswith("six>=0;python")
    assert "pysocks" in data["changes"]["added"]


def test_existing_bare_dependency(invoke: Invoker, project: UVRevProject) -> None:
    """Environments of older versions store bare names: no revision for six -> six>=0"""
    assert report(invoke("add", "six", "--json"))["revision"] == 1
    data: Any = read_pyproject(project)
    data["project"]["dependencies"] = ["six"]
    write_pyproject(project, data)
    uv("lock", project=project)
    git("commit", "--all", "-m", "like an older uvrev", project=project)
    assert dependencies(project) == ["six"]
    head = git("rev-parse", "HEAD", project=project, capture_output=True)

    result = invoke("add", "six", "--json")

    data = report(result)
    assert data["revision"] == data["previous_revision"]
    assert data["all_changed_dependencies"] == []
    # the files are kept as committed, nothing is left uncommitted
    assert dependencies(project) == ["six"]
    assert git("rev-parse", "HEAD", project=project, capture_output=True) == head
    assert git("status", "--porcelain", project=project, capture_output=True) == ""
    assert parse_pyproject_deps(project.pyproject_path.read_text()) == {"six": None}
