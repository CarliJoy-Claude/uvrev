"""uvrev sync --clean --inplace: the new venv is built next to the old one and swapped in"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import Invoker, writable_paths

from uvrev import operations
from uvrev.types import UVRevProject
from uvrev.verify import find_problems

pytestmark = pytest.mark.usefixtures("private_cache")


def module(project: UVRevProject) -> Path:
    return project.site_packages[0] / "sample" / "simple.py"


def inode(path: Path) -> int:
    return path.stat().st_ino


def mentions(venv: Path, text: bytes) -> list[Path]:
    """Files of the venv (text and bytecode) containing the text"""
    return [
        path
        for path in venv.rglob("*")
        if path.is_file() and not path.is_symlink() and text in path.read_bytes()
    ]


@pytest.fixture
def project(synced_project: UVRevProject) -> UVRevProject:
    """Protected environment with sampleproject and peppercorn installed"""
    return synced_project


class TestReplace:
    def test_replaced(self, invoke: Invoker, project: UVRevProject) -> None:
        old = inode(project.venv_path)

        result = invoke("sync", "--clean", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {"clean": True, "inplace": True}
        assert report["all_changed_packages"] == []
        # a new venv, valid, protected, and nothing is left behind
        assert inode(project.venv_path) != old
        assert not project.venv_new_path.exists()
        assert not project.venv_to_delete_path.exists()
        assert find_problems(project) == ()
        assert writable_paths(project.venv_path) == ()
        # in this order: clean the cache, build aside, sync into it, compile, protect
        steps = [
            "uv cache clean --force",
            "uv venv --relocatable .venv_new",
            "uv sync --frozen",
            "-m compileall",
            "chmod -R a-w .venv",
        ]
        positions = [result.stderr.index(step) for step in steps]
        assert positions == sorted(positions)
        # git ignores the temporary folders (uv writes a .gitignore into venvs)
        status = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=project.project_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status == ""

    def test_scripts_and_activate_work_after_the_rename(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        """The venv was built under another name, it must not remember it"""
        assert invoke("sync", "--clean").exit_code == 0

        venv = project.venv_path
        script = venv / "bin" / "sample"
        assert script.read_text().startswith("#!/bin/sh")
        ran = subprocess.run((str(script),), capture_output=True, check=False)
        assert ran.returncode == 0, ran.stderr
        active = subprocess.run(
            ("bash", "-c", f'. {venv}/bin/activate && printf %s "$VIRTUAL_ENV"'),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert active == str(venv)
        imported = subprocess.run(
            (
                str(venv / "bin" / "python"),
                "-c",
                "import sample, sys; print(sys.prefix)",
            ),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert imported == str(venv)
        # no file knows the name the venv was built under
        assert mentions(venv, b".venv_new") == []

    def test_bytecode_has_the_final_path(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        """uv would compile it into the temporary venv: it is compiled afterwards"""
        assert invoke("sync", "--clean").exit_code == 0

        compiled = list(project.venv_path.rglob("*.pyc"))
        assert compiled
        assert mentions(project.venv_path, b".venv_new") == []
        assert any(
            str(project.venv_path).encode() in path.read_bytes() for path in compiled
        )

    def test_no_bytecode_if_disabled(
        self,
        invoke: Invoker,
        project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("UVREV_COMPILE_BYTECODE", "false")

        assert invoke("sync", "--clean").exit_code == 0

        # (the interpreter compiles _virtualenv.py of every venv itself at startup)
        compiled = [
            path.name
            for path in project.venv_path.rglob("*.pyc")
            if "_virtualenv" not in path.name
        ]
        assert compiled == []

    def test_protection_disabled(
        self,
        invoke: Invoker,
        project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without protection chmod is not needed, the new venv stays writable"""
        monkeypatch.setenv("UVREV_PROTECT", "false")
        monkeypatch.setenv("UVREV_CHMOD_COMMAND", "uvrev-test-missing-chmod")

        result = invoke("sync", "--clean")

        assert result.exit_code == 0, result.output
        assert writable_paths(project.venv_path) != ()
        assert find_problems(project) == ()


class TestFailure:
    def test_old_venv_survives(
        self,
        invoke: Invoker,
        project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing can be downloaded: the new venv is removed, the old one is untouched"""
        monkeypatch.setenv("UV_OFFLINE", "1")
        old = inode(project.venv_path)

        result = invoke("sync", "--clean", "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["status"] == "error"
        assert "uv sync" in report["error_message"]
        assert "uv venv --relocatable .venv_new" in result.stderr
        assert inode(project.venv_path) == old
        assert not project.venv_new_path.exists()
        assert not project.venv_to_delete_path.exists()
        assert writable_paths(project.venv_path) == ()
        imported = subprocess.run(
            (str(project.venv_path / "bin" / "python"), "-c", "import sample"),
            check=False,
        )
        assert imported.returncode == 0

    def test_swap_is_rolled_back(self, project: UVRevProject) -> None:
        """If the new venv can not be moved into place, the old one is put back"""
        old = inode(project.venv_path)
        assert not project.venv_new_path.exists()

        with pytest.raises(OSError, match="venv_new"):
            operations.swap_venv(project)

        assert inode(project.venv_path) == old
        assert not project.venv_to_delete_path.exists()

    def test_old_venv_not_deletable(
        self,
        invoke: Invoker,
        project: UVRevProject,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The new venv is in place anyway: only a warning, cleaned up next time"""

        def busy(path: Path) -> None:
            raise OSError("Device or resource busy")

        old = inode(project.venv_path)

        operations.replace_venv(project, remove_tree=busy)

        err = capsys.readouterr().err
        assert "The new environment is in place" in err
        assert "Device or resource busy" in err
        assert inode(project.venv_path) != old
        assert inode(project.venv_to_delete_path) == old
        assert find_problems(project) == ()

        assert invoke("sync", "--clean").exit_code == 0
        assert not project.venv_to_delete_path.exists()

    def test_leftovers_are_removed(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        """A killed sync leaves folders behind, one of them is a protected venv"""
        new, trash = project.venv_new_path, project.venv_to_delete_path
        (new / "lib").mkdir(parents=True)
        (new / "lib" / "module.py").touch()
        (trash / "lib").mkdir(parents=True)
        (trash / "lib" / "module.py").touch()
        for directory in (trash / "lib", trash):
            directory.chmod(0o555)

        result = invoke("sync", "--clean")

        assert result.exit_code == 0, result.output
        assert not new.exists()
        assert not trash.exists()

    def test_killed_between_the_renames(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        """No .venv, the old one is in .venv_to_delete: a new one is created"""
        project.venv_path.rename(project.venv_to_delete_path)

        result = invoke("sync", "--clean")

        assert result.exit_code == 0, result.output
        assert not project.venv_to_delete_path.exists()
        assert find_problems(project) == ()
        assert writable_paths(project.venv_path) == ()


class TestOptions:
    @pytest.mark.parametrize("flag", ["--inplace", "--no-inplace"])
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_needs_clean(
        self, invoke: Invoker, project: UVRevProject, flag: str, json_flag: list[str]
    ) -> None:
        """A usage error: exit code 2 and no report, like other wrong command lines"""
        old = inode(project.venv_path)

        result = invoke("sync", flag, *json_flag)

        assert result.exit_code == 2
        assert "can only be used with --clean" in result.output
        assert result.stdout == ""
        assert inode(project.venv_path) == old

    def test_help(self, invoke: Invoker) -> None:
        # the help is wrapped to the width of the terminal
        output = " ".join(invoke("sync", "--help").output.split())

        assert "--inplace" in output
        assert "twice as much free space" in output
        assert "--no-inplace" in output

    def test_not_inplace(self, invoke: Invoker, project: UVRevProject) -> None:
        """The venv is deleted first, uv creates the new one in the place"""
        result = invoke("sync", "--clean", "--no-inplace")

        assert result.exit_code == 0, result.output
        assert "uv venv" not in result.stderr
        assert ".venv_new" not in result.stderr
        assert find_problems(project) == ()
        assert not (project.project_path / ".venv_new").exists()

    def test_environment_is_not_changed_by_the_option(self) -> None:
        # uv gets UV_PROJECT_ENVIRONMENT only for the sync into the new venv
        assert "UV_PROJECT_ENVIRONMENT" not in os.environ
