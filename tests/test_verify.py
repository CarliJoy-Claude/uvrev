import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from conftest import Invoker, writable_paths
from packaging.utils import NormalizedName

from uvrev import operations
from uvrev.cli import cli
from uvrev.protection import unprotect
from uvrev.reporting import Services
from uvrev.settings import load_settings
from uvrev.types import UVRevProject
from uvrev.verify import Problem, VerifyResult, find_problems, verify

SAMPLE = NormalizedName("sampleproject")


def invoke_unfixable(*args: str) -> Result:
    """Invoke the cli with a verify that always leaves a problem"""

    def unfixable(project: UVRevProject, fix: bool) -> VerifyResult:
        problems = (Problem("modified", Path("broken.py"), None),)
        return VerifyResult(problems, problems)

    services = Services(load_settings(), verify=unfixable)
    return CliRunner().invoke(cli, args=args, obj=services)


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
        # bytecode is not part of RECORD: allowed if its source exists
        pycache = synced_project.site_packages[0] / "sample" / "__pycache__"
        pycache.mkdir(exist_ok=True)
        (pycache / "extra.cpython-312.pyc").write_bytes(b"")
        orphan = pycache / "gone.cpython-312.pyc"
        orphan.write_bytes(b"")
        # the whole venv is checked, not only site-packages
        script = synced_project.venv_path / "bin" / "my-script"
        script.write_text("")

        problems = find_problems(synced_project)

        assert set(problems) == {
            Problem("untracked", extra),
            Problem("untracked", orphan),
            Problem("untracked", script),
        }

    def test_not_synced(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        for path in synced_project.site_packages[0].glob("peppercorn*"):
            shutil.rmtree(path)

        problems = find_problems(synced_project)

        assert Problem("not_synced", synced_project.lock_path) in problems


@pytest.fixture
def hardlinked_project(
    sample_project: UVRevProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> UVRevProject:
    """The sample project installed with uv's default hardlinks from its cache

    A private cache is used, as the tests poison it on purpose.
    """
    monkeypatch.setenv("UV_LINK_MODE", "hardlink")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    operations.sync_packages(sample_project)
    assert module(sample_project).stat().st_nlink > 1, "not hardlinked"
    return sample_project


class TestPoisonedCache:
    """A file changed in the venv changes the hardlinked file in the uv cache

    See https://github.com/astral-sh/uv/issues/22979
    """

    def test_fix(self, hardlinked_project: UVRevProject) -> None:
        unprotect(hardlinked_project)
        module(hardlinked_project).write_text("print('poisoned')")

        result = verify(hardlinked_project, fix=True)

        assert result.remaining == ()
        assert result.fixed
        assert "poisoned" not in module(hardlinked_project).read_text()


class TestVerifyFix:
    def test_fix(self, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).write_text("print('changed')")
        (synced_project.venv_path / "bin" / "sample").unlink()
        for path in synced_project.site_packages[0].glob("peppercorn*"):
            shutil.rmtree(path)
        extra = synced_project.site_packages[0] / "extra.py"
        extra.write_text("")

        result = verify(synced_project, fix=True)

        # the venv is valid again, the untracked file is removed
        assert result.remaining == ()
        assert not extra.exists()
        assert result.fixed
        assert Problem("modified", module(synced_project), SAMPLE) in result.problems
        assert "changed" not in module(synced_project).read_text()
        assert writable_paths(synced_project.venv_path) == ()

    def test_nothing_to_fix(self, synced_project: UVRevProject) -> None:
        """--fix protects the venv again in any case"""
        unprotect(synced_project)

        assert verify(synced_project, fix=True) == VerifyResult()
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

    def test_untracked(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        """A file that no package installed makes the venv invalid, --fix removes it"""
        unprotect(synced_project)
        extra = synced_project.site_packages[0] / "extra.py"
        extra.write_text("")

        result = invoke("verify")

        assert result.exit_code == 1
        assert "extra.py: not installed by any package" in result.output

        assert invoke("verify", "--fix").exit_code == 0
        assert not extra.exists()
        assert writable_paths(synced_project.venv_path) == ()

    def test_fix(self, invoke: Invoker, synced_project: UVRevProject) -> None:
        unprotect(synced_project)
        module(synced_project).unlink()

        result = invoke("verify", "--fix")

        assert result.exit_code == 0
        # the cache entry might be poisoned as well, it is removed first
        assert "uv cache clean --force sampleproject" in result.output
        assert "--reinstall-package=sampleproject" in result.output
        assert module(synced_project).exists()

    def test_fix_fails(self, synced_project: UVRevProject) -> None:
        result = invoke_unfixable("verify", "--fix")

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
        report = json.loads(result.stdout)
        assert report["status"] == "success"
        assert "--reinstall-package=sampleproject" in result.stderr
        assert "changed" not in module(synced_project).read_text()
        # the report shows what verify found and repaired
        assert report["verify"] == {
            "problems": [
                {
                    "kind": "modified",
                    "path": str(module(synced_project)),
                    "package": "sampleproject",
                    "is_warning": False,
                }
            ],
            "remaining_problems": [],
            "fixed": True,
        }

    def test_nothing_to_fix(
        self, invoke: Invoker, synced_project: UVRevProject
    ) -> None:
        report = json.loads(invoke("sync", "--verify", "--json").stdout)

        assert report["verify"] == {
            "problems": [],
            "remaining_problems": [],
            "fixed": False,
        }

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

        result = invoke("sync", "--no-verify", "--json")

        assert result.exit_code == 0
        assert not module(synced_project).exists()
        # null: verify did not run
        assert json.loads(result.stdout)["verify"] is None

    def test_fails(self, synced_project: UVRevProject) -> None:
        """Problems that remain fail the command, the report still shows the change"""
        result = invoke_unfixable("add", "peppercorn", "--verify", "--json")

        assert result.exit_code == 1
        assert "1 problem(s) could not be fixed" in result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == "error"
        assert report["error_message"] == "1 problem(s) could not be fixed"
        # the revision was created before verifying
        assert (report["previous_revision"], report["revision"]) == (1, 2)
        assert report["all_changed_dependencies"] == ["peppercorn"]
        assert report["verify"]["fixed"] is False
        assert [p["path"] for p in report["verify"]["remaining_problems"]] == [
            "broken.py"
        ]
