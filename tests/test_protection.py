import stat
from pathlib import Path

import pytest
from conftest import Invoker, writable_paths

from uvrev.commands import uv
from uvrev.protection import ChmodNotFoundError, protect, unprotect, writable_venv
from uvrev.settings import Settings
from uvrev.types import SubprocessFailure, UVRevProject


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def project(tmp_path: Path) -> UVRevProject:
    """A project with a fake venv: a file, a script and a symlink to an outside file"""
    project = UVRevProject(tmp_path / "project")
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


@pytest.fixture
def no_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)


class TestProtect:
    def test_removes_write_keeps_rest(self, project: UVRevProject) -> None:
        protect(project)

        venv = project.venv_path
        assert mode(venv) == 0o555
        # read permissions are kept as they are (not readable for others)
        assert mode(venv / "lib" / "module.py") == 0o440
        assert mode(venv / "bin" / "script") == 0o555
        assert writable_paths(venv) == ()

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

    def test_missing_venv(self, tmp_path: Path, no_chmod: None) -> None:
        """Nothing happens if the venv does not exist (yet), chmod is not needed"""
        project = UVRevProject(tmp_path)

        protect(project)
        unprotect(project)

    def test_invalid_mode(self, project: UVRevProject) -> None:
        project = UVRevProject(project.project_path, Settings(protect_mode="bogus"))

        with pytest.raises(SubprocessFailure, match="chmod -R bogus .venv"):
            protect(project)

    def test_chmod_missing(self, project: UVRevProject, no_chmod: None) -> None:
        with pytest.raises(ChmodNotFoundError) as exc_info:
            protect(project)

        message = exc_info.value.format_message()
        assert "'chmod' command was not found" in message
        assert "protect = false" in message
        assert "UVREV_PROTECT=false" in message


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
        project = UVRevProject(tmp_path)

        with writable_venv(project):
            project.venv_path.mkdir()

        assert mode(project.venv_path) == 0o555

    def test_protection_disabled(self, project: UVRevProject, no_chmod: None) -> None:
        """Without protection nothing is changed, chmod is not needed"""
        project = UVRevProject(project.project_path, Settings(protect=False))

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
        assert "next change of the environment protects it again" in result.output
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
        self, invoke: Invoker, synced_project: UVRevProject, no_chmod: None
    ) -> None:
        result = invoke("sync")

        assert result.exit_code == 1
        assert "'chmod' command was not found" in result.output

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
