import json
import shutil
from pathlib import Path

import pytest
from conftest import Invoker, writable_paths
from packaging.utils import NormalizedName

import uvrev.cli
from uvrev.protection import unprotect
from uvrev.types import UVRevProject
from uvrev.verify import Problem, find_problems, verify

SAMPLE = NormalizedName("sampleproject")


def module(project: UVRevProject) -> Path:
    return project.site_packages[0] / "sample" / "simple.py"


def kinds(problems: tuple[Problem, ...]) -> set[tuple[str, str | None]]:
    return {(problem.kind, problem.package) for problem in problems}


class TestFindProblems:
    def test_fine(self, synced_project: UVRevProject) -> None:
        assert find_problems(synced_project) == ()

    def test_modified(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).write_text("print('changed')")

        problems = find_problems(synced_project)

        assert problems == (Problem("modified", module(synced_project), SAMPLE),)

    def test_same_size_modified(self, synced_project: UVRevProject) -> None:
        """A change keeping the size is found by the hash"""
        path = module(synced_project)
        content = path.read_bytes()
        unprotect(synced_project)
        path.write_bytes(content.swapcase())

        assert ("modified", "sampleproject") in kinds(find_problems(synced_project))

    def test_missing(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).unlink()

        problems = find_problems(synced_project)

        assert Problem("missing", module(synced_project), SAMPLE) in problems

    def test_missing_script(self, synced_project: UVRevProject) -> None:
        """Entries outside of site-packages like ../../../bin/sample are checked"""
        unprotect(synced_project)
        (synced_project.venv_path / "bin" / "sample").unlink()

        problems = find_problems(synced_project)

        script = synced_project.site_packages[0].parents[2] / "bin" / "sample"
        assert Problem("missing", script, SAMPLE) in problems

    def test_untracked(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        extra = synced_project.site_packages[0] / "sample" / "extra.py"
        extra.write_text("")
        # bytecode is not part of RECORD and ignored
        pycache = synced_project.site_packages[0] / "sample" / "__pycache__"
        pycache.mkdir(exist_ok=True)
        (pycache / "extra.cpython-312.pyc").write_bytes(b"")

        problems = find_problems(synced_project)

        assert kinds(problems) == {("untracked", None)}
        assert Problem("untracked", extra) in problems

    def test_not_synced(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        for path in synced_project.site_packages[0].glob("peppercorn*"):
            shutil.rmtree(path)

        problems = find_problems(synced_project)

        assert Problem("not_synced", synced_project.lock_path) in problems


class TestVerifyFix:
    def test_fix(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).write_text("print('changed')")
        (synced_project.venv_path / "bin" / "sample").unlink()
        for path in synced_project.site_packages[0].glob("peppercorn*"):
            shutil.rmtree(path)
        extra = synced_project.site_packages[0] / "extra.py"
        extra.write_text("")

        problems = verify(synced_project, fix=True)

        # only the untracked file is left, it may be someone's work
        assert problems == (Problem("untracked", extra),)
        assert "changed" not in module(synced_project).read_text()
        assert writable_paths(synced_project.venv_path) == ()

    def test_nothing_to_fix(self, synced_project: UVRevProject) -> None:
        """--fix protects the venv again in any case"""
        unprotect(synced_project)

        assert verify(synced_project, fix=True) == ()
        assert writable_paths(synced_project.venv_path) == ()


class TestCli:
    def test_fine(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        result = invoke("verify")

        assert result.exit_code == 0
        assert "Environment is fine" in result.output

    def test_problems(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).unlink()

        result = invoke("verify")

        assert result.exit_code == 1
        assert "simple.py (sampleproject): missing" in result.output
        assert "uvrev verify --fix" in result.output

    def test_warning_only(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        """Untracked files are reported, but are no error"""
        unprotect(synced_project)
        (synced_project.site_packages[0] / "extra.py").write_text("")
        invoke("protect")

        result = invoke("verify")

        assert result.exit_code == 0
        assert "extra.py: not installed by any package" in result.output

    def test_fix(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).unlink()

        result = invoke("verify", "--fix")

        assert result.exit_code == 0
        assert "--reinstall-package=sampleproject" in result.output
        assert module(synced_project).exists()

    def test_fix_fails(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        remaining = (Problem("modified", Path("broken.py"), None),)
        monkeypatch.setattr(uvrev.cli, "verify", lambda *args, **kwargs: remaining)

        result = invoke("verify", "--fix")

        assert result.exit_code == 1
        assert "broken.py: modified" in result.output
        assert "1 problem(s) could not be fixed" in result.output


class TestVerifyAfterChange:
    def test_option(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).write_text("print('changed')")

        result = invoke("sync", "--verify", "--json")

        assert result.exit_code == 0
        # stdout only contains the report
        assert json.loads(result.stdout)["status"] == "success"
        assert "--reinstall-package=sampleproject" in result.stderr
        assert "changed" not in module(synced_project).read_text()

    def test_setting(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("UVREV_VERIFY_AFTER_CHANGE", "true")
        unprotect(synced_project)
        module(synced_project).unlink()

        result = invoke("sync")

        assert result.exit_code == 0
        assert module(synced_project).exists()

    def test_disabled(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-verify wins over the setting"""
        monkeypatch.setenv("UVREV_VERIFY_AFTER_CHANGE", "true")
        unprotect(synced_project)
        module(synced_project).unlink()

        result = invoke("sync", "--no-verify")

        assert result.exit_code == 0
        assert not module(synced_project).exists()

    def test_fails(
        self,
        invoke: Invoker,
        synced_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Problems that remain fail the command, with a JSON error report"""
        remaining = (Problem("modified", Path("broken.py"), None),)
        monkeypatch.setattr(uvrev.cli, "verify", lambda *args, **kwargs: remaining)

        result = invoke("sync", "--verify", "--json")

        assert result.exit_code == 1
        report = json.loads(result.stdout)
        assert report["status"] == "error"
        assert report["error_message"] == "1 problem(s) could not be fixed"
