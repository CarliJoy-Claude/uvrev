import shutil
from pathlib import Path
from typing import Any, Protocol

import pytest
from click.testing import CliRunner, Result
from packaging.utils import canonicalize_name
from packaging.version import Version

import uvrev.cli
from uvrev import constants, operations
from uvrev.commands import git
from uvrev.io import read_pyproject
from uvrev.operations import show_history
from uvrev.parser import parse_uv_lock_packages
from uvrev.types import UVRevProject


def create_fake_env(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    proj = UVRevProject(path)
    proj.lock_path.touch()
    proj.pyproject_path.write_text("[tool.uv]\npackage = false\n")


class Invoker(Protocol):
    def __call__(self, *args: str) -> Result: ...


@pytest.fixture(scope="module")
def shared_basedir_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    A shared base directory for integration tests that depend on each others state

    USE **ONLY** FOR INTEGRATION TESTS

    Normally tests should be independent of other tests
    """
    tmpdir = tmp_path_factory.mktemp("shared_basedir")
    return tmpdir


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
    return UVRevProject(shared_basedir / constants.DEFAULT_ENV)


@pytest.fixture
def invoke() -> Invoker:
    """Create a CLI test runner."""
    runner = CliRunner()

    def invoke(*args: str) -> Result:
        return runner.invoke(uvrev.cli.cli, args=args)

    return invoke


@pytest.fixture(autouse=True)
def tmp_basedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Ensure only tmp basedir are used
    """
    base_dir = tmp_path / "tmp_basedir"
    monkeypatch.setenv("UVREV_BASE", str(base_dir))
    return base_dir


@pytest.fixture
def upgradeable_project(fake_basedir: Path, data_dir: Path) -> UVRevProject:
    """Create a project with an uv.lock that can be updated"""
    proj = UVRevProject(fake_basedir / constants.DEFAULT_ENV)
    operations.create_env(fake_basedir, constants.DEFAULT_ENV, constants.DEFAULT_PYTHON)
    shutil.copytree(data_dir / "upgradeable", proj.project_path, dirs_exist_ok=True)
    operations.create_revision(proj, "Add sampleproject")
    return proj


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
            ["env", "--help"],
            ["env", "create", "--help"],
        ],
        ids=lambda x: " ".join(x),
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
        "cmd, folder",
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
        proj = UVRevProject(target)
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
            ".git",
            ".venv",
        }
        created_files = {f.name for f in target.iterdir()}
        assert created_files == accepted_files


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
    def test_sample(self, fake_basedir: Path, data_dir: Path, invoke: Invoker) -> None:
        target = fake_basedir / constants.DEFAULT_ENV
        shutil.copytree(data_dir / "sample", target)

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
        print(history)
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
        print(history)
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
        print(history)
        assert f"Upgraded: {sample_dep}" in history
        assert f"^{sample_pkg}==3.0.0 →" not in history
        assert f"^{sample_dep}==0.5 →" in history


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
        result = invoke("add", "cowsay", "sampleproject")
        assert result.exit_code == 0
        assert "Created revision" not in result.output

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
        assert "uv run --locked cowsay" in result.output
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

    @pytest.mark.dependency(name="4-history", depends=["3-restore"])
    def test_history(self, invoke: Invoker, shared_project: UVRevProject) -> None:
        result = invoke("history")
        assert result.exit_code == 0
        print(result.output)


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
        ids=lambda x: " ".join(x),
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
