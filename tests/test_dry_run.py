"""--dry-run reports what a command would change without touching anything"""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from conftest import Invoker, permissions

from uvrev import operations, revisions
from uvrev.commands import git
from uvrev.interpreter import preview_python
from uvrev.types import UVRevProject

NO_CHANGES: dict[str, list[str]] = {
    "added": [],
    "updated": [],
    "downgraded": [],
    "removed": [],
}


def snapshot(project: UVRevProject) -> dict[str, Any]:
    """Everything a dry run must not change"""
    return {
        "head": git("rev-parse", "HEAD", project=project, capture_output=True),
        "tags": git("tag", "--list", project=project, capture_output=True),
        "status": git("status", "--porcelain", project=project, capture_output=True),
        "files": revisions.current_state(project),
        "installed": operations.list_venv_packages(project),
        "permissions": permissions(project.venv_path),
        "entries": sorted(path.name for path in project.project_path.iterdir()),
    }


def report(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.stdout)
    assert data["status"] == "success"
    assert data["options"]["dry_run"] is True
    # no revision is created
    assert data["revision"] == data["previous_revision"]
    return data


@pytest.fixture
def project(upgradeable_project: UVRevProject) -> UVRevProject:
    """Project with sampleproject 3.0.0 (upgradeable to 4.0.0) and peppercorn 0.5

    The packages are locked, but not installed: the lock file of the test data
    can not be synced with every Python version.
    """
    return upgradeable_project


class TestUpgrade:
    def test_all(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("upgrade", "--dry-run", "--json")

        data = report(result)
        assert data["options"] == {"python": None, "dry_run": True}
        assert data["changes"]["updated"] == ["peppercorn", "sampleproject"]
        assert data["versions"]["packages"]["sampleproject"]["old"] == "3.0.0"
        assert data["versions"]["packages"]["sampleproject"]["new"] != "3.0.0"
        assert data["dependency_changes"] == NO_CHANGES
        assert data["python"] is None
        # uv only locks, in a copy of the files
        assert "uv lock --upgrade" in result.stderr
        assert "uv sync" not in result.stderr
        assert "chmod" not in result.stderr
        assert snapshot(project) == before

        # the real command changes exactly that
        real = json.loads(invoke("upgrade", "--json").stdout)
        assert real["options"] == {"python": None, "dry_run": False}
        assert real["revision"] == 2
        for field in ("changes", "versions", "dependency_changes"):
            assert real[field] == data[field]

    def test_package(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("upgrade", "peppercorn", "--dry-run", "--json")

        data = report(result)
        assert data["arguments"] == ["peppercorn"]
        assert data["all_changed_packages"] == ["peppercorn"]
        assert "uv lock --upgrade-package peppercorn" in result.stderr
        assert snapshot(project) == before

    def test_without_json(self, invoke: Invoker, project: UVRevProject) -> None:
        """The changelog of the change is shown"""
        before = snapshot(project)

        result = invoke("upgrade", "--dry-run")

        assert result.exit_code == 0
        assert result.stdout == ""
        assert "^sampleproject==3.0.0 → sampleproject==" in result.stderr
        assert "Dry run: nothing was changed." in result.stderr
        assert "created revision" not in result.stderr
        assert snapshot(project) == before


class TestAdd:
    def test_add(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("add", "six", "--dry-run", "--json")

        data = report(result)
        assert data["options"] == {
            "upgrade_all": False,
            "python": None,
            "dry_run": True,
        }
        assert data["changes"] == {**NO_CHANGES, "added": ["six"]}
        assert data["dependency_changes"] == {**NO_CHANGES, "added": ["six"]}
        assert data["versions"]["dependencies"] == {"six": {"old": None, "new": ""}}
        assert "uv add --no-sync --raw 'six>=0'" in result.stderr
        assert "Created revision" not in result.stderr
        assert snapshot(project) == before

    def test_upgrade_all(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("add", "--upgrade-all", "six", "--dry-run", "--json")

        data = report(result)
        assert data["changes"]["added"] == ["six"]
        assert data["changes"]["updated"] == ["peppercorn", "sampleproject"]
        assert "uv add --no-sync --raw --upgrade 'six>=0'" in result.stderr
        assert "uv lock --upgrade" in result.stderr
        assert snapshot(project) == before

    def test_nothing_to_do(self, invoke: Invoker, project: UVRevProject) -> None:
        """Adding an existing dependency would change nothing"""
        data = report(invoke("add", "sampleproject", "--dry-run", "--json"))

        assert data["changes"] == data["dependency_changes"] == NO_CHANGES
        # only tool.uv.environments would be set, as the test data has none
        assert data["environments"]["old"] == []

        assert invoke("add", "sampleproject").exit_code == 0
        before = snapshot(project)

        data = report(invoke("add", "sampleproject", "--dry-run", "--json"))

        assert data["changes"] == data["dependency_changes"] == NO_CHANGES
        assert data["environments"] is None
        assert snapshot(project) == before

    def test_failure(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("add", "uvrev-does-not-exist", "--dry-run", "--json")

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["status"] == "error"
        assert data["options"]["dry_run"] is True
        assert snapshot(project) == before

    def test_verify_is_skipped(self, invoke: Invoker, project: UVRevProject) -> None:
        result = invoke("add", "six", "--dry-run", "--verify", "--json")

        assert report(result)["verify"] is None
        assert "uv sync" not in result.stderr


class TestRemove:
    def test_remove(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("remove", "sampleproject", "--dry-run", "--json")

        data = report(result)
        assert data["options"] == {"ignore_missing": False, "dry_run": True}
        assert data["changes"]["removed"] == ["peppercorn", "sampleproject"]
        assert data["dependency_changes"]["removed"] == ["sampleproject"]
        assert "uv remove --no-sync sampleproject" in result.stderr
        assert "Removed" not in result.stderr
        assert snapshot(project) == before

    def test_ignore_missing(self, invoke: Invoker, project: UVRevProject) -> None:
        result = invoke(
            "remove", "--ignore-missing", "not-installed", "--dry-run", "--json"
        )

        data = report(result)
        assert data["changes"] == NO_CHANGES
        assert "uv remove" not in result.stderr


class TestRestore:
    def test_restore(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("restore", "0", "--dry-run", "--json")

        data = report(result)
        assert data["arguments"] == ["0"]
        assert data["options"] == {"dry_run": True}
        assert data["changes"]["removed"] == ["peppercorn", "sampleproject"]
        assert data["dependency_changes"]["removed"] == ["sampleproject"]
        # uv.lock of the revision is only checked, nothing is installed
        assert "uv lock --check" in result.stderr
        assert "uv sync" not in result.stderr
        assert "Restored" not in result.stderr
        assert snapshot(project) == before

    def test_outdated_lock(self, invoke: Invoker, project: UVRevProject) -> None:
        """The dry run fails like the restore: uv.lock of the test data is outdated"""
        before = snapshot(project)

        result = invoke("restore", "1", "--dry-run", "--json")

        assert result.exit_code == 1
        assert json.loads(result.stdout)["status"] == "error"
        assert "uv lock --check" in result.stderr
        assert snapshot(project) == before

    def test_unknown_revision(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)

        result = invoke("restore", "99", "--dry-run", "--json")

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["status"] == "error"
        assert data["error_message"] == "Revision 99 does not exist"
        assert snapshot(project) == before


class TestUpgradePython:
    def test_same_version(self, invoke: Invoker, project: UVRevProject) -> None:
        before = snapshot(project)
        pinned = project.python_version_path.read_text().strip()

        result = invoke("upgrade-python", pinned, "--dry-run", "--json")

        data = report(result)
        assert data["arguments"] == [pinned]
        assert data["python"] is None
        assert data["changes"] == NO_CHANGES
        assert "uv lock\n" in result.stderr
        assert "is already used" not in result.stderr
        assert snapshot(project) == before

    def test_predicted_interpreter(self, project: UVRevProject) -> None:
        """If the pinned version changes, the interpreter uv finds is reported"""
        exact = project.python_lock_path.read_text().strip()
        # "changed" pin that works with a single installed Python: the exact version
        with operations.scratch_copy(project) as scratch:
            scratch.python_version_path.write_text(f"{exact}\n")
            project.python_lock_path.write_text("3.0.0\n")

            change = revisions.preview_change(project, scratch)

        assert not change.created
        assert {name: str(v) for name, v in change.python.new.items()} == {
            "python": exact
        }

    def test_unknown_interpreter(self, project: UVRevProject) -> None:
        """Without an interpreter for the new version the recorded one is kept"""
        recorded = project.python_lock_path.read_text()
        with operations.scratch_copy(project) as scratch:
            scratch.python_version_path.write_text("3.99\n")

            assert preview_python(project, scratch) == recorded

    def test_no_recorded_version(self, project: UVRevProject, tmp_path: Path) -> None:
        """Environments of old versions have no recorded interpreter version"""
        project.python_lock_path.unlink()
        project.python_version_path.unlink()
        with operations.scratch_copy(project) as scratch:
            # missing files are not copied
            assert not scratch.python_version_path.exists()
            assert preview_python(project, scratch) == ""
