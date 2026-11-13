"""Tests for tracking and changing the Python version of an environment"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

import uvrev.cli
from uvrev import constants, environment, operations, revisions
from uvrev.commands import git
from uvrev.exceptions import SubprocessFailure
from uvrev.interpreter import get_python_version
from uvrev.io import read_pyproject
from uvrev.protection import unprotect
from uvrev.settings import Settings
from uvrev.types import UVRevProject


def invoke(*args: str) -> Result:
    return CliRunner().invoke(uvrev.cli.cli, args=args)


def two_pythons() -> tuple[str, str]:
    """Two installed Python minor versions, the lower one first"""
    installed = [
        version
        for version in ("3.12", "3.13", "3.14", "3.15")
        if subprocess.run(
            ("uv", "python", "find", "--no-python-downloads", version),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ]
    if len(installed) < 2:  # pragma: no cover
        pytest.skip("needs a 2nd Python version")
    return installed[0], installed[1]


@pytest.fixture
def project(fake_basedir: Path) -> UVRevProject:
    """Freshly created project (revision 0)"""
    return environment.create_env(fake_basedir, constants.DEFAULT_ENV, Settings())


def pinned(proj: UVRevProject) -> str:
    """Python minor version pinned in .python-version"""
    return proj.python_version_path.read_text().strip()


def python_version(proj: UVRevProject) -> str:
    version = get_python_version(proj)
    assert version is not None
    return str(version)


def last_commit_message(proj: UVRevProject) -> str:
    return git("log", "-1", "--format=%B", project=proj, capture_output=True)


def commit_without_revision(proj: UVRevProject, *args: str) -> None:
    """Change the committed state without creating a revision"""
    git("commit", "--quiet", "-m", "outside of uvrev", *args, project=proj)


class TestPythonLock:
    def test_created_with_env(self, project: UVRevProject) -> None:
        """The exact Python version is committed in the first revision"""
        assert project.python_lock_path.read_text() == f"{python_version(project)}\n"
        assert python_version(project).startswith(f"{pinned(project)}.")
        committed = git(
            "show",
            f"rev_0:{constants.PYTHON_VERSION_LOCK_FILE}",
            project=project,
            capture_output=True,
        )
        assert committed == project.python_lock_path.read_text()

    def test_unchanged_python_not_in_changelog(
        self, project: UVRevProject, data_dir: Path
    ) -> None:
        shutil.copytree(data_dir / "sample", project.project_path, dirs_exist_ok=True)
        revisions.create_revision(project, "Add sampleproject")

        message = last_commit_message(project)
        assert "Pinned Versions:" in message
        assert "Python:" not in message

    def test_changed_interpreter(self, project: UVRevProject) -> None:
        """A changed interpreter (e.g. a patch update) is recorded in the next
        revision, even if nothing else changed"""
        project.python_lock_path.write_text("3.0.0\n")
        commit_without_revision(project, "--all")

        result = invoke("upgrade", "--json")

        assert result.exit_code == 0
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["revision"] == 1
        assert report["all_changed_packages"] == []
        assert report["python"] == {"old": "3.0.0", "new": python_version(project)}
        message = last_commit_message(project)
        assert f"Python:\n    ^python==3.0.0 → python=={python_version(project)}" in (
            message
        )
        assert "Pinned Versions:" not in message

    def test_env_without_lock(self, project: UVRevProject) -> None:
        """Environments created by older versions of uvrev get the lock file"""
        git("rm", "--quiet", constants.PYTHON_VERSION_LOCK_FILE, project=project)
        commit_without_revision(project)

        result = invoke("upgrade")

        assert result.exit_code == 0
        assert project.python_lock_path.exists()
        assert f"+python=={python_version(project)}" in last_commit_message(project)

    def test_no_venv(self, project: UVRevProject) -> None:
        """Without virtual environment the recorded version is kept"""
        unprotect(project)
        shutil.rmtree(project.venv_path)

        assert get_python_version(project) is None
        change = revisions.create_revision(project)
        assert not change.created
        assert project.python_lock_path.exists()

    def test_sync_reports_python(self, project: UVRevProject) -> None:
        """Syncing a removed environment reports the newly used interpreter"""
        unprotect(project)
        shutil.rmtree(project.venv_path)

        result = invoke("sync", "--json")

        assert result.exit_code == 0
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["python"] == {"old": None, "new": python_version(project)}

    def test_uv_python_ignored(
        self, project: UVRevProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UV_PYTHON does not change the interpreter of an environment"""
        before = python_version(project)
        monkeypatch.setenv("UV_PYTHON", "3.99")

        result = invoke("sync")

        assert result.exit_code == 0, result.output
        assert python_version(project) == before

    def test_create_with_python(self, fake_basedir: Path) -> None:
        """The given Python version is pinned"""
        version = get_python_version(
            environment.create_env(fake_basedir, "default", Settings())
        )
        assert version is not None
        minor = f"{version.major}.{version.minor}"

        result = invoke("env", "create", "pinned", "--python", minor)

        assert result.exit_code == 0, result.output
        assert pinned(UVRevProject(fake_basedir / "pinned", Settings())) == minor

    def test_broken_interpreter(self, tmp_path: Path) -> None:
        proj = UVRevProject(tmp_path, Settings())
        python = proj.venv_path / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)

        with pytest.raises(SubprocessFailure):
            get_python_version(proj)


class TestUpgradePython:
    def test_invalid_request(self, project: UVRevProject) -> None:
        """uv decides what a valid request is, its failure is reported"""
        result = invoke("upgrade-python", "nonsense", "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert "uv python pin nonsense" in report["error_message"]
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""

    @pytest.mark.parametrize("kind", ["exact", "range", "implementation", "path"])
    def test_other_requests(self, project: UVRevProject, kind: str) -> None:
        """Everything uv python pin accepts works, not only major.minor

        The requests resolve to the interpreter in use, so only the pin changes.
        """
        current = pinned(project)
        major, minor = map(int, current.split("."))
        exact = python_version(project)
        interpreter = (project.venv_path / "bin" / "python").resolve()
        request, stored = {
            "exact": (exact, exact),
            "range": (
                f">={current},<{major}.{minor + 1}",
                f">={current}, <{major}.{minor + 1}",
            ),
            "implementation": (f"cpython@{current}", f"cpython@{current}"),
            "path": (str(interpreter), str(interpreter)),
        }[kind]

        result = invoke("upgrade-python", request, "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["arguments"] == [request]
        assert report["revision"] == 1
        # the request as stored by uv, the interpreter is the same
        assert report["python_request"] == {"old": current, "new": stored}
        assert report["python"] is None
        assert pinned(project) == stored
        assert python_version(project) == exact
        subject = last_commit_message(project).splitlines()[0]
        assert subject == f"(rev 1) Pinned Python {request}"

    def test_same_version(self, project: UVRevProject) -> None:
        result = invoke("upgrade-python", pinned(project), "--json")

        assert result.exit_code == 0
        assert "already used" in result.stderr
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["revision"] == report["previous_revision"] == 0
        assert report["python"] is None
        assert report["python_request"] is None

    def test_changed_pin(self, project: UVRevProject) -> None:
        """A revision is created if the pinned version changes"""
        current = pinned(project)
        project.python_version_path.write_text("3.11\n")
        commit_without_revision(project, "--all")

        result = invoke("upgrade-python", current)

        assert result.exit_code == 0, result.output
        assert f"Changed Python to {current}" in result.output
        # the interpreter of the venv is the same, only the pin changed
        assert f"Pinned Python {current}" in last_commit_message(project)

    def test_missing_python_version_file(self, project: UVRevProject) -> None:
        current = pinned(project)
        project.python_version_path.unlink()

        plan = operations.plan_upgrade_python(current)
        change = operations.apply_change(project, plan).change

        # the file is recreated with the same content
        assert not change.created
        assert pinned(project) == current

    def test_unavailable_version(
        self, project: UVRevProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If uv fails, the files are reset to the last revision"""
        monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")

        result = invoke("upgrade-python", "3.99", "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["status"] == "error"
        assert report["revision"] == 0
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""
        assert python_version(project).startswith(f"{pinned(project)}.")

    def test_restore_unavailable_version(
        self, project: UVRevProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restoring a revision whose Python is not available changes nothing"""
        current = pinned(project)
        # revision 1 pins a Python version that does not exist
        project.python_version_path.write_text("3.99\n")
        commit_without_revision(project, "--all")
        git("tag", "-m", "rev 1", "--annotate", "rev_1", project=project)
        # revision 2 is the current state again
        project.python_version_path.write_text(f"{current}\n")
        commit_without_revision(project, "--all")
        git("tag", "-m", "rev 2", "--annotate", "rev_2", project=project)
        python_before = python_version(project)
        monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")

        result = invoke("restore", "1", "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["status"] == "error"
        assert report["revision"] == 2
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""
        assert pinned(project) == current
        assert python_version(project) == python_before

    def test_upgrade_and_restore(self, fake_basedir: Path) -> None:
        old_pinned, version = two_pythons()
        project = environment.create_env(fake_basedir, "two", Settings(), old_pinned)
        old_python = python_version(project)
        requires_python = read_pyproject(project)["project"]["requires-python"]

        result = invoke("upgrade-python", "-e", "two", version, "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        new_python = python_version(project)
        assert new_python.startswith(f"{version}.")
        assert report["revision"] == 1
        assert report["python"] == {"old": old_python, "new": new_python}
        assert report["python_request"] == {"old": old_pinned, "new": version}
        assert project.python_version_path.read_text() == f"{version}\n"
        # requires-python is not changed
        pyproject: Any = read_pyproject(project)
        assert pyproject["project"]["requires-python"] == requires_python
        message = last_commit_message(project)
        assert (
            f"Upgraded Python: {old_python} → {new_python} (pinned {version})"
            in message
        )
        assert f"python=={old_python} → python=={new_python}" in message

        # the requested Python version is restored as well
        result = invoke("restore", "-e", "two", "0", "--json")

        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["python"] == {"old": new_python, "new": old_python}
        assert python_version(project) == old_python
        assert pinned(project) == old_pinned

        # downgrading works as well, as requires-python was not changed
        result = invoke("upgrade-python", "-e", "two", version)
        assert result.exit_code == 0, result.output
        result = invoke("upgrade-python", "-e", "two", old_pinned)
        assert result.exit_code == 0, result.output
        assert python_version(project) == old_python
        assert (
            f"Downgraded Python: {new_python} → {old_python} (pinned {old_pinned})"
            in last_commit_message(project)
        )

    def test_below_requires_python(self, fake_basedir: Path) -> None:
        """uv python pin refuses versions not matching requires-python"""
        old, new = two_pythons()
        project = environment.create_env(fake_basedir, "two", Settings(), new)

        result = invoke("upgrade-python", "-e", "two", old, "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert "uv python pin" in report["error_message"]
        assert pinned(project) == new
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""


class TestAddPython:
    def test_add_with_same_python(self, project: UVRevProject) -> None:
        """Works with a single installed Python as well"""
        current = pinned(project)

        result = invoke("add", "--python", current, "peppercorn", "--json")

        assert result.exit_code == 0, result.output
        # the request is pinned first, uv add then uses the pin
        pin = result.stderr.index(f"uv python pin {current}")
        assert pin < result.stderr.index("uv add --raw 'peppercorn>=0'")
        assert "uv add --raw --python" not in result.stderr
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {
            "upgrade_all": False,
            "python": current,
            "dry_run": False,
        }
        assert report["python"] is None
        assert report["python_request"] is None
        assert pinned(project) == current
        # nothing changed for Python, so the message does not mention it
        subject = last_commit_message(project).splitlines()[0]
        assert subject == "(rev 1) Added: peppercorn"

    def test_add_with_python(self, fake_basedir: Path) -> None:
        """uv python pin pins the version, uv add installs it"""
        old, new = two_pythons()
        project = environment.create_env(fake_basedir, "two", Settings(), old)
        old_python = python_version(project)
        requires_python = read_pyproject(project)["project"]["requires-python"]

        result = invoke("add", "-e", "two", "--python", new, "peppercorn", "--json")

        assert result.exit_code == 0, result.output
        assert f"uv python pin {new}" in result.stderr
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {
            "upgrade_all": False,
            "python": new,
            "dry_run": False,
        }
        assert report["python"] == {"old": old_python, "new": python_version(project)}
        assert report["all_changed_dependencies"] == ["peppercorn"]
        assert pinned(project) == new
        pyproject: Any = read_pyproject(project)
        assert pyproject["project"]["requires-python"] == requires_python
        subject = last_commit_message(project).splitlines()[0]
        assert subject == (
            "(rev 1) Added: peppercorn; "
            f"Upgraded Python: {old_python} → {python_version(project)} (pinned {new})"
        )

    def test_add_with_python_below_requires_python(self, fake_basedir: Path) -> None:
        """uv refuses the version, the files are not changed"""
        old, new = two_pythons()
        project = environment.create_env(fake_basedir, "two", Settings(), new)

        result = invoke("add", "-e", "two", "--python", old, "peppercorn")

        assert result.exit_code == 1
        assert pinned(project) == new
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""


class TestUpgradeWithPython:
    """uvrev upgrade --python pins like add --python and upgrade-python"""

    def test_same_python(self, upgradeable_project: UVRevProject) -> None:
        """Works with a single installed Python as well"""
        current = pinned(upgradeable_project)

        result = invoke("upgrade", "--python", current, "--json")

        assert result.exit_code == 0, result.output
        pin = result.stderr.index(f"uv python pin {current}")
        assert pin < result.stderr.index("uv sync --upgrade")
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {"python": current, "dry_run": False}
        assert report["changes"]["updated"] == ["peppercorn", "sampleproject"]
        assert report["python"] is None
        assert report["python_request"] is None
        subject = last_commit_message(upgradeable_project).splitlines()[0]
        assert subject == "(rev 2) Upgraded all packages"

    def test_package_and_request(self, upgradeable_project: UVRevProject) -> None:
        exact = python_version(upgradeable_project)

        result = invoke("upgrade", "peppercorn", "--python", exact, "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["arguments"] == ["peppercorn"]
        assert report["all_changed_packages"] == ["peppercorn"]
        assert report["python_request"]["new"] == exact
        subject = last_commit_message(upgradeable_project).splitlines()[0]
        assert subject == f"(rev 2) Upgraded: peppercorn; Pinned Python {exact}"

    def test_dry_run(self, upgradeable_project: UVRevProject) -> None:
        current = pinned(upgradeable_project)
        exact = python_version(upgradeable_project)

        result = invoke("upgrade", "--python", exact, "--dry-run", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {"python": exact, "dry_run": True}
        assert report["python_request"] == {"old": current, "new": exact}
        assert report["changes"]["updated"] == ["peppercorn", "sampleproject"]
        assert report["revision"] == report["previous_revision"] == 1
        # nothing was changed
        assert pinned(upgradeable_project) == current
        status = git(
            "status", "--porcelain", project=upgradeable_project, capture_output=True
        )
        assert status == ""

    def test_invalid_request(self, upgradeable_project: UVRevProject) -> None:
        current = pinned(upgradeable_project)

        result = invoke("upgrade", "--python", "nonsense")

        assert result.exit_code == 1
        assert pinned(upgradeable_project) == current
        status = git(
            "status", "--porcelain", project=upgradeable_project, capture_output=True
        )
        assert status == ""

    def test_other_python(self, fake_basedir: Path) -> None:
        old, new = two_pythons()
        project = environment.create_env(fake_basedir, "two", Settings(), old)
        old_python = python_version(project)

        result = invoke("upgrade", "-e", "two", "--python", new, "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        new_python = python_version(project)
        assert report["python"] == {"old": old_python, "new": new_python}
        assert report["python_request"] == {"old": old, "new": new}
        subject = last_commit_message(project).splitlines()[0]
        assert subject == (
            "(rev 1) Upgraded all packages; "
            f"Upgraded Python: {old_python} → {new_python} (pinned {new})"
        )


def real_interpreter(project: UVRevProject) -> Path:
    """The interpreter the venv of the project links to"""
    return (project.venv_path / "bin" / "python").resolve()


class TestInterpreterPath:
    """A Python request can be the path of an interpreter"""

    def test_relative_path(
        self, project: UVRevProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative path refers to the working directory, not to the environment"""
        interpreter = real_interpreter(project)
        exact = python_version(project)
        monkeypatch.chdir(interpreter.parent)

        result = invoke("upgrade-python", f"./{interpreter.name}", "--json")

        assert result.exit_code == 0, result.output
        assert f"uv python pin {interpreter}" in result.stderr
        report: dict[str, Any] = json.loads(result.stdout)
        # the absolute path is pinned and reported
        assert report["arguments"] == [str(interpreter)]
        assert report["python_request"]["new"] == str(interpreter)
        assert report["python"] is None
        assert pinned(project) == str(interpreter)
        assert python_version(project) == exact

    def test_relative_path_dry_run(
        self, project: UVRevProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dry run happens in a temporary folder, the path is still found"""
        interpreter = real_interpreter(project)
        current = pinned(project)
        monkeypatch.chdir(interpreter.parent.parent)
        relative = f"{interpreter.parent.name}/{interpreter.name}"

        result = invoke("upgrade", "--python", relative, "--dry-run", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {"python": str(interpreter), "dry_run": True}
        assert report["python_request"] == {"old": current, "new": str(interpreter)}
        assert pinned(project) == current

    def test_add_with_path(self, project: UVRevProject) -> None:
        interpreter = real_interpreter(project)

        result = invoke("add", "--python", str(interpreter), "peppercorn", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["python_request"]["new"] == str(interpreter)
        assert pinned(project) == str(interpreter)
        subject = last_commit_message(project).splitlines()[0]
        assert subject == f"(rev 1) Added: peppercorn; Pinned Python {interpreter}"

    @pytest.mark.parametrize("kind", ["missing", "empty_folder"])
    def test_wrong_path(self, project: UVRevProject, kind: str, tmp_path: Path) -> None:
        """uv pins a wrong path, but fails to use it: the files are reset

        (A folder is a valid request for uv, if it contains bin/python.)
        """
        current = pinned(project)
        folder = tmp_path / "no-python"
        folder.mkdir()
        path = folder / "python" if kind == "missing" else folder

        result = invoke("upgrade-python", str(path), "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["status"] == "error"
        assert pinned(project) == current
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""

    def test_env_create_pins_path(
        self, project: UVRevProject, fake_basedir: Path
    ) -> None:
        """env create pins the path itself, not only the minor version"""
        interpreter = real_interpreter(project)

        result = invoke(
            "env", "create", "byPath", "--python", str(interpreter), "--json"
        )

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["python_request"] == str(interpreter)
        assert report["python"] == python_version(project)
        created = UVRevProject(fake_basedir / "byPath", Settings())
        assert pinned(created) == str(interpreter)
        status = git("status", "--porcelain", project=created, capture_output=True)
        assert status == ""

        # the same request again is no reason for a warning
        result = invoke("env", "create", "byPath", "--python", str(interpreter))
        assert result.exit_code == 0
        assert "is ignored" not in result.stderr

    def test_env_create_pins_range(
        self, project: UVRevProject, fake_basedir: Path
    ) -> None:
        """A range is pinned as given, uv stores it with spaces"""
        major, minor = map(int, pinned(project).split("."))
        request = f">={major}.{minor},<{major}.{minor + 1}"

        result = invoke("env", "create", "byRange", "--python", request, "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["python_request"] == f">={major}.{minor}, <{major}.{minor + 1}"

        result = invoke("env", "create", "byRange", "--python", request)
        assert "is ignored" not in result.stderr
