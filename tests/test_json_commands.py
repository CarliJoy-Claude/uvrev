"""--json of env create, protect, unprotect and verify"""

import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result
from conftest import Invoker

from uvrev import constants
from uvrev.cli import cli
from uvrev.protection import unprotect
from uvrev.reporting import Services
from uvrev.settings import Settings, load_settings
from uvrev.types import UVRevProject
from uvrev.verify import Problem, VerifyResult

HEADER = ("schema_version", "status", "command", "arguments", "options")


def report(result: Result, exit_code: int = 0) -> dict[str, Any]:
    """Parse the report: nothing else is on stdout, the common fields come first"""
    assert result.exit_code == exit_code, result.output
    data: dict[str, Any] = json.loads(result.stdout)
    assert tuple(data)[: len(HEADER)] == HEADER
    assert tuple(data)[-1] == "error_message"
    assert data["schema_version"] == 2
    assert data["status"] == ("error" if exit_code else "success")
    assert (data["error_message"] is None) is (exit_code == 0)
    return data


def module(project: UVRevProject) -> Path:
    return project.site_packages[0] / "sample" / "simple.py"


class TestEnvCreate:
    def test_created(self, invoke: Invoker, fake_basedir: Path) -> None:
        result = invoke("env", "create", "--json")

        project = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
        pinned = project.python_version_path.read_text().strip()
        exact = project.python_lock_path.read_text().strip()
        assert report(result) == {
            "schema_version": 2,
            "status": "success",
            "command": "env create",
            "arguments": [],
            "options": {"python": None, "force": False},
            "created": True,
            "path": str(project.project_path),
            # the exact version of the interpreter and the pinned request
            "python": exact,
            "python_request": pinned,
            "error_message": None,
        }
        assert exact.startswith(f"{pinned}.")
        assert "uv init" in result.stderr

    def test_existing(self, invoke: Invoker, fake_basedir: Path) -> None:
        """created is false and --python is not applied, but reported"""
        first = report(invoke("env", "create", "demo", "--json"))
        pinned = first["python_request"]

        result = invoke("env", "create", "demo", "--python", "3.99", "--json")

        data = report(result)
        assert data["arguments"] == ["demo"]
        assert data["options"] == {"python": "3.99", "force": False}
        assert data["created"] is False
        assert data["python_request"] == pinned
        assert data["python"] == first["python"]
        assert data["path"] == first["path"]
        assert f"has Python {pinned} pinned, --python 3.99 is ignored" in result.stderr
        assert "uvrev upgrade-python 3.99" in result.stderr

    def test_existing_same_request(self, invoke: Invoker, fake_basedir: Path) -> None:
        """No warning if the request is the pinned one"""
        pinned = report(invoke("env", "create", "--json"))["python_request"]

        result = invoke("env", "create", "--python", pinned)

        assert result.exit_code == 0
        assert "is ignored" not in result.stderr

    def test_existing_other_request(self, invoke: Invoker, fake_basedir: Path) -> None:
        """Only the pinned request is compared, not what it resolves to"""
        first = report(invoke("env", "create", "--json"))

        result = invoke("env", "create", "--python", first["python"])

        assert result.exit_code == 0
        assert f"--python {first['python']} is ignored" in result.stderr

    def test_failure(self, invoke: Invoker, fake_basedir: Path) -> None:
        """A folder that is no complete environment still fails without --force"""
        (fake_basedir / "broken").mkdir(parents=True)

        data = report(invoke("env", "create", "broken", "--json"), exit_code=1)

        assert data["created"] is False
        assert data["python"] is None
        assert data["python_request"] is None
        assert data["path"] == str(fake_basedir / "broken")
        assert "already exists" in data["error_message"]


class TestProtect:
    def test_changed(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        venv = str(synced_project.venv_path)

        # the venv is protected after every change: nothing to do
        data = report(invoke("protect", "--json"))
        assert data["command"] == "protect"
        assert (data["changed"], data["protected"], data["path"]) == (False, True, venv)
        assert (data["state"], data["repair"]) == (None, None)

        data = report(invoke("unprotect", "--json"))
        assert (data["changed"], data["protected"], data["path"]) == (True, False, venv)
        assert (data["state"], data["repair"]) == ("post-chmod", None)

        # already unprotected
        data = report(invoke("unprotect", "--json"))
        assert (data["changed"], data["protected"]) == (False, False)
        assert data["state"] == "post-chmod"

        # nothing was changed in the meantime, so nothing is repaired
        data = report(invoke("protect", "--json"))
        assert (data["changed"], data["protected"], data["state"]) == (True, True, None)
        assert data["repair"] == {"mode": "fast", "packages": [], "removed_files": []}

    def test_repair(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        report(invoke("unprotect", "--json"))
        module(synced_project).write_text("print('hotfix')")
        extra = synced_project.site_packages[0] / "extra.py"
        extra.write_text("")

        data = report(invoke("protect", "--json"))

        assert data["repair"] == {
            "mode": "fast",
            "packages": ["sampleproject"],
            "removed_files": [str(extra)],
        }

    def test_failure(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("UVREV_CHMOD_COMMAND", "uvrev-test-missing-chmod")

        data = report(invoke("protect", "--json"), exit_code=1)

        assert data["changed"] is False
        assert data["protected"] is None
        assert (data["state"], data["repair"]) == (None, None)
        assert "command was not found" in data["error_message"]


class TestVerify:
    def test_fine(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        data = report(invoke("verify", "--json"))

        assert data["command"] == "verify"
        assert data["options"] == {"fix": False}
        assert data["problems"] == data["remaining_problems"] == []
        assert data["fixed"] is False

    def test_problems_and_fix(
        self, invoke: Invoker, synced_project: UVRevProject
    ) -> None:
        unprotect(synced_project)
        module(synced_project).write_text("print('changed')")
        modified = {
            "kind": "modified",
            "path": str(module(synced_project)),
            "package": "sampleproject",
            "is_warning": False,
        }

        result = invoke("verify", "--json")

        data = report(result, exit_code=1)
        assert data["problems"] == data["remaining_problems"] == [modified]
        assert data["fixed"] is False
        assert data["error_message"] == (
            "1 problem(s) found, fix them with 'uvrev verify --fix'"
        )
        # the problems are logged as well
        assert "simple.py (sampleproject): modified" in result.stderr

        data = report(invoke("verify", "--fix", "--json"))

        assert data["options"] == {"fix": True}
        assert data["problems"] == [modified]
        assert data["remaining_problems"] == []
        assert data["fixed"] is True

        # nothing left to fix
        data = report(invoke("verify", "--fix", "--json"))
        assert data["problems"] == []
        assert data["fixed"] is False

    def test_untracked(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        """A file that no package installed is removed by --fix"""
        unprotect(synced_project)
        extra = synced_project.site_packages[0] / "extra.py"
        extra.write_text("")

        data = report(invoke("verify", "--fix", "--json"))

        untracked = {
            "kind": "untracked",
            "path": str(extra),
            "package": None,
            "is_warning": False,
        }
        assert data["problems"] == [untracked]
        assert data["remaining_problems"] == []
        assert data["fixed"] is True
        assert not extra.exists()

    def test_not_fixable(self, synced_project: UVRevProject) -> None:
        def unfixable(project: UVRevProject, fix: bool) -> VerifyResult:
            problems = (Problem("modified", Path("broken.py"), None),)
            return VerifyResult(problems, problems)

        services = Services(load_settings(), verify=unfixable)

        result = CliRunner().invoke(cli, ("verify", "--fix", "--json"), obj=services)

        data = report(result, exit_code=1)
        assert data["error_message"] == "1 problem(s) could not be fixed"
        assert len(data["remaining_problems"]) == 1
        assert data["fixed"] is False

    def test_failure(self, synced_project: UVRevProject) -> None:
        """A failure of verify itself is reported without problems"""

        def failing(project: UVRevProject, fix: bool) -> VerifyResult:
            raise click.ClickException("uv failed")

        services = Services(load_settings(), verify=failing)

        result = CliRunner().invoke(cli, ("verify", "--json"), obj=services)

        data = report(result, exit_code=1)
        assert data["error_message"] == "uv failed"
        assert data["problems"] == data["remaining_problems"] == []
