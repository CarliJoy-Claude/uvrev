"""uvrev unprotect for a hotfix, and the repair by the next protecting command"""

import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any

import pytest
from conftest import Invoker, writable_paths

from uvrev.commands import git
from uvrev.exceptions import VenvChangedError
from uvrev.marker import Marker, read_marker, write_marker
from uvrev.protection import protect, unprotect, writable_venv
from uvrev.repair import (
    changed_since,
    mark_unprotected,
    repair,
)
from uvrev.settings import Settings
from uvrev.types import UVRevProject
from uvrev.verify import Problem, find_problems, verify

SAMPLE = "sampleproject"


def module(project: UVRevProject) -> Path:
    return project.site_packages[0] / "sample" / "simple.py"


def marker(project: UVRevProject) -> Marker | None:
    return read_marker(project.unprotected_marker_path)


def ctimes(project: UVRevProject) -> dict[Path, int]:
    venv = project.venv_path
    return {path: path.lstat().st_ctime_ns for path in (venv, *venv.rglob("*"))}


@pytest.fixture
def project(synced_project: UVRevProject) -> UVRevProject:
    """Protected environment with sampleproject and peppercorn installed"""
    assert marker(synced_project) is None
    return synced_project


@pytest.fixture
def unprotected(project: UVRevProject) -> UVRevProject:
    mark_unprotected(project)
    return project


class TestUnprotect:
    def test_marks_and_makes_writable(self, project: UVRevProject) -> None:
        changed, mark = mark_unprotected(project)

        assert changed
        assert mark == marker(project)
        assert mark.state == "post-chmod"
        assert writable_paths(project.venv_path) != ()
        # the time is taken after chmod: nothing counts as changed
        assert max(ctimes(project).values()) <= mark.stamp
        assert changed_since(project, mark.stamp) == ()
        # the marker is ignored by git
        status = git("status", "--porcelain", project=project, capture_output=True)
        assert status == ""

    def test_again_changes_nothing(self, unprotected: UVRevProject) -> None:
        """Neither the time nor the files, chmod would change their ctime"""
        mark = marker(unprotected)
        before = ctimes(unprotected)

        changed, again = mark_unprotected(unprotected)

        assert not changed
        assert again == mark == marker(unprotected)
        assert ctimes(unprotected) == before

    @pytest.mark.parametrize("content", ["", "5\npre-chmod\n", "garbage"])
    def test_undefined_becomes_forced(
        self, project: UVRevProject, content: str
    ) -> None:
        """An unprotect that did not finish, or a damaged marker"""
        project.unprotected_marker_path.write_text(content)

        changed, mark = mark_unprotected(project)

        assert changed
        assert mark.state == "forced"
        assert marker(project) == mark
        assert writable_paths(project.venv_path) != ()

        # forced is a defined state: nothing is done again
        assert mark_unprotected(project) == (False, mark)

    def test_hotfix_right_after_is_found(self, project: UVRevProject) -> None:
        """Also directly after unprotecting, see wait_for_clock"""
        for _ in range(20):
            _, mark = mark_unprotected(project)

            module(project).write_text("print('hotfix')")

            problems = changed_since(project, mark.stamp)
            assert problems == (Problem("modified", module(project), SAMPLE),)  # type: ignore[arg-type]
            assert repair(project) is not None

    def test_not_interrupted(self, project: UVRevProject) -> None:
        """Ctrl+C is ignored while the venv is made writable"""
        handled: list[int] = []
        previous = signal.signal(
            signal.SIGINT, lambda number, _: handled.append(number)
        )
        try:
            os.kill(os.getpid(), signal.SIGINT)
            assert handled == [signal.SIGINT]
            # afterwards the handler is active again
            mark_unprotected(project)
            assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_chmod_fails(self, project: UVRevProject) -> None:
        """If the venv can not be made writable, the state stays undefined"""
        broken = UVRevProject(project.project_path, Settings(unprotect_mode="bogus"))

        with pytest.raises(Exception, match="chmod"):
            mark_unprotected(broken)

        mark = marker(project)
        assert mark is not None
        assert mark.state == "pre-chmod"
        assert not mark.is_defined

    def test_writable_venv_keeps_marker(self, unprotected: UVRevProject) -> None:
        """Changes by uvrev itself neither create nor change the mark"""
        mark = marker(unprotected)

        with writable_venv(unprotected):
            pass

        assert marker(unprotected) == mark

    def test_writable_venv_creates_no_marker(self, project: UVRevProject) -> None:
        with writable_venv(project):
            pass
        unprotect(project)
        protect(project)

        assert marker(project) is None


class TestChangedSince:
    def test_modified(self, unprotected: UVRevProject) -> None:
        mark = marker(unprotected)
        assert mark is not None
        module(unprotected).write_text("print('hotfix')")

        problems = changed_since(unprotected, mark.stamp)

        assert [(p.kind, p.package) for p in problems] == [("modified", SAMPLE)]

    def test_mtime_set_back(self, unprotected: UVRevProject) -> None:
        """The ctime counts: it can not be set back like the mtime"""
        mark = marker(unprotected)
        assert mark is not None
        before = module(unprotected).stat()
        module(unprotected).write_text("print('hotfix')")
        os.utime(module(unprotected), ns=(before.st_atime_ns, before.st_mtime_ns))

        assert module(unprotected).stat().st_mtime_ns == before.st_mtime_ns
        assert len(changed_since(unprotected, mark.stamp)) == 1

    def test_replaced_file(self, unprotected: UVRevProject, tmp_path: Path) -> None:
        """Editors often write a new file and rename it"""
        mark = marker(unprotected)
        assert mark is not None
        new = tmp_path / "simple.py"
        new.write_text("print('hotfix')")
        shutil.move(new, module(unprotected))

        problems = changed_since(unprotected, mark.stamp)

        assert Problem("modified", module(unprotected), SAMPLE) in problems  # type: ignore[arg-type]

    def test_deleted_file_and_folder(self, unprotected: UVRevProject) -> None:
        mark = marker(unprotected)
        assert mark is not None
        module(unprotected).unlink()
        shutil.rmtree(unprotected.site_packages[0] / "peppercorn")

        problems = changed_since(unprotected, mark.stamp)

        assert {(p.kind, p.package) for p in problems} == {
            ("missing", SAMPLE),
            ("missing", "peppercorn"),
        }
        assert Problem("missing", module(unprotected), SAMPLE) in problems  # type: ignore[arg-type]

    def test_untracked_and_bytecode(self, unprotected: UVRevProject) -> None:
        mark = marker(unprotected)
        assert mark is not None
        site = unprotected.site_packages[0]
        extra = site / "sample" / "extra.py"
        extra.write_text("")
        script = unprotected.venv_path / "bin" / "my-script"
        script.write_text("")
        pycache = site / "sample" / "__pycache__"
        pycache.mkdir(exist_ok=True)
        # bytecode of an existing source is fine, without source it is not
        (pycache / "simple.cpython-399.pyc").write_bytes(b"")
        orphan = pycache / "gone.cpython-399.pyc"
        orphan.write_bytes(b"")

        problems = changed_since(unprotected, mark.stamp)

        assert set(problems) == {
            Problem("untracked", extra),
            Problem("untracked", script),
            Problem("untracked", orphan),
        }

    def test_volatile_venv_file(self, unprotected: UVRevProject) -> None:
        """uv creates and uses .venv/.lock, that is no change of the venv"""
        mark = marker(unprotected)
        assert mark is not None
        (unprotected.venv_path / ".lock").write_text("")

        assert changed_since(unprotected, mark.stamp) == ()

    @pytest.mark.parametrize("name", ["pyvenv.cfg", "bin/activate", "bin/python"])
    def test_venv_file_changed(self, unprotected: UVRevProject, name: str) -> None:
        """The files of the venv itself can not be restored"""
        mark = marker(unprotected)
        assert mark is not None
        path = unprotected.venv_path / name
        if path.is_symlink():
            target = path.readlink()
            path.unlink()
            path.symlink_to(target)
        else:
            path.write_text(path.read_text() + "\n# changed")

        with pytest.raises(VenvChangedError, match=r"uvrev sync --clean") as error:
            changed_since(unprotected, mark.stamp)

        assert error.value.path == unprotected.venv_path.resolve() / name

    @pytest.mark.parametrize("name", ["pyvenv.cfg", "bin/python"])
    def test_venv_file_missing(self, unprotected: UVRevProject, name: str) -> None:
        mark = marker(unprotected)
        assert mark is not None
        (unprotected.venv_path / name).unlink()

        with pytest.raises(VenvChangedError):
            changed_since(unprotected, mark.stamp)


class TestRepair:
    def test_not_marked(self, project: UVRevProject) -> None:
        assert repair(project) is None

    def test_fast(self, unprotected: UVRevProject) -> None:
        site = unprotected.site_packages[0]
        module(unprotected).write_text("print('hotfix')")
        (unprotected.venv_path / "bin" / "sample").unlink()
        extra = site / "extra.py"
        extra.write_text("")
        orphan = site / "sample" / "__pycache__" / "gone.cpython-399.pyc"
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"")

        result = repair(unprotected)

        assert result is not None
        assert result.mode == "fast"
        assert result.packages == (SAMPLE,)
        assert set(result.removed_files) == {extra, orphan}
        # valid, protected and no longer marked
        assert "hotfix" not in module(unprotected).read_text()
        assert find_problems(unprotected) == ()
        assert writable_paths(unprotected.venv_path) == ()
        assert marker(unprotected) is None

    def test_nothing_changed(self, unprotected: UVRevProject) -> None:
        result = repair(unprotected)

        assert result is not None
        assert (result.mode, result.packages, result.removed_files) == ("fast", (), ())
        assert writable_paths(unprotected.venv_path) == ()
        assert marker(unprotected) is None

    def test_bytecode_in_sync(self, unprotected: UVRevProject) -> None:
        """The bytecode is compiled again before the venv is read-only"""
        pycache = module(unprotected).parent / "__pycache__"
        compiled = list(pycache.glob("simple.*.pyc"))
        assert compiled, "uv compiles the bytecode on install"
        module(unprotected).write_text("print('hotfix')")
        for path in compiled:
            path.unlink()

        repair(unprotected)

        assert all(path.exists() for path in compiled)
        # up to date: the bytecode is newer than the restored source
        newest_source = module(unprotected).stat().st_mtime_ns
        assert all(path.stat().st_mtime_ns >= newest_source for path in compiled)

    def test_no_bytecode_if_disabled(self, project: UVRevProject) -> None:
        project = UVRevProject(project.project_path, Settings(compile_bytecode=False))
        mark_unprotected(project)
        pycache = module(project).parent / "__pycache__"
        shutil.rmtree(pycache)

        repair(project)

        assert not pycache.exists()

    def test_venv_file_changed_keeps_mark(self, unprotected: UVRevProject) -> None:
        mark = marker(unprotected)
        config = unprotected.venv_path / "pyvenv.cfg"
        config.write_text(config.read_text() + "\n# changed")

        with pytest.raises(VenvChangedError):
            repair(unprotected)

        assert marker(unprotected) == mark

    @pytest.mark.parametrize("state", ["forced", "pre-chmod", "undefined"])
    def test_full(
        self,
        unprotected: UVRevProject,
        state: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without a reliable time every file is compared with its hash"""
        # a change BEFORE the stamp: the fast repair would not see it
        module(unprotected).write_text("print('hotfix')")
        extra = unprotected.site_packages[0] / "extra.py"
        extra.write_text("")
        path = unprotected.unprotected_marker_path
        if state == "undefined":
            path.chmod(0o644)
            path.write_text("garbage")
        else:
            newest = max(ctimes(unprotected).values())
            write_marker(path, Marker(state, newest + 10**9))  # type: ignore[arg-type]

        result = repair(unprotected)

        assert result is not None
        assert result.mode == "full"
        assert result.packages == (SAMPLE,)
        assert result.removed_files == (extra,)
        assert "hotfix" not in module(unprotected).read_text()
        assert marker(unprotected) is None
        assert writable_paths(unprotected.venv_path) == ()
        assert "uvrev sync --clean" in capsys.readouterr().err


class TestCli:
    def test_unprotect_and_protect(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        result = invoke("unprotect")

        assert result.exit_code == 0
        assert "is writable" in result.stderr
        assert "chmod -R u+w .venv" in result.stderr
        # only one environment: no warning about shared files
        assert "other environments" not in result.stderr

        result = invoke("unprotect")

        assert "is already unprotected" in result.stderr
        assert "chmod" not in result.stderr

        module(project).write_text("print('hotfix')")
        result = invoke("protect")

        assert result.exit_code == 0
        assert "1 package(s) reinstalled (sampleproject), 0 file(s) removed" in (
            result.stderr
        )
        assert marker(project) is None

    def test_forced_warns(self, invoke: Invoker, project: UVRevProject) -> None:
        project.unprotected_marker_path.write_text("")

        result = invoke("unprotect", "--json")

        assert result.exit_code == 0
        assert "not known since when the venv is writable" in result.stderr
        assert json.loads(result.stdout)["state"] == "forced"

        result = invoke("protect", "--json")

        assert json.loads(result.stdout)["repair"]["mode"] == "full"
        assert "uvrev sync --clean" in result.stderr

    def test_other_environments_warning(
        self, invoke: Invoker, project: UVRevProject, fake_basedir: Path
    ) -> None:
        assert invoke("env", "create", "other").exit_code == 0

        result = invoke("unprotect")

        assert "There are other environments: other." in result.stderr
        assert "uvrev verify --fix" in result.stderr

    def test_change_repairs_first(self, invoke: Invoker, project: UVRevProject) -> None:
        """A command that changes the environment repairs it first"""
        invoke("unprotect")
        module(project).write_text("print('hotfix')")

        result = invoke("add", "six", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["repair"] == {
            "mode": "fast",
            "packages": [SAMPLE],
            "removed_files": [],
        }
        assert report["all_changed_dependencies"] == ["six"]
        # repaired before the packages were added
        assert result.stderr.index("Repairing") < result.stderr.index("uv add")
        assert "hotfix" not in module(project).read_text()
        assert marker(project) is None
        assert writable_paths(project.venv_path) == ()

    def test_not_marked_no_repair(self, invoke: Invoker, project: UVRevProject) -> None:
        report = json.loads(invoke("sync", "--json").stdout)

        assert report["repair"] is None

    def test_dry_run_does_not_repair(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        invoke("unprotect")
        mark = marker(project)
        module(project).write_text("print('hotfix')")

        result = invoke("add", "six", "--dry-run", "--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["repair"] is None
        assert "hotfix" in module(project).read_text()
        assert marker(project) == mark

    def test_run_keeps_hotfix(self, invoke: Invoker, project: UVRevProject) -> None:
        """uvrev run neither repairs nor syncs nor protects"""
        invoke("unprotect")
        mark = marker(project)
        module(project).write_text("print('hotfix')")
        # not in sync with uv.lock: uvrev run would sync a protected environment
        shutil.rmtree(project.site_packages[0] / "peppercorn")

        result = invoke("run", "python", "-c", "import sample")

        assert result.exit_code == 0
        assert "uv sync" not in result.stderr
        assert "chmod" not in result.stderr
        assert "uv run --no-sync python" in result.stderr
        assert "hotfix" in module(project).read_text()
        assert marker(project) == mark

    def test_repair_fails(self, invoke: Invoker, project: UVRevProject) -> None:
        """The environment stays marked and the command fails"""
        invoke("unprotect")
        mark = marker(project)
        config = project.venv_path / "pyvenv.cfg"
        config.write_text(config.read_text() + "\n# changed")

        result = invoke("sync", "--json")

        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["status"] == "error"
        assert "uvrev sync --clean" in report["error_message"]
        assert marker(project) == mark

    def test_verify_fix_removes_mark(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        invoke("unprotect")
        module(project).write_text("print('hotfix')")

        assert invoke("verify", "--fix").exit_code == 0

        assert marker(project) is None
        assert writable_paths(project.venv_path) == ()

    def test_verify_fix_keeps_mark_if_problems_remain(
        self, unprotected: UVRevProject
    ) -> None:
        mark = marker(unprotected)

        def unfixable(project: UVRevProject) -> tuple[Problem, ...]:
            return (Problem("modified", Path("broken.py")),)

        result = verify(unprotected, fix=True, find=unfixable)

        assert result.remaining
        assert marker(unprotected) == mark

    def test_verify_keeps_mark(self, invoke: Invoker, project: UVRevProject) -> None:
        """Only checking changes nothing"""
        invoke("unprotect")
        mark = marker(project)

        assert invoke("verify").exit_code == 0

        assert marker(project) == mark


class TestSyncClean:
    @pytest.fixture(autouse=True)
    def private_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """--clean deletes the complete uv cache: never the one of the developer"""
        cache = tmp_path / "uv-cache"
        monkeypatch.setenv("UV_CACHE_DIR", str(cache))
        return cache

    def test_clean(
        self, invoke: Invoker, project: UVRevProject, private_cache: Path
    ) -> None:
        # the way out of a state that can not be repaired
        invoke("unprotect")
        config = project.venv_path / "pyvenv.cfg"
        original = config.read_text()
        config.write_text(original + "\n# changed")
        module(project).write_text("print('hotfix')")
        extra = project.site_packages[0] / "extra.py"
        extra.write_text("")
        (private_cache / "left-over").mkdir(parents=True, exist_ok=True)

        result = invoke("sync", "--clean", "--json")

        assert result.exit_code == 0, result.output
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["options"] == {"clean": True}
        assert report["repair"] is None
        # the versions are the same, so no package changed
        assert report["all_changed_packages"] == []
        assert "uv cache clean --force\n" in result.stderr
        assert "Repairing" not in result.stderr
        assert not (private_cache / "left-over").exists()
        # the venv is new, valid, protected and not marked
        assert "changed" not in config.read_text()
        assert not extra.exists()
        assert find_problems(project) == ()
        assert writable_paths(project.venv_path) == ()
        assert marker(project) is None

    def test_clean_without_venv(self, invoke: Invoker, project: UVRevProject) -> None:
        unprotect(project)
        shutil.rmtree(project.venv_path)

        result = invoke("sync", "--clean")

        assert result.exit_code == 0, result.output
        assert find_problems(project) == ()

    def test_sync_option_in_report(
        self, invoke: Invoker, project: UVRevProject
    ) -> None:
        report = json.loads(invoke("sync", "--json").stdout)

        assert report["options"] == {"clean": False}
