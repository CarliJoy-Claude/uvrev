"""The operations on an environment: create, change, sync, restore, history.

Everything that changes the tracked files of an environment (``pyproject.toml``,
``uv.lock``, ``.python-version``) and records it as a revision (a git commit
and a ``rev_<n>`` tag) lives here, with the state comparison it needs.

A change is planned as data (``Change``, built by the ``plan_*`` functions)
and run by ``apply_change``, so all changes behave the same and can be shown
as a dry run. ``sync_packages`` only brings the venv in line with the files.

The functions take a ``UVRevProject`` and return what happened (``Outcome``).
They do not print reports or parse options (``cli.py``). How a venv is
verified and repaired is in ``verify.py`` and ``repair.py``.
"""

import contextlib
import getpass
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import distributions, version as package_version
from pathlib import Path

import click
import tomlkit
from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from . import constants
from .commands import git, uv
from .differ import Diff, create_changelog
from .exceptions import RevisionNotFoundError
from .io import exclude_from_git, read_pyproject, write_pyproject
from .marker import remove_marker
from .parser import (
    any_version,
    parse_pyproject_deps,
    parse_python_lock,
    parse_python_request,
    parse_uv_environments,
    parse_uv_lock_packages,
    same_except_any_version,
)
from .protection import unprotect, writable_venv
from .repair import RepairResult, repair
from .report import RevisionChange
from .settings import Settings
from .types import Revision, SubprocessFailure, UVRevProject
from .verify import Verifier, VerifyResult

PYPROJECT_FILE = "pyproject.toml"
LOCK_FILE = "uv.lock"


@dataclass(frozen=True)
class Outcome:
    """Everything a command did to an environment"""

    # changes compared to the previous revision
    change: RevisionChange
    # repair of the unprotected environment before the change, None if it was
    # not marked as unprotected
    repaired: RepairResult | None = None
    # result of verifying (and fixing) the venv afterwards, None if it did not run
    verified: VerifyResult | None = None


def create_env(
    base_dir: Path,
    name: str,
    python: str | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> UVRevProject:
    """Create a new UV project with revision tracking.

    Creating an environment that was created successfully before does nothing,
    the Python version is not checked in this case.

    Args:
        base_dir: Base directory to create project in.
        name: Name of the project.
        python: Python request (a version, range, implementation or the path
            of an interpreter), None to let uv choose its default.
            uv pins it in ``.python-version``.
        force: Delete the folder of the environment first, if it was not
            created successfully (no ``.uvrev_created_successfully``).
        settings: Settings of uvrev (defaults if not given).

    Returns:
        Created UVRevProject instance.

    Raises:
        FileExistsError: If the folder exists but the environment was not
            created successfully and ``force`` is not given.
    """
    target = base_dir.joinpath(name)
    project = UVRevProject(target, settings or Settings())
    if project.created_successfully():
        return project
    if target.exists():
        if not force:
            raise FileExistsError(
                f"Folder for project '{name}' ({target}) already exists, "
                "but the environment was not created successfully. "
                f"Use 'uvrev env fix {name}' if it is a working environment "
                f"(e.g. created by an older uvrev) or 'uvrev env create --force {name}' "
                "to delete and create it again."
            )
        # a protected .venv can not be deleted
        unprotect(project)
        shutil.rmtree(target)
    target.mkdir(parents=True)
    # Create project using uv
    uv(
        "init",
        *(("--python", python) if python else ()),
        "--no-package",
        "--no-readme",
        "--no-description",
        "--no-workspace",
        "--name",
        f"uvrev-venv-{name}",
        ".",
        project=project,
    )
    target.joinpath("main.py").unlink(missing_ok=True)
    if python:
        # uv init only pins the minor version of the interpreter it found:
        # pin the request itself (e.g. a range or the path of an interpreter),
        # like the commands that change the Python later
        uv("python", "pin", python, project=project)
    # Configure pyproject.toml
    data = read_pyproject(project)
    data.setdefault("tool", {}).setdefault("uv", {})["package"] = False
    data.setdefault("project", {})["version"] = "0"
    write_pyproject(project, data)
    update_environments(project)
    with writable_venv(project):
        uv("sync", project=project)
    update_python_lock(project)

    # Create initial revision
    configure_git(project)
    git("add", ".", project=project)
    git("commit", "-m", constants.INITIAL_COMMIT_MSG, project=project)
    git(
        "tag",
        "-m",
        constants.INITIAL_COMMIT_MSG,
        "--annotate",
        "rev_0",
        project=project,
    )

    mark_created(project)
    return project


def git_identity(
    getuser: Callable[[], str] = getpass.getuser, hostname: str | None = None
) -> str:
    """Email of the user running uvrev, used for the commits of the revisions

    >>> git_identity(lambda: "deploy", "server1")
    'deploy@server1'
    """
    try:
        user = getuser()
    except (OSError, KeyError):
        # no user name for the uid, e.g. in a container with an arbitrary uid
        user = "uvrev"
    return f"{user}@{hostname or socket.gethostname() or 'localhost'}"


def configure_git(project: UVRevProject) -> None:
    """Configure the git repository of the environment.

    The revisions are committed by uvrev, so it always uses its own identity
    (name ``uvrev``, email ``<user>@<hostname>``) instead of the one of the
    user, which is usually missing for service users. Signing is disabled, as
    it needs a key and can ask for a passphrase, which would hang uvrev.
    """
    config = (
        ("user.name", "uvrev"),
        ("user.email", git_identity()),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("tag.forceSignAnnotated", "false"),
    )
    for key, value in config:
        git("config", "--local", key, value, project=project)


def mark_created(project: UVRevProject) -> None:
    """Mark the environment as created successfully.

    The marker contains the uvrev version and is ignored by git
    (``.git/info/exclude``, so no tracked file is changed).
    """
    exclude_from_git(
        project, constants.CREATED_MARKER_FILE, constants.UNPROTECTED_MARKER_FILE
    )
    project.created_marker_path.write_text(f"{package_version('uvrev')}\n")


def supported_python_versions(
    project: UVRevProject, requires_python: str
) -> tuple[str, ...]:
    """Python versions (major.minor) the environment is resolved for.

    Taken from the setting ``python_versions_supported`` (by default the Python
    versions installed for uv). Only versions matching ``requires-python`` are
    used, as uv rejects environments that don't.

    Args:
        project: Project with the settings.
        requires_python: ``requires-python`` of the project.

    Returns:
        Sorted versions.
    """
    spec = SpecifierSet(requires_python)
    versions = {Version(v) for v in project.settings.python_versions_supported}
    return tuple(str(v) for v in sorted(versions) if v in spec)


def environment_markers(
    versions: tuple[str, ...],
    sys_platform: str = sys.platform,
    machine: str = platform.machine(),
) -> tuple[str, ...]:
    """Markers for ``tool.uv.environments``, one per Python version.

    >>> for marker in environment_markers(("3.12", "3.14"), "linux", "x86_64"):
    ...     print(marker)
    python_version == '3.12' and sys_platform == 'linux' and platform_machine == 'x86_64'
    python_version == '3.14' and sys_platform == 'linux' and platform_machine == 'x86_64'
    """
    return tuple(
        f"python_version == '{version}' and sys_platform == '{sys_platform}'"
        f" and platform_machine == '{machine}'"
        for version in versions
    )


def update_environments(project: UVRevProject) -> None:
    """Update ``tool.uv.environments`` in pyproject.toml, if needed.

    Called before uv resolves, otherwise the resolution might fail.
    If no Python version is supported, the setting is removed.
    """
    data = read_pyproject(project)
    requires_python = str(data.get("project", {}).get("requires-python", ""))
    wanted = environment_markers(supported_python_versions(project, requires_python))
    current = tuple(parse_uv_environments(tomlkit.dumps(data)))
    if current == wanted:
        return
    uv_table = data.setdefault("tool", {}).setdefault("uv", {})
    if wanted:
        markers = tomlkit.array()
        markers.extend(wanted)
        uv_table["environments"] = markers.multiline(True)
    else:
        del uv_table["environments"]
    write_pyproject(project, data)


def list_projects(base_dir: Path) -> tuple[str, ...]:
    """List all projects.

    Args:
        base_dir: Base directory containing projects.

    Returns:
        List of project names.
    """
    if not base_dir.exists():
        return ()
    return tuple(
        p.name
        for p in base_dir.iterdir()
        if p.is_dir() and (p / "pyproject.toml").exists()
    )


# Prints the version of the running interpreter, PEP 440 compatible
# (platform.python_version() can be e.g. "3.15.0a1+" which is no valid version)
_PRINT_PYTHON_VERSION = """
import sys
v = sys.version_info
pre = {"alpha": "a", "beta": "b", "candidate": "rc"}.get(v.releaselevel)
print(f"{v.major}.{v.minor}.{v.micro}" + (f"{pre}{v.serial}" if pre else ""))
"""


def get_python_version(project: UVRevProject) -> Version | None:
    """Get the exact version of the Python interpreter of the virtual environment.

    The interpreter is asked directly, as the linked interpreter can change
    without uv or uvrev noticing (e.g. a patch update of a managed Python).

    Args:
        project: Project to inspect.

    Returns:
        Version of the interpreter, None if the virtual environment does not exist.
    """
    python = project.interpreter
    return interpreter_version(python) if python else None


def interpreter_version(python: Path) -> Version:
    """Ask a Python interpreter for its exact version"""
    try:
        output = subprocess.run(
            (str(python), "-c", _PRINT_PYTHON_VERSION),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e
    return Version(output.strip())


def update_python_lock(project: UVRevProject) -> None:
    """Write the exact Python version of the environment to the lock file.

    Kept unchanged if the virtual environment does not exist.
    """
    if version := get_python_version(project):
        project.python_lock_path.write_text(f"{version}\n")


def committed_files(
    project: UVRevProject, ref: str, paths: tuple[Path, ...]
) -> tuple[str, ...]:
    """Names of the given files (in the project root) that exist in the git ref."""
    names = tuple(path.name for path in paths)
    output = git(
        "ls-tree",
        "--name-only",
        ref,
        "--",
        *names,
        project=project,
        capture_output=True,
    )
    existing = set(output.splitlines())
    return tuple(name for name in names if name in existing)


def tracked_paths(project: UVRevProject) -> tuple[Path, ...]:
    """Files whose state is tracked in the revisions"""
    return (
        project.pyproject_path,
        project.lock_path,
        project.python_version_path,
        project.python_lock_path,
    )


def get_last_revision_number(project: UVRevProject) -> Revision:
    """Get the number of the latest revision.

    Uses git tag -l to list all numeric tags and returns the highest.

    Args:
        project: Project to get revision number for.

    Returns:
        Latest revision number (0 if no revisions exist).
    """
    output = git(
        "tag",
        "--list",
        "rev_[0-9]*",
        project=project,
        capture_output=True,
    )

    last_rev = 0

    for rev in output.splitlines(keepends=False):
        with contextlib.suppress(ValueError):
            last_rev = max(last_rev, int(rev.removeprefix("rev_")))

    return Revision(last_rev)


def no_change(project: UVRevProject) -> RevisionChange:
    """The outcome of a command that changed nothing: no new revision"""
    revision = get_last_revision_number(project)
    return RevisionChange(revision, revision)


def committed_state(project: UVRevProject) -> Mapping[str, str]:
    """Content of the tracked files in the last revision, by file name.

    Files missing in the revision are empty.
    """
    paths = tracked_paths(project)
    committed = committed_files(project, "HEAD", paths)
    return {
        path.name: git(
            "show", f"HEAD:{path.name}", project=project, capture_output=True
        )
        if path.name in committed
        else ""
        for path in paths
    }


def current_state(project: UVRevProject) -> Mapping[str, str]:
    """Current content of the tracked files, by file name (empty if missing)"""
    return {
        path.name: path.read_text() if path.exists() else ""
        for path in tracked_paths(project)
    }


def is_same_state(old: Mapping[str, str], new: Mapping[str, str]) -> bool:
    """True if two states of the tracked files describe the same environment.

    This is also the case if only ``name`` was rewritten to ``name>=0``
    (or back), which is the same requirement.
    """
    if old == new:
        return True
    others = (constants.PYTHON_VERSION_FILE, constants.PYTHON_VERSION_LOCK_FILE)
    return all(old[name] == new[name] for name in others) and same_except_any_version(
        old[PYPROJECT_FILE], new[PYPROJECT_FILE], old[LOCK_FILE], new[LOCK_FILE]
    )


def state_change(
    old: Mapping[str, str],
    new: Mapping[str, str],
    previous_revision: Revision,
    revision: Revision,
) -> RevisionChange:
    """Compare two states of the tracked files"""
    return RevisionChange(
        previous_revision,
        revision,
        packages=Diff(
            parse_uv_lock_packages(old[LOCK_FILE]),
            parse_uv_lock_packages(new[LOCK_FILE]),
        ),
        dependencies=Diff(
            parse_pyproject_deps(old[PYPROJECT_FILE]),
            parse_pyproject_deps(new[PYPROJECT_FILE]),
        ),
        python=Diff(
            parse_python_lock(old[constants.PYTHON_VERSION_LOCK_FILE]),
            parse_python_lock(new[constants.PYTHON_VERSION_LOCK_FILE]),
        ),
        environments=Diff(
            parse_uv_environments(old[PYPROJECT_FILE]),
            parse_uv_environments(new[PYPROJECT_FILE]),
        ),
        python_request=Diff(
            parse_python_request(old[constants.PYTHON_VERSION_FILE]),
            parse_python_request(new[constants.PYTHON_VERSION_FILE]),
        ),
    )


def changelog_of(change: RevisionChange) -> str:
    """Changelog of a change, as written in the commit of a revision"""
    return create_changelog(
        change.dependencies, change.packages, change.python, change.environments
    )


def create_revision(
    project: UVRevProject, message: str = "Update packages"
) -> RevisionChange:
    """Create a new revision (commit + tag).

    Args:
        project: Project to create revision in.
        message: Commit message.

    Returns:
        Changes compared to the previous revision.
        If nothing changed, no revision is created.
    """
    last_rev = get_last_revision_number(project)
    update_python_lock(project)

    old = committed_state(project)
    current = current_state(project)
    if old == current:
        return RevisionChange(last_rev, last_rev)
    if is_same_state(old, current):
        # only "name" was rewritten to "name>=0" (or back): keep the committed
        # files instead of an empty revision
        committed = (name for name, content in old.items() if content)
        git("checkout", "HEAD", "--", *committed, project=project)
        return RevisionChange(last_rev, last_rev)

    rev_num = Revision(last_rev + 1)
    change = state_change(old, current, last_rev, rev_num)

    # Commit changes
    git(
        "add",
        "--",
        *(path.name for path in tracked_paths(project) if path.exists()),
        project=project,
    )

    commit_msg = f"(rev {rev_num}) {message}\n{changelog_of(change)}"
    git("commit", "-m", commit_msg, project=project)

    # Create annotated tag (can be signed)
    git("tag", "-m", commit_msg, "--annotate", f"rev_{rev_num}", project=project)

    return change


@contextlib.contextmanager
def scratch_copy(project: UVRevProject) -> Iterator[UVRevProject]:
    """A temporary copy of the files of an environment, for dry runs.

    It has no venv and no git repository, so uv can resolve changes in it
    (without syncing) while the environment stays untouched.
    """
    with tempfile.TemporaryDirectory(prefix="uvrev-dry-run-") as directory:
        for path in (
            project.pyproject_path,
            project.lock_path,
            project.python_version_path,
        ):
            if path.exists():
                shutil.copy2(path, directory)
        yield UVRevProject(Path(directory), project.settings)


def preview_python(project: UVRevProject, scratch: UVRevProject) -> str:
    """The exact Python version the environment would get, as lock file content.

    Only predicted if the pinned version changed and uv finds an interpreter
    for it. Otherwise the recorded version is kept: a newer patch version or an
    interpreter uv would download is only seen by the real command.
    """
    recorded = (
        project.python_lock_path.read_text()
        if project.python_lock_path.exists()
        else ""
    )
    if (
        current_state(project)[constants.PYTHON_VERSION_FILE]
        == (current_state(scratch)[constants.PYTHON_VERSION_FILE])
    ):
        return recorded
    try:
        python = uv("python", "find", project=scratch, capture_output=True).strip()
    except SubprocessFailure:
        return recorded
    return f"{interpreter_version(Path(python))}\n"


def preview_change(project: UVRevProject, scratch: UVRevProject) -> RevisionChange:
    """What a command would change, from the scratch copy it was applied to.

    No revision is created, so ``revision`` equals ``previous_revision``.
    """
    last_rev = get_last_revision_number(project)
    old = committed_state(project)
    new = {
        **current_state(scratch),
        constants.PYTHON_VERSION_LOCK_FILE: preview_python(project, scratch),
    }
    if is_same_state(old, new):
        return RevisionChange(last_rev, last_rev)
    return state_change(old, new, last_rev, last_rev)


@contextlib.contextmanager
def reset_on_failure(project: UVRevProject) -> Iterator[None]:
    """Reset the files to the last revision, if a command fails.

    Used around commands that change the files, so a failure (e.g. the Python
    version is not available) leaves no state behind that is not part of a
    revision.
    """
    try:
        yield
    except click.ClickException:
        # a failed command, or a missing one (uv, chmod)
        changed = (
            project.pyproject_path,
            project.python_version_path,
            project.lock_path,
        )
        reset = committed_files(project, "HEAD", changed)
        git("checkout", "HEAD", "--", *reset, project=project)
        raise


def list_installed_packages(
    project: UVRevProject,
) -> tuple[tuple[NormalizedName, Version, bool], ...]:
    """Get installed packages and which are in pyproject.toml.

    Args:
        project: Project to list packages for.

    Returns:
        Tuple of (pkg, version, defined_in_pyproject.toml)
    """
    all_packages = parse_uv_lock_packages(project.lock_path.read_text())
    pyproject_deps = set(
        parse_pyproject_deps(project.pyproject_path.read_text()).keys()
    )
    return tuple(
        (pkg, version, pkg in pyproject_deps)
        for pkg, version in sorted(all_packages.items())
    )


def python_request(project: UVRevProject) -> str | None:
    """The Python request pinned for the environment (``.python-version``).

    It is what was given to ``uv python pin``: a version, but also a range,
    an implementation or a path. None if nothing is pinned.
    """
    if not project.python_version_path.is_file():
        return None
    return project.python_version_path.read_text().strip() or None


def python_message(
    request: str | None,
    old: tuple[str | None, Version | None],
    new: tuple[str | None, Version | None],
) -> str:
    """Part of the revision message about a pinned Python request.

    Args:
        request: The Python request given by the user, None if none was given.
        old: Pinned request and exact interpreter version before the change.
        new: The same after the change.

    Returns:
        Empty if neither the pinned request nor the interpreter changed.

    Examples:
        >>> old = ("3.13", Version("3.13.9"))
        >>> python_message("3.14", old, ("3.14", Version("3.14.1")))
        'Upgraded Python: 3.13.9 → 3.14.1 (pinned 3.14)'
        >>> python_message("3.12", old, ("3.12", Version("3.12.3")))
        'Downgraded Python: 3.13.9 → 3.12.3 (pinned 3.12)'
        >>> python_message("cpython-3.13", old, ("cpython-3.13", Version("3.13.9")))
        'Pinned Python cpython-3.13'
        >>> python_message("3.13", (None, None), ("3.13", Version("3.13.9")))
        'Pinned Python 3.13'
        >>> python_message("3.13", old, old)
        ''
        >>> python_message(None, old, ("3.13", Version("3.13.10")))
        ''
    """
    if request is None or old == new:
        return ""
    old_version, new_version = old[1], new[1]
    if old_version is None or new_version is None or old_version == new_version:
        return f"Pinned Python {request}"
    action = "Upgraded" if new_version > old_version else "Downgraded"
    return f"{action} Python: {old_version} → {new_version} (pinned {request})"


def split_missing(
    project: UVRevProject, packages: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split package names into dependencies of the project and others.

    The names are compared normalized (``Foo_Bar`` is ``foo-bar``).

    Returns:
        The dependencies and the names that are no dependencies.
    """
    dependencies = parse_pyproject_deps(project.pyproject_path.read_text())
    present = tuple(p for p in packages if canonicalize_name(p) in dependencies)
    missing = tuple(p for p in packages if canonicalize_name(p) not in dependencies)
    return present, missing


def list_venv_packages(project: UVRevProject) -> Mapping[NormalizedName, Version]:
    """Get the packages actually installed in the virtual environment.

    Args:
        project: Project to inspect.

    Returns:
        Dictionary mapping canonical package names to their installed versions.
        Empty if the virtual environment does not exist (yet).
    """
    return {
        canonicalize_name(dist.metadata["Name"]): Version(dist.version)
        for dist in distributions(path=[str(path) for path in project.site_packages])
    }


def sync_packages(
    project: UVRevProject, clean: bool = False, verify: Verifier | None = None
) -> Outcome:
    """Sync the virtual environment with the lock file.

    As the lock file is not changed, no revision is created.

    Args:
        project: Project to sync.
        clean: Start anew: the complete uv cache and the virtual environment
            are deleted first, so everything is downloaded and installed again.
            This is the way out if the environment can not be repaired
            (it is no longer marked as unprotected afterwards). Running
            processes of the environment lose their files.
        verify: Verifies and fixes the venv afterwards, None to skip.

    Returns:
        What was done. The change contains the installed packages (their
        versions, so a clean sync of an intact environment reports no changes).
    """
    # a clean sync creates the venv anew, so there is nothing to repair
    repaired = None if clean else repair(project)
    installed_before = list_venv_packages(project)
    python_before = get_python_version(project)
    if clean:
        # a protected .venv can not be deleted
        unprotect(project)
        # --force: don't wait for other uv processes using the cache
        uv("cache", "clean", "--force", project=project)
        if project.venv_path.exists():
            shutil.rmtree(project.venv_path)
    with writable_venv(project):
        uv("sync", "--frozen", project=project)
    if clean:
        remove_marker(project.unprotected_marker_path)
    python_after = get_python_version(project)
    rev = get_last_revision_number(project)
    change = RevisionChange(
        rev,
        rev,
        Diff(installed_before, list_venv_packages(project)),
        python=Diff(
            {canonicalize_name("python"): python_before} if python_before else {},
            {canonicalize_name("python"): python_after} if python_after else {},
        ),
    )
    return Outcome(change, repaired, verify(project, True) if verify else None)


def show_history(project: UVRevProject) -> str:
    """Get revision history.

    Args:
        project: Project to get history for.

    Returns:
        Formatted git log output.
    """
    return git(
        "log",
        "--tags",
        "--color=always",
        "--format=%ai  %C(bold)%C(green)%s%C(reset)%n%b",
        "--reverse",
        project=project,
        capture_output=True,
    )


type UvCall = tuple[str, ...]


@dataclass(frozen=True)
class Change:
    """What a command does to the files of an environment, as data.

    It only describes the change, ``apply_change`` runs it.
    """

    # uv commands as they run for real (a dry run translates them)
    uv_calls: tuple[UvCall, ...] = ()
    # message of the revision, empty if the change only pins Python
    message: str = ""
    # Python request to pin before the uv calls, e.g. ``3.14``, ``>=3.13,<3.15``,
    # ``cpython-3.14`` or ``/usr/bin/python3.14``
    python: str | None = None
    # take the files from this revision before the uv calls
    restore_from: Revision | None = None

    @property
    def is_empty(self) -> bool:
        """True if there is nothing to do

        >>> Change().is_empty, Change(python="3.14").is_empty
        (True, False)
        """
        return not self.uv_calls and not self.python


def plan_add(
    packages: tuple[str, ...], upgrade_all: bool = False, python: str | None = None
) -> Change:
    """Plan adding packages.

    Args:
        packages: Requirements to add. A requirement without version
            (``six``) replaces an existing constraint (``six==1.15``).
        upgrade_all: Add with ``uv add --upgrade`` (so a package needing newer
            versions of locked packages can be added) and run ``uv sync --upgrade``
            afterwards, so everything is upgraded.
        python: Python request, pinned before the packages are added.

    Examples:
        >>> plan_add(("six", "numpy>=2")).uv_calls
        (('add', '--raw', 'six>=0', 'numpy>=2'),)
        >>> change = plan_add(("six",), upgrade_all=True, python="3.14")
        >>> change.uv_calls
        (('add', '--raw', '--upgrade', 'six>=0'), ('sync', '--upgrade'))
        >>> change.message, change.python
        ('Added: six; upgraded all packages', '3.14')
    """
    # uv keeps an existing constraint for a bare name, so a requirement
    # without version is given as "any version"
    requirements = tuple(any_version(package) for package in packages)
    message = f"Added: {', '.join(packages)}"
    if not upgrade_all:
        return Change((("add", "--raw", *requirements),), message, python)
    return Change(
        (("add", "--raw", "--upgrade", *requirements), ("sync", "--upgrade")),
        f"{message}; upgraded all packages",
        python,
    )


def plan_remove(packages: tuple[str, ...]) -> Change:
    """Plan removing packages, nothing is done if there are none.

    >>> plan_remove(("six", "numpy"))
    Change(uv_calls=(('remove', 'six', 'numpy'),), message='Removed: six, numpy', python=None, restore_from=None)
    >>> plan_remove(()).is_empty
    True
    """
    if not packages:
        return Change()
    return Change((("remove", *packages),), f"Removed: {', '.join(packages)}")


def plan_upgrade(packages: tuple[str, ...], python: str | None = None) -> Change:
    """Plan upgrading the given packages, or all if none is given.

    Args:
        packages: Package names to upgrade (empty for all).
        python: Python request, pinned before the packages are upgraded.

    Examples:
        >>> plan_upgrade(())
        Change(uv_calls=(('sync', '--upgrade'),), message='Upgraded all packages', python=None, restore_from=None)
        >>> change = plan_upgrade(("six", "numpy"), "3.14")
        >>> change.uv_calls
        (('sync', '--upgrade-package', 'six'), ('sync', '--upgrade-package', 'numpy'))
        >>> change.message, change.python
        ('Upgraded: six, numpy', '3.14')
    """
    if not packages:
        return Change((("sync", "--upgrade"),), "Upgraded all packages", python)
    calls = tuple(("sync", "--upgrade-package", package) for package in packages)
    return Change(calls, f"Upgraded: {', '.join(packages)}", python)


def plan_upgrade_python(request: str) -> Change:
    """Plan changing the Python of the environment without upgrading packages.

    The request is pinned and the environment synced, which recreates the
    virtual environment and updates ``uv.lock`` if needed.
    ``requires-python`` is not changed, so downgrades are possible as well.

    Args:
        request: Python request, everything ``uv python pin`` accepts
            (e.g. ``3.14``, ``3.14.1``, ``>=3.13,<3.15``, ``cpython-3.14`` or
            the path of an interpreter like ``/usr/bin/python3.14``).

    Examples:
        >>> plan_upgrade_python("3.14")
        Change(uv_calls=(('sync',),), message='', python='3.14', restore_from=None)
    """
    return Change((("sync",),), python=request)


def plan_restore(revision: Revision) -> Change:
    """Plan restoring the files of a revision and installing them.

    The sync recreates the virtual environment if the Python version changed.

    >>> change = plan_restore(Revision(3))
    >>> change.uv_calls, change.message, change.restore_from
    ((('sync', '--locked'),), 'Restored to revision 3', 3)
    """
    return Change(
        (("sync", "--locked"),),
        f"Restored to revision {revision}",
        restore_from=revision,
    )


def dry_run_call(call: UvCall) -> UvCall:
    """The uv call of a dry run: it only changes the files, never the venv.

    >>> dry_run_call(("add", "--raw", "six>=0"))
    ('add', '--no-sync', '--raw', 'six>=0')
    >>> dry_run_call(("remove", "six"))
    ('remove', '--no-sync', 'six')
    >>> dry_run_call(("sync", "--upgrade"))
    ('lock', '--upgrade')
    >>> dry_run_call(("sync", "--locked"))
    ('lock', '--check')
    """
    command, *args = call
    if command == "sync":
        # uv lock has no --locked, --check fails as well if uv.lock is outdated
        return ("lock", *("--check" if arg == "--locked" else arg for arg in args))
    return (command, "--no-sync", *args)


def restore_files(
    project: UVRevProject, target: UVRevProject, revision: Revision
) -> None:
    """Write the files of a revision of the project into the target.

    The exact Python version is not restored, it is recorded again from the
    recreated virtual environment.

    Args:
        project: Project with the revisions.
        target: The project itself, or its scratch copy in a dry run.
        revision: Revision to take the files from.

    Raises:
        RevisionNotFoundError: If the revision does not exist.
    """
    tag_name = f"rev_{revision}"
    try:
        git("rev-parse", tag_name, project=project, capture_output=True)
    except SubprocessFailure:
        raise RevisionNotFoundError(f"Revision {revision} does not exist") from None
    paths = (project.pyproject_path, project.lock_path, project.python_version_path)
    for name in committed_files(project, tag_name, paths):
        content = git(
            "show", f"{tag_name}:{name}", project=project, capture_output=True
        )
        target.project_path.joinpath(name).write_text(content)


def run_steps(
    project: UVRevProject, target: UVRevProject, change: Change, dry_run: bool
) -> None:
    """Run the steps of a change. All changes run the same, so they behave the same:

    1. The files of a revision are restored (if asked for). Otherwise
       ``tool.uv.environments`` is updated, as uv might fail to resolve without.
       A restored revision keeps its environments, so it is resolved as before.
    2. The Python request is pinned with ``uv python pin`` (if given), which
       accepts versions, ranges, implementations and paths and checks them
       against ``requires-python``. uv uses the pin for the following commands.
    3. The uv commands of the change run.

    Args:
        project: Project to change.
        target: Where the steps run: the project itself, or its scratch copy
            in a dry run.
        change: The planned change.
        dry_run: Only the files may be changed, see ``dry_run_call``.
    """
    if change.restore_from is not None:
        restore_files(project, target, change.restore_from)
    else:
        update_environments(target)
    if change.python:
        uv("python", "pin", change.python, project=target)
    for call in change.uv_calls:
        uv(*(dry_run_call(call) if dry_run else call), project=target)


def preview(project: UVRevProject, change: Change) -> RevisionChange:
    """What a change would do: it runs in a scratch copy of the environment"""
    if change.is_empty:
        return no_change(project)
    with scratch_copy(project) as scratch:
        run_steps(project, scratch, change, dry_run=True)
        return preview_change(project, scratch)


def commit(project: UVRevProject, change: Change) -> RevisionChange:
    """Run a change in the environment and create a revision.

    If a command fails, the files are reset to the last revision.
    """
    if change.is_empty:
        return no_change(project)
    before = (python_request(project), get_python_version(project))
    with reset_on_failure(project), writable_venv(project):
        run_steps(project, project, change, dry_run=False)
    after = (python_request(project), get_python_version(project))
    parts = (change.message, python_message(change.python, before, after))
    # a change that only pins Python has no message of its own
    message = "; ".join(filter(None, parts)) or f"Pinned Python {change.python}"
    return create_revision(project, message)


def apply_change(
    project: UVRevProject,
    change: Change,
    dry_run: bool = False,
    verify: Verifier | None = None,
) -> Outcome:
    """Apply a planned change to the environment and create a revision.

    An environment that is marked as unprotected (``uvrev unprotect``) is
    repaired first. Afterwards the venv is verified and fixed, if asked for.

    Args:
        project: Project to change.
        change: The planned change, see the ``plan_*`` functions.
        dry_run: Only predict the change in a scratch copy: the environment is
            not touched, so nothing is repaired or verified and no revision is
            created.
        verify: Verifies and fixes the venv afterwards, None to skip.

    Returns:
        What was done.
    """
    if dry_run:
        return Outcome(preview(project, change))
    repaired = repair(project)
    revision_change = commit(project, change)
    return Outcome(revision_change, repaired, verify(project, True) if verify else None)
