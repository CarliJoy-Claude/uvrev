import json
import platform
import sys
from importlib.metadata import version
from itertools import chain
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from conftest import Invoker, writable_paths
from packaging.utils import canonicalize_name
from packaging.version import Version

from uvrev import constants, environment
from uvrev.commands import git
from uvrev.io import read_pyproject
from uvrev.parser import parse_uv_lock_packages
from uvrev.revisions import show_history
from uvrev.settings import Settings
from uvrev.types import UVRevProject


def create_fake_env(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    proj = UVRevProject(path, Settings())
    proj.lock_path.touch()
    proj.pyproject_path.write_text("[tool.uv]\npackage = false\n")


@pytest.fixture(scope="module")
def shared_basedir_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    A shared base directory for integration tests that depend on each others state

    USE **ONLY** FOR INTEGRATION TESTS

    Normally tests should be independent of other tests
    """
    return tmp_path_factory.mktemp("shared_basedir")


@pytest.fixture
def shared_basedir(
    shared_basedir_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """
    A shared base directory for integration tests that depend on each others state

    USE **ONLY** FOR INTEGRATION TESTS

    Normally tests should be independent of other tests
    """
    monkeypatch.setenv("UVREV_BASE", str(shared_basedir_folder))
    return shared_basedir_folder


@pytest.fixture
def shared_project(shared_basedir: Path) -> UVRevProject:
    """
    Shared project for integration tests
    """
    return UVRevProject(shared_basedir / constants.DEFAULT_ENV, Settings())


@pytest.fixture(autouse=True)
def tmp_basedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Ensure only tmp basedir are used
    """
    base_dir = tmp_path / "tmp_basedir"
    monkeypatch.setenv("UVREV_BASE", str(base_dir))
    return base_dir


def parse_report(result: Result) -> dict[str, Any]:
    """Parse the JSON report, ensuring nothing else was written to stdout"""
    report: dict[str, Any] = json.loads(result.stdout)
    assert report["schema_version"] == 2
    # the flat list is the union of all detailed changes
    for changes, all_changed in (
        ("changes", "all_changed_packages"),
        ("dependency_changes", "all_changed_dependencies"),
    ):
        assert (
            sorted(chain.from_iterable(report[changes].values()))
            == (report[all_changed])
        )
    return report


NO_CHANGES: dict[str, list[str]] = {
    "added": [],
    "updated": [],
    "downgraded": [],
    "removed": [],
}


class TestCLIDefinition:
    """Tests for CLI commands."""

    # Commands require arguments
    @pytest.mark.parametrize(
        "command",
        [
            ["add"],
            ["remove"],
            ["restore"],
            ["run"],
        ],
    )
    def test_requires_arguments(self, invoke: Invoker, command: list[str]) -> None:
        """Commands require arguments."""
        result = invoke(*command)

        assert result.exit_code != 0

    # Env list in empty directory
    def test_env_list_empty(
        self, invoke: Invoker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env list shows message when empty."""
        result = invoke("env", "list")

        assert result.exit_code == 0
        assert "No projects found" in result.output

    # Help text is available
    @pytest.mark.parametrize(
        "command",
        [
            ["--help"],
            ["add", "--help"],
            ["remove", "--help"],
            ["sync", "--help"],
            ["upgrade", "--help"],
            ["restore", "--help"],
            ["env", "--help"],
            ["env", "create", "--help"],
        ],
        ids=" ".join,
    )
    def test_help_available(self, invoke: Invoker, command: list[str]) -> None:
        """Help text is available."""
        result = invoke(*command)

        assert result.exit_code == 0
        assert "Usage:" in result.output or "Show this message" in result.output


class TestEnvCreate:
    """uvrev env create"""

    def test_base_env_already_exists(self, invoke: Invoker, fake_basedir: Path) -> None:
        fake_basedir.joinpath(constants.DEFAULT_ENV).mkdir(parents=True)
        result = invoke("env", "create")
        assert result.exit_code == 1
        assert constants.DEFAULT_ENV in result.output
        assert "already exists" in result.output

    def test_named_env_already_exists(
        self, invoke: Invoker, fake_basedir: Path
    ) -> None:
        fake_basedir.joinpath("foobar").mkdir(parents=True)
        result = invoke("env", "create", "foobar")
        assert result.exit_code == 1
        assert "foobar" in result.output
        assert "already exists" in result.output

    @pytest.mark.parametrize(
        ("cmd", "folder"),
        [
            pytest.param(
                ["env", "create"],
                constants.DEFAULT_ENV,
                marks=pytest.mark.dependency(name="0-create-default"),
            ),
            pytest.param(
                ["env", "create", "foobar"],
                "foobar",
                marks=pytest.mark.dependency(name="0-create-foobar"),
            ),
        ],
    )
    def test_env_create(
        self, invoke: Invoker, cmd: list[str], folder: str, shared_basedir: Path
    ) -> None:
        result = invoke(*cmd)
        assert result.exit_code == 0
        target = shared_basedir / folder
        proj = UVRevProject(target, Settings())
        assert proj.exists()

        # Output tells the user what where exactly thing were created
        assert f"created at {target}" in result.output

        # pyproject.toml has been modified
        pyproj: Any = read_pyproject(proj)
        assert pyproj["project"]["version"] == "0"
        assert pyproj["tool"]["uv"]["package"] is False

        # correct name has been used, otherwise there could be collision with pkgs on pypi
        assert pyproj["project"]["name"] == f"uvrev-venv-{folder}"

        # There is only one commit which is tagged
        commits = git(
            "log", "--all", "--format=%s", project=proj, capture_output=True
        ).splitlines(keepends=False)
        tags = git(
            "log", "--tags", "--format=%s", project=proj, capture_output=True
        ).splitlines(keepends=False)
        assert commits == tags == [constants.INITIAL_COMMIT_MSG]

        # folder contains all wanted files but no more
        accepted_files = {
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
            ".python-version",
            ".python-version.lock",
            constants.CREATED_MARKER_FILE,
            ".git",
            ".venv",
        }
        created_files = {f.name for f in target.iterdir()}
        assert created_files == accepted_files

        # the venv is protected
        assert writable_paths(proj.venv_path) == ()


class TestEnvCreateIdempotent:
    """uvrev env create on existing environments and uvrev env fix"""

    def test_create_again(self, invoke: Invoker, fake_basedir: Path) -> None:
        assert invoke("env", "create").exit_code == 0
        proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
        head = git("rev-parse", "HEAD", project=proj, capture_output=True)

        result = invoke("env", "create", "--python", "3.99")

        # nothing is done, the Python version is not checked
        assert result.exit_code == 0, result.output
        assert "already exists" in result.output
        assert git("rev-parse", "HEAD", project=proj, capture_output=True) == head
        # also the function itself is idempotent
        assert (
            environment.create_env(fake_basedir, constants.DEFAULT_ENV, Settings())
            == proj
        )

    def test_created_marker(self, invoke: Invoker, fake_basedir: Path) -> None:
        assert invoke("env", "create").exit_code == 0
        proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())

        assert proj.created_marker_path.read_text().strip() == version("uvrev")
        # the marker is ignored by git
        status = git("status", "--porcelain", project=proj, capture_output=True)
        assert status == ""
        # the initial revision is an annotated tag as well
        tag_type = git("cat-file", "-t", "rev_0", project=proj, capture_output=True)
        assert tag_type.strip() == "tag"

    def test_force(self, invoke: Invoker, fake_basedir: Path) -> None:
        """--force deletes environments that were not created successfully"""
        target = fake_basedir / constants.DEFAULT_ENV
        create_fake_env(target)
        target.joinpath("leftover").touch()

        result = invoke("env", "create")
        assert result.exit_code == 1
        assert "uvrev env fix" in result.output
        assert "--force" in result.output

        result = invoke("env", "create", "--force")
        assert result.exit_code == 0, result.output
        assert not target.joinpath("leftover").exists()
        assert UVRevProject(target, Settings()).created_successfully()

    def test_warning_and_fix(self, invoke: Invoker, fake_basedir: Path) -> None:
        target = fake_basedir / "old"
        create_fake_env(target)

        result = invoke("path", "-e", "old")
        assert result.exit_code == 0
        assert "was not created successfully" in result.output
        assert "uvrev env fix old" in result.output
        assert "uvrev env fix old" in invoke("env", "list").output

        # .git/info/exclude is created if needed
        result = invoke("env", "fix", "old")
        assert result.exit_code == 0
        assert "is not a git repository" in result.output
        assert UVRevProject(target, Settings()).created_successfully()
        exclude = target / ".git" / "info" / "exclude"
        ignored = (
            f"{constants.CREATED_MARKER_FILE}\n{constants.UNPROTECTED_MARKER_FILE}\n"
        )
        assert exclude.read_text() == ignored

        # existing ignores are kept and the marker is added only once
        exclude.write_text("*.log")
        assert invoke("env", "fix", "old").exit_code == 0
        assert invoke("env", "fix", "old").exit_code == 0
        assert exclude.read_text() == f"*.log\n{ignored}"

        result = invoke("path", "-e", "old")
        assert "was not created successfully" not in result.output
        assert "uvrev env fix" not in invoke("env", "list").output

    def test_fix_missing(self, invoke: Invoker, fake_basedir: Path) -> None:
        result = invoke("env", "fix", "missing")
        assert result.exit_code == 1
        assert "does not exist" in result.output


class TestEnvironments:
    """tool.uv.environments"""

    def environments(self, proj: UVRevProject) -> list[str]:
        uv_table: Any = read_pyproject(proj)["tool"]["uv"]
        return list(uv_table.get("environments", []))

    def test_created_with_installed(self, invoke: Invoker, fake_basedir: Path) -> None:
        """Without setting, the installed versions matching requires-python"""
        assert invoke("env", "create").exit_code == 0
        proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
        pinned = proj.python_version_path.read_text().strip()

        markers = self.environments(proj)
        assert markers
        assert any(f"python_version == '{pinned}' and" in m for m in markers)
        assert all(
            f"sys_platform == '{sys.platform}'" in m
            and f"platform_machine == '{platform.machine()}'" in m
            for m in markers
        )

    def test_setting(
        self,
        invoke: Invoker,
        fake_basedir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert invoke("env", "create").exit_code == 0
        proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV, Settings())
        pinned = proj.python_version_path.read_text().strip()

        # no supported version (3.0 is below requires-python):
        # tool.uv.environments is removed
        monkeypatch.setenv(constants.PYTHON_VERSIONS_SUPPORTED_ENV, "3.0")
        report = parse_report(invoke("upgrade", "--json"))
        assert report["revision"] == 1
        assert report["environments"]["new"] == []
        assert self.environments(proj) == []

        # versions below requires-python are not used
        monkeypatch.setenv(constants.PYTHON_VERSIONS_SUPPORTED_ENV, f"3.0, {pinned}")
        result = invoke("add", "peppercorn", "--json")

        assert result.exit_code == 0, result.output
        report = parse_report(result)
        assert report["revision"] == 2
        assert report["environments"] == {
            "old": [],
            "new": list(environment.environment_markers((pinned,))),
        }
        assert self.environments(proj) == report["environments"]["new"]
        message = git("log", "-1", "--format=%B", project=proj, capture_output=True)
        assert "Environments:" in message

        # nothing to update anymore
        report = parse_report(invoke("remove", "peppercorn", "--json"))
        assert report["revision"] == 3
        assert report["environments"] is None

        # sync and restore don't update the environments
        assert invoke("sync").exit_code == 0
        assert invoke("restore", "1").exit_code == 0
        assert self.environments(proj) == []

    def test_invalid_setting(
        self,
        invoke: Invoker,
        upgradeable_project: UVRevProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(constants.PYTHON_VERSIONS_SUPPORTED_ENV, "3.12,abc")

        result = invoke("upgrade")

        assert result.exit_code == 1
        assert constants.PYTHON_VERSIONS_SUPPORTED_ENV in result.output


class TestEnvList:
    """uvrev env list"""

    def test_env_list_empty(self, invoke: Invoker, fake_basedir: Path) -> None:
        """
        Tell user if no projects are found.
        """
        result = invoke("env", "list")

        assert result.exit_code == 0
        assert "No projects found" in result.output

    def test_envs_found(self, invoke: Invoker, fake_basedir: Path) -> None:
        create_fake_env(fake_basedir / "foo")
        create_fake_env(fake_basedir / "bar")
        create_fake_env(fake_basedir / "bar_blub")

        result = invoke("env", "list")
        assert result.exit_code == 0
        assert "foo" in result.output
        assert "bar" in result.output
        assert "bar_blub" in result.output

    @pytest.mark.dependency(
        name="env-list", depends=["0-create-default", "0-create-foobar"]
    )
    def test_env_list_integration(self, invoke: Invoker, shared_basedir: Path) -> None:
        """
        The env created by `uvrev env create` are actually listed
        """

        assert shared_basedir.exists()

        result = invoke("env", "list")
        assert result.exit_code == 0
        assert constants.DEFAULT_ENV in result.output
        assert "foobar" in result.output


class TestPath:
    """uvrev path"""

    def test_manually_created(self, fake_basedir: Path, invoke: Invoker) -> None:
        """The path of the selected env shall be return"""
        target = fake_basedir / "foo"
        create_fake_env(target)

        result = invoke("path", "-e", "foo")

        assert result.exit_code == 0
        assert str(target) in result.output

    @pytest.mark.dependency(name="show-path", depends=["0-create-default"])
    def test_created_by_subcommand(self, invoke: Invoker, shared_basedir: Path) -> None:
        """Also the path of the env created with uvenv create shall be returned"""
        target = shared_basedir / constants.DEFAULT_ENV

        result = invoke("path")

        assert result.exit_code == 0
        assert str(target) in result.output


class TestSync:
    def test_sample(self, sample_project: UVRevProject, invoke: Invoker) -> None:
        target = sample_project.project_path

        # find simple file for all python versions on all platforms
        sample_exists = any(
            (sp / "sample" / "simple.py").is_file()
            for lib in ("lib", "lib64", "Lib")
            for sp in target.joinpath(".venv", lib).glob("python*/site-packages")
        )
        assert sample_exists is False

        result = invoke("sync")
        assert result.exit_code == 0
        assert "uv sync --frozen" in result.output
        assert "Sync complete" in result.output

        sample_exists = any(
            (sp / "sample" / "simple.py").is_file()
            for lib in ("lib", "lib64", "Lib")
            for sp in target.joinpath(".venv", lib).glob("python*/site-packages")
        )
        assert sample_exists is True

    def test_json(self, sample_project: UVRevProject, invoke: Invoker) -> None:
        """The changes of the installed packages are reported"""
        result = invoke("sync", "--json")

        assert result.exit_code == 0
        # logs are still shown, but on stderr
        assert "uv sync --frozen" in result.stderr
        report = parse_report(result)
        assert report["status"] == "success"
        assert report["command"] == "sync"
        assert report["arguments"] == []
        assert report["revision"] == report["previous_revision"] == 1
        assert report["changes"] == {
            "added": ["peppercorn", "sampleproject"],
            "updated": [],
            "downgraded": [],
            "removed": [],
        }
        assert report["all_changed_packages"] == ["peppercorn", "sampleproject"]
        assert report["versions"]["packages"] == {
            "peppercorn": {"old": None, "new": "0.6"},
            "sampleproject": {"old": None, "new": "4.0.0"},
        }
        assert report["dependency_changes"] == NO_CHANGES
        assert report["error_message"] is None

        # Syncing again does not change anything
        result = invoke("sync", "--json")

        assert result.exit_code == 0
        report = parse_report(result)
        assert report["revision"] == 1
        assert report["changes"] == NO_CHANGES
        assert report["all_changed_packages"] == []

    def test_json_error(self, fake_basedir: Path, invoke: Invoker) -> None:
        """A failure is reported as JSON, even if no revision can be determined"""
        create_fake_env(fake_basedir / constants.DEFAULT_ENV)

        result = invoke("sync", "--json")

        assert result.exit_code == 1
        report = parse_report(result)
        assert report["status"] == "error"
        assert report["revision"] is None
        assert "uv sync --frozen" in report["error_message"]
        assert report["all_changed_packages"] == []


class TestUpgrade:
    def test_upgrade_all(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """
        Ensure all package have been upgrade
        """
        sample_pkg = canonicalize_name("sampleproject")
        sample_dep = canonicalize_name("peppercorn")
        self_pkg = canonicalize_name(f"uvrev-venv-{constants.DEFAULT_ENV}")

        deps_before = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # check the lock file is not yet updated
        assert deps_before == {
            sample_pkg: Version("3.0.0"),
            sample_dep: Version("0.5"),
            self_pkg: Version("1"),
        }

        result = invoke("upgrade")

        assert result.exit_code == 0
        assert "Upgraded all packages" in result.output
        assert "created revision 2" in result.output

        deps_after = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # everything is actually updated
        assert deps_after[sample_pkg] > deps_before[sample_pkg]
        assert deps_after[sample_dep] > deps_before[sample_dep]
        # but the virtual package
        assert deps_after[self_pkg] == deps_before[self_pkg]

        # History describes the update
        history = show_history(upgradeable_project)
        assert "Upgraded all packages" in history
        assert f"^{sample_pkg}==3.0.0 →" in history
        assert f"^{sample_dep}==0.5 →" in history

    def test_upgrade_pkg_only(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """
        Ensure all package have been upgrade
        """
        sample_pkg = canonicalize_name("sampleproject")
        sample_dep = canonicalize_name("peppercorn")
        self_pkg = canonicalize_name(f"uvrev-venv-{constants.DEFAULT_ENV}")

        deps_before = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # check the lock file is not yet updated
        assert deps_before == {
            sample_pkg: Version("3.0.0"),
            sample_dep: Version("0.5"),
            self_pkg: Version("1"),
        }

        result = invoke("upgrade", sample_pkg)

        assert result.exit_code == 0
        assert "Upgraded 1 package(s)" in result.output
        assert "created revision 2" in result.output

        deps_after = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # only pkg actually updated
        assert deps_after[sample_pkg] > deps_before[sample_pkg]
        # rest is unchanged
        assert deps_after[sample_dep] == deps_before[sample_dep]
        assert deps_after[self_pkg] == deps_before[self_pkg]

        # History describes the update
        history = show_history(upgradeable_project)
        assert f"Upgraded: {sample_pkg}" in history
        assert f"^{sample_pkg}==3.0.0 →" in history
        assert f"^{sample_dep}==0.5 →" not in history

    def test_upgrade_dep_only(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """
        Ensure all package have been upgrade
        """
        sample_pkg = canonicalize_name("sampleproject")
        sample_dep = canonicalize_name("peppercorn")
        self_pkg = canonicalize_name(f"uvrev-venv-{constants.DEFAULT_ENV}")

        deps_before = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # check the lock file is not yet updated
        assert deps_before == {
            sample_pkg: Version("3.0.0"),
            sample_dep: Version("0.5"),
            self_pkg: Version("1"),
        }

        result = invoke("upgrade", sample_dep)

        assert result.exit_code == 0
        assert "Upgraded 1 package(s)" in result.output
        assert "created revision 2" in result.output

        deps_after = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        # only dep is actually updated
        assert deps_after[sample_dep] > deps_before[sample_dep]
        # rest is unchanged
        assert deps_after[sample_pkg] == deps_before[sample_pkg]
        assert deps_after[self_pkg] == deps_before[self_pkg]

        # History describes the update
        history = show_history(upgradeable_project)
        assert f"Upgraded: {sample_dep}" in history
        assert f"^{sample_pkg}==3.0.0 →" not in history
        assert f"^{sample_dep}==0.5 →" in history

    def test_upgrade_json(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        result = invoke("upgrade", "--json")

        assert result.exit_code == 0
        report = parse_report(result)
        assert report["command"] == "upgrade"
        assert report["previous_revision"] == 1
        assert report["revision"] == 2
        assert report["changes"] == {
            "added": [],
            "updated": ["peppercorn", "sampleproject"],
            "downgraded": [],
            "removed": [],
        }
        assert report["versions"]["packages"]["sampleproject"]["old"] == "3.0.0"
        # the constraints in pyproject.toml are untouched
        assert report["dependency_changes"] == NO_CHANGES

        # Nothing left to upgrade
        result = invoke("upgrade", "--json")

        assert result.exit_code == 0
        assert "already up to date" in result.stderr
        report = parse_report(result)
        assert report["revision"] == report["previous_revision"] == 2
        assert report["all_changed_packages"] == []

    def test_downgrade_json(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """Pinning an older version reports it as downgraded"""
        assert invoke("upgrade").exit_code == 0

        result = invoke("add", "sampleproject==3.0.0", "--json")

        assert result.exit_code == 0, result.output
        report = parse_report(result)
        assert report["changes"]["downgraded"] == ["sampleproject"]
        assert "sampleproject" not in report["changes"]["updated"]
        assert report["versions"]["packages"]["sampleproject"]["new"] == "3.0.0"
        # a changed constraint can not be a downgrade
        assert report["dependency_changes"] == {
            "added": [],
            "updated": ["sampleproject"],
            "downgraded": [],
            "removed": [],
        }
        history = show_history(upgradeable_project)
        assert "vsampleproject==" in history
        assert "→ sampleproject==3.0.0" in history


class TestAddUpgradeAll:
    def test_add_upgrade_json(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """Adding with --upgrade-all adds the package and upgrades everything"""
        result = invoke("add", "--upgrade-all", "peppercorn", "--json")

        assert result.exit_code == 0
        assert "Created revision 2" in result.output
        # added with --upgrade, so locked versions don't block the new package
        assert "uv add --raw --upgrade 'peppercorn>=0'" in result.output
        assert "uv sync --upgrade" in result.output
        report = parse_report(result)
        assert report["command"] == "add"
        assert report["arguments"] == ["peppercorn"]
        assert report["options"] == {
            "upgrade_all": True,
            "python": None,
            "dry_run": False,
        }
        assert report["revision"] == 2
        assert report["changes"] == {
            "added": [],
            "updated": ["peppercorn", "sampleproject"],
            "downgraded": [],
            "removed": [],
        }
        assert report["dependency_changes"] == {
            "added": ["peppercorn"],
            "updated": [],
            "downgraded": [],
            "removed": [],
        }

        history = show_history(upgradeable_project)
        assert "Added: peppercorn; upgraded all packages" in history

    def test_add_without_upgrade_json(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """Without --upgrade-all the other packages keep their version"""
        result = invoke("add", "peppercorn", "--json")

        assert result.exit_code == 0
        report = parse_report(result)
        assert report["options"] == {
            "upgrade_all": False,
            "python": None,
            "dry_run": False,
        }
        assert report["changes"] == NO_CHANGES
        assert report["all_changed_dependencies"] == ["peppercorn"]

    def test_add_failing_json(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """The error report contains the options as well"""
        result = invoke("add", "--upgrade-all", "uvrev-does-not-exist", "--json")

        assert result.exit_code == 1
        report = parse_report(result)
        assert report["status"] == "error"
        assert report["options"] == {
            "upgrade_all": True,
            "python": None,
            "dry_run": False,
        }
        assert report["revision"] == report["previous_revision"] == 1


class TestCliIntegration:
    """
    Integrations of adding/deleting and restoring packages
    """

    @pytest.mark.dependency(name="1-add", depends=["0-create-default"])
    def test_add(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("add", "cowsay", "sampleproject")
        assert result.exit_code == 0
        assert "Created revision 1" in result.output

        installed = parse_uv_lock_packages(shared_project.lock_path.read_text())
        assert "cowsay" in installed
        assert "sampleproject" in installed

    @pytest.mark.dependency(name="1-add", depends=["1-add"])
    def test_add_again(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        """adding again does not change the revision"""
        result = invoke("add", "cowsay", "sampleproject", "--json")
        assert result.exit_code == 0
        assert "Created revision" not in result.output

        report = parse_report(result)
        assert report["command"] == "add"
        assert report["arguments"] == ["cowsay", "sampleproject"]
        assert report["revision"] == report["previous_revision"] == 1
        assert report["all_changed_packages"] == []
        assert report["all_changed_dependencies"] == []

        installed = parse_uv_lock_packages(shared_project.lock_path.read_text())
        assert "cowsay" in installed
        assert "sampleproject" in installed

    @pytest.mark.dependency(name="2-list", depends=["1-add"])
    def test_list(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("list")
        assert result.exit_code == 0
        assert "cowsay" in result.output
        assert "sampleproject" in result.output

    @pytest.mark.dependency(name="2-run", depends=["1-add"])
    def test_run(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("run", "cowsay", "-t", "This is actually working")
        assert result.exit_code == 0
        assert "uv sync --locked --check" in result.output
        assert "uv run --no-sync cowsay" in result.output
        assert "This is actually working" in result.output

    @pytest.mark.dependency(name="2-remove", depends=["1-add"])
    def test_remove(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("remove", "sampleproject")
        assert result.exit_code == 0
        assert "Removed sampleproject" in result.output
        assert "Created revision 2" in result.output

        installed = parse_uv_lock_packages(shared_project.lock_path.read_text())

        # still installed
        assert "cowsay" in installed
        # removed
        assert "sampleproject" not in installed

    @pytest.mark.dependency(name="2-remove-fail", depends=["2-remove"])
    def test_remove_failing(
        self, invoke: Invoker, shared_project: UVRevProject
    ) -> None:
        result = invoke("remove", "not-installed", "--json")
        assert result.exit_code == 1

        report = parse_report(result)
        assert report["status"] == "error"
        assert report["command"] == "remove"
        assert report["revision"] == report["previous_revision"] == 2
        assert "uv remove not-installed" in report["error_message"]
        assert report["changes"] == NO_CHANGES

    @pytest.mark.dependency(name="3-restore", depends=["2-remove"])
    def test_restore(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("restore", "1")
        assert result.exit_code == 0
        assert "Restored to revision 1" in result.output

        installed = parse_uv_lock_packages(shared_project.lock_path.read_text())
        # now both again install
        assert "cowsay" in installed
        assert "sampleproject" in installed

    @pytest.mark.dependency(name="3-restore-fail", depends=["2-remove"])
    def test_restore_failing(
        self, invoke: Invoker, shared_project: UVRevProject
    ) -> None:
        result = invoke("restore", "10")
        assert result.exit_code == 1
        assert "Revision 10 does not exist" in result.output

    @pytest.mark.dependency(name="3-restore-fail", depends=["3-restore"])
    def test_restore_failing_json(
        self, invoke: Invoker, shared_project: UVRevProject
    ) -> None:
        result = invoke("restore", "10", "--json")
        assert result.exit_code == 1

        report = parse_report(result)
        assert report["status"] == "error"
        assert report["arguments"] == ["10"]
        assert report["revision"] == 3
        assert report["error_message"] == "Revision 10 does not exist"

    @pytest.mark.dependency(name="4-history", depends=["3-restore"])
    def test_history(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("history")
        assert result.exit_code == 0

    @pytest.mark.dependency(name="5-add-transitive", depends=["3-restore"])
    def test_add_transitive_json(
        self, invoke: Invoker, shared_project: UVRevProject
    ) -> None:
        """
        Adding an already locked transitive package only changes the dependencies
        """
        result = invoke("add", "peppercorn", "--json")
        assert result.exit_code == 0

        report = parse_report(result)
        assert report["previous_revision"] == 3
        assert report["revision"] == 4
        assert report["dependency_changes"] == {
            "added": ["peppercorn"],
            "updated": [],
            "downgraded": [],
            "removed": [],
        }
        assert report["versions"]["dependencies"] == {
            "peppercorn": {"old": None, "new": ""}
        }
        assert report["all_changed_packages"] == []


class TestRemoveIgnoreMissing:
    def test_fails_without_option(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """Without --ignore-missing nothing is removed, if one package is missing"""
        result = invoke("remove", "sampleproject", "not-installed")

        assert result.exit_code == 1
        deps = parse_uv_lock_packages(upgradeable_project.lock_path.read_text())
        assert "sampleproject" in deps

    def test_skips_missing(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        # the names are compared normalized
        result = invoke(
            "remove", "--ignore-missing", "SampleProject", "not-installed", "--json"
        )

        assert result.exit_code == 0, result.output
        assert "Not a dependency, skipped: not-installed" in result.stderr
        assert "uv remove SampleProject\n" in result.stderr
        report = parse_report(result)
        assert report["arguments"] == ["SampleProject", "not-installed"]
        assert report["options"] == {"ignore_missing": True, "dry_run": False}
        assert report["revision"] == 2
        assert report["dependency_changes"]["removed"] == ["sampleproject"]

    def test_nothing_left(
        self, invoke: Invoker, upgradeable_project: UVRevProject
    ) -> None:
        """If no package is a dependency, nothing happens and no revision is created"""
        result = invoke("remove", "--ignore-missing", "not-installed", "--json")

        assert result.exit_code == 0, result.output
        assert "Nothing to remove" in result.stderr
        assert "uv remove" not in result.stderr
        report = parse_report(result)
        assert report["status"] == "success"
        assert report["revision"] == report["previous_revision"] == 1
        assert report["all_changed_dependencies"] == []


class TestEnvVariable:
    """UVREV_ENV selects the environment, if --env is not given"""

    @pytest.fixture
    def envs(self, fake_basedir: Path) -> Path:
        for name in ("base", "foo", "bar"):
            create_fake_env(fake_basedir / name)
        return fake_basedir

    def test_default(self, invoke: Invoker, envs: Path) -> None:
        assert invoke("path").stdout.strip() == str(envs / "base")

    def test_variable(
        self, invoke: Invoker, envs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(constants.ENV_VARIABLE, "foo")

        assert invoke("path").stdout.strip() == str(envs / "foo")
        # the option wins
        assert invoke("path", "-e", "bar").stdout.strip() == str(envs / "bar")

    def test_wins_over_activated(
        self, invoke: Invoker, envs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", str(envs / "bar" / ".venv"))
        assert invoke("path").stdout.strip() == str(envs / "bar")

        monkeypatch.setenv(constants.ENV_VARIABLE, "foo")

        assert invoke("path").stdout.strip() == str(envs / "foo")

    def test_missing(
        self, invoke: Invoker, envs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(constants.ENV_VARIABLE, "nope")

        result = invoke("path")

        assert result.exit_code == 2
        assert "Project 'nope' does not exist" in result.output


class TestCommon:
    @pytest.mark.parametrize(
        "cmd",
        [
            ["add", "pandas"],
            ["remove", "pandas"],
            ["sync"],
            ["list"],
            ["history"],
            ["restore", "2"],
            ["path"],
            ["run", "pytest"],
        ],
        ids=" ".join,
    )
    def test_missing_env(
        self,
        cmd: list[str],
        invoke: Invoker,
        fake_basedir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """
        Fail if env is missing and also warn if VIRTUAL_ENV is not uvrev env.
        """
        venv = str(tmp_path / "missing" / ".venv")
        monkeypatch.setenv("VIRTUAL_ENV", venv)
        result = invoke(*cmd)
        assert result.exit_code == 2
        assert f" VIRTUAL_ENV ({venv}) is not a uvrev env. Ignoring." in result.output
