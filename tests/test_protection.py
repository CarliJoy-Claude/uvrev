import os
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import Invoker, writable_paths

from uvrev import constants
from uvrev.commands import uv, uv_environ
from uvrev.exceptions import ChmodNotFoundError, SubprocessFailure
from uvrev.protection import protect, unprotect, writable_venv
from uvrev.settings import Settings
from uvrev.types import UVRevProject


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def project(tmp_path: Path) -> UVRevProject:
    """A project with a fake venv: a file, a script and a symlink to an outside file"""
    project = UVRevProject(tmp_path / "project", Settings())
    venv = project.venv_path
    (venv / "lib").mkdir(parents=True)
    (venv / "bin").mkdir()
    (venv / "lib" / "module.py").write_text("x = 1")
    (venv / "lib" / "module.py").chmod(0o640)
    (venv / "bin" / "script").write_text("#!/bin/sh")
    (venv / "bin" / "script").chmod(0o755)

    outside = tmp_path / "python"
    outside.write_text("interpreter")
    outside.chmod(0o755)
    (venv / "bin" / "python").symlink_to(outside)
    return project


MISSING_CHMOD = "uvrev-test-missing-chmod"


class TestProtect:
    def test_removes_write_keeps_rest(self, project: UVRevProject) -> None:
        protect(project)

        venv = project.venv_path
        assert mode(venv) == 0o555
        # read permissions are kept as they are (not readable for others)
        assert mode(venv / "lib" / "module.py") == 0o440
        assert mode(venv / "bin" / "script") == 0o555
        assert writable_paths(venv) == ()

    def test_lock_stays_writable_for_owner(self, project: UVRevProject) -> None:
        """uv opens the lock of the venv for writing, also to only read the venv"""
        lock = project.venv_path / constants.VENV_LOCK_FILE
        lock.touch()
        lock.chmod(0o664)

        protect(project)

        assert mode(lock) == 0o644
        assert mode(project.venv_path) == 0o555

    def test_linked_lock_untouched(self, project: UVRevProject, tmp_path: Path) -> None:
        """A lock that is a link is not followed: its target stays as it is"""
        outside = tmp_path / "outside"
        outside.touch()
        outside.chmod(0o444)
        (project.venv_path / constants.VENV_LOCK_FILE).symlink_to(outside)

        protect(project)

        assert mode(outside) == 0o444

    def test_symlink_target_untouched(
        self, project: UVRevProject, tmp_path: Path
    ) -> None:
        """The interpreter the venv links to must not be changed"""
        protect(project)

        assert mode(tmp_path / "python") == 0o755

    def test_unprotect(self, project: UVRevProject) -> None:
        protect(project)

        unprotect(project)

        venv = project.venv_path
        assert mode(venv / "lib") == 0o755
        assert mode(venv / "lib" / "module.py") == 0o640
        assert mode(venv / "bin" / "script") == 0o755

    def test_configured_modes(self, project: UVRevProject) -> None:
        project = UVRevProject(
            project.project_path, Settings(protect_mode="go-w", unprotect_mode="g+w")
        )
        (project.venv_path / "lib" / "module.py").chmod(0o666)

        protect(project)
        assert mode(project.venv_path / "lib" / "module.py") == 0o644

        unprotect(project)
        assert mode(project.venv_path / "lib" / "module.py") == 0o664

    def test_missing_venv(self, tmp_path: Path) -> None:
        """Nothing happens if the venv does not exist (yet), chmod is not needed"""
        project = UVRevProject(tmp_path, Settings(chmod_command=MISSING_CHMOD))

        protect(project)
        unprotect(project)

    def test_invalid_mode(self, project: UVRevProject) -> None:
        project = UVRevProject(project.project_path, Settings(protect_mode="bogus"))

        with pytest.raises(SubprocessFailure, match=r"chmod -R bogus \.venv"):
            protect(project)

    def test_chmod_missing(self, project: UVRevProject) -> None:
        project = UVRevProject(
            project.project_path, Settings(chmod_command=MISSING_CHMOD)
        )

        with pytest.raises(ChmodNotFoundError) as exc_info:
            protect(project)

        message = exc_info.value.format_message()
        assert f"'{MISSING_CHMOD}' command was not found" in message
        assert "protect = false" in message
        assert "UVREV_PROTECT=false" in message


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="root ignores file permissions (CI runs the tests as a normal user)",
)
class TestDenied:
    """The protection really denies changes"""

    def test_write(self, project: UVRevProject) -> None:
        protect(project)

        with pytest.raises(PermissionError):
            (project.venv_path / "lib" / "module.py").write_text("x = 2")

    def test_create_and_delete(self, project: UVRevProject) -> None:
        protect(project)

        with pytest.raises(PermissionError):
            (project.venv_path / "lib" / "new.py").touch()
        with pytest.raises(PermissionError):
            (project.venv_path / "lib" / "module.py").unlink()

    def test_writable_again(self, project: UVRevProject) -> None:
        protect(project)

        with writable_venv(project):
            (project.venv_path / "lib" / "module.py").write_text("x = 2")
            (project.venv_path / "lib" / "new.py").touch()


class TestWritableVenv:
    def test_protected_afterwards(self, project: UVRevProject) -> None:
        protect(project)

        with writable_venv(project):
            assert mode(project.venv_path / "lib" / "module.py") == 0o640

        assert writable_paths(project.venv_path) == ()

    def test_protected_after_failure(self, project: UVRevProject) -> None:
        with pytest.raises(RuntimeError), writable_venv(project):
            raise RuntimeError("uv failed")

        assert writable_paths(project.venv_path) == ()

    def test_venv_created_within(self, tmp_path: Path) -> None:
        """uv may create the venv within the block, it is protected anyway"""
        project = UVRevProject(tmp_path, Settings())

        with writable_venv(project):
            project.venv_path.mkdir()

        assert mode(project.venv_path) == 0o555

    def test_protection_disabled(self, project: UVRevProject) -> None:
        """Without protection nothing is changed, chmod is not needed"""
        project = UVRevProject(
            project.project_path,
            Settings(protect=False, chmod_command=MISSING_CHMOD),
        )

        with writable_venv(project):
            pass

        assert mode(project.venv_path / "lib" / "module.py") == 0o640


class TestCli:
    def test_changes_keep_protection(self, synced_project: UVRevProject) -> None:
        assert writable_paths(synced_project.venv_path) == ()

    def test_protect_unprotect(
        self, invoke: Invoker, synced_project: UVRevProject
    ) -> None:
        result = invoke("unprotect")

        assert result.exit_code == 0
        assert "chmod -R u+w .venv" in result.output
        assert "next change of the environment repairs and protects it" in result.output
        assert writable_paths(synced_project.venv_path) != ()

        result = invoke("protect")

        assert result.exit_code == 0
        assert "chmod -R a-w .venv" in result.output
        assert writable_paths(synced_project.venv_path) == ()

    def test_protection_disabled(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("UVREV_PROTECT", "false")

        result = invoke("protect")

        assert result.exit_code == 0
        assert "Run 'uvrev unprotect' before changing" in result.output

        result = invoke("unprotect")

        assert "protects it again" not in result.output

    def test_chmod_missing(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("UVREV_CHMOD_COMMAND", MISSING_CHMOD)

        result = invoke("sync")

        assert result.exit_code == 1
        assert f"'{MISSING_CHMOD}' command was not found" in result.output
        assert "UVREV_PROTECT=false" in result.output

    def test_run_syncs_first(
        self, invoke: Invoker, sample_project: UVRevProject
    ) -> None:
        """A venv not in sync is synced before running, the run itself is protected"""
        # the lock file of the test data is not up to date with its pyproject.toml
        uv("lock", project=sample_project)

        result = invoke("run", "python", "-c", "import sample")

        assert result.exit_code == 0
        assert "uv sync --locked\n" in result.output
        assert writable_paths(sample_project.venv_path) == ()


def test_uv_locks_protected_venv(synced_project: UVRevProject) -> None:
    """uv can take its lock to read a protected venv (it only warns if it can not)"""
    assert writable_paths(synced_project.venv_path) == ()

    result = subprocess.run(
        ("uv", "--verbose", "sync", "--frozen", "--check"),
        cwd=synced_project.project_path,
        env=uv_environ(synced_project, os.environ),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Failed to acquire environment lock" not in result.stderr
