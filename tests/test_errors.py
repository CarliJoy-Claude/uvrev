"""With --json every failure prints a report, missing commands are named"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from conftest import Invoker

from uvrev.cli import cli
from uvrev.commands import git, uv
from uvrev.exceptions import CommandNotFoundError
from uvrev.reporting import Services
from uvrev.settings import Settings, installed_python_versions, load_settings
from uvrev.types import UVRevProject
from uvrev.verify import VerifyResult


def error_report(result: Result) -> dict[str, Any]:
    """The report of a failed command: nothing else is on stdout, exit code 1"""
    assert result.exit_code == 1, result.output
    report: dict[str, Any] = json.loads(result.stdout)
    assert report["status"] == "error"
    assert report["all_changed_packages"] == []
    return report


def only_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Restrict the PATH to the given commands"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        target = shutil.which(name)
        assert target is not None
        (bin_dir / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(bin_dir))


class TestUnknownEnvironment:
    def test_json(self, invoke: Invoker, fake_basedir: Path) -> None:
        result = invoke("add", "-e", "nope", "six", "--upgrade-all", "--json")

        report = error_report(result)
        assert "Project 'nope' does not exist" in report["error_message"]
        assert report["command"] == "add"
        assert report["arguments"] == ["six"]
        assert report["options"] == {
            "upgrade_all": True,
            "python": None,
            "dry_run": False,
        }
        assert report["revision"] is None

    def test_without_json(self, invoke: Invoker, fake_basedir: Path) -> None:
        """Without --json it is a usage error like before"""
        result = invoke("add", "-e", "nope", "six")

        assert result.exit_code == 2
        assert result.stdout == ""
        assert "Usage:" in result.stderr
        assert "Project 'nope' does not exist" in result.stderr

    def test_usage_error_has_no_report(self, invoke: Invoker) -> None:
        """Wrong usage of the command line is no failure of the command"""
        result = invoke("add", "--json", "--no-such-option", "six")

        assert result.exit_code == 2
        assert result.stdout == ""


def test_invalid_settings_json(
    invoke: Invoker, synced_project: UVRevProject, isolate_settings: Path
) -> None:
    isolate_settings.parent.mkdir(parents=True)
    isolate_settings.write_text('[uvrev]\nprotect = "maybe"\n')

    report = error_report(invoke("sync", "--json"))

    assert "Invalid settings" in report["error_message"]
    assert report["command"] == "sync"


class TestMissingCommand:
    def test_uv_json(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        only_commands(tmp_path, monkeypatch, "git", "chmod")

        report = error_report(invoke("add", "six", "--json"))

        assert "The 'uv' command was not found" in report["error_message"]
        assert report["revision"] == 1
        # nothing is left behind
        status = git(
            "status", "--porcelain", project=synced_project, capture_output=True
        )
        assert status == ""

    def test_uv(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        only_commands(tmp_path, monkeypatch, "git", "chmod")

        result = invoke("sync")

        assert result.exit_code == 1
        assert "The 'uv' command was not found" in result.stderr
        assert "Could not convert" not in result.stderr

    def test_git(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        only_commands(tmp_path, monkeypatch)

        result = invoke("history")

        assert result.exit_code == 1
        assert "The 'git' command was not found" in result.stderr

    def test_settings_without_uv(self) -> None:
        """The default of python_versions_supported does not need uv"""
        assert installed_python_versions("uvrev-test-missing-uv") == ()

    def test_missing_directory_is_no_missing_command(self, tmp_path: Path) -> None:
        project = UVRevProject(tmp_path / "missing", Settings())

        with pytest.raises(FileNotFoundError):
            git("status", project=project)
        with pytest.raises(FileNotFoundError):
            uv("--version", project=project)

    def test_message(self) -> None:
        error = CommandNotFoundError("uv")

        assert error.command == "uv"
        assert "install it" in error.format_message()


def test_unexpected_error_json(synced_project: UVRevProject) -> None:
    """A bug is reported as well, the traceback is still raised"""

    def broken(project: UVRevProject, fix: bool) -> VerifyResult:
        raise RuntimeError("boom")

    services = Services(load_settings(), verify=broken)

    result = CliRunner().invoke(cli, ("sync", "--verify", "--json"), obj=services)

    assert isinstance(result.exception, RuntimeError)
    report = error_report(result)
    assert report["error_message"] == "RuntimeError: boom"
