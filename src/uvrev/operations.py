"""The operations that change an existing environment: add, remove, upgrade,
restore and sync.

A change of the files is planned as data (``Change``, built by the ``plan_*``
functions) and run by ``apply_change``, so all changes behave the same and can
be shown as a dry run. ``sync_packages`` only brings the venv in line with the
files.

The functions take a ``UVRevProject`` and return what happened (``Outcome``).
They do not print reports or parse options (``cli.py``). Revisions are in
``revisions.py``, verifying and repairing a venv in ``verify.py`` and
``repair.py``.
"""

import contextlib
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from . import constants
from .commands import uv
from .differ import Diff
from .environment import update_environments
from .interpreter import get_python_version, python_message, python_request
from .marker import remove_marker
from .parser import (
    any_version,
    parse_pyproject_deps,
    parse_uv_lock_packages,
)
from .protection import unprotect, writable_venv
from .repair import RepairResult, repair
from .revisions import (
    RevisionChange,
    create_revision,
    get_last_revision_number,
    no_change,
    preview_change,
    reset_on_failure,
    restore_files,
)
from .types import Revision, UVRevProject
from .verify import Verifier, VerifyResult


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


@contextlib.contextmanager
def scratch_copy(project: UVRevProject) -> Iterator[UVRevProject]:
    """A temporary copy of the files of an environment, for dry runs.

    It has no venv and no git repository, so uv can resolve changes in it
    (without syncing) while the environment stays untouched.
    """
    with tempfile.TemporaryDirectory(prefix="uvrev-dry-run-") as directory:
        for path in project.source_paths:
            if path.exists():
                shutil.copy2(path, directory)
        yield UVRevProject(Path(directory), project.settings)


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
            {constants.PYTHON_NAME: python_before} if python_before else {},
            {constants.PYTHON_NAME: python_after} if python_after else {},
        ),
    )
    return Outcome(change, repaired, verify(project, True) if verify else None)


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
