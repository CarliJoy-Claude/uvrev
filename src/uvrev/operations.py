import contextlib
import json
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from importlib.metadata import distributions, version as package_version
from pathlib import Path

import click
import tomlkit
from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import InvalidVersion, Version

from . import constants
from .commands import git, uv
from .differ import Diff, create_changelog
from .exceptions import RevisionNotFoundError
from .io import read_pyproject, write_pyproject
from .parser import (
    parse_pyproject_deps,
    parse_python_lock,
    parse_uv_environments,
    parse_uv_lock_packages,
)
from .protection import unprotect, writable_venv
from .report import RevisionChange
from .settings import Settings
from .types import Revision, SubprocessFailure, UVRevProject


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
        python: Python version to use, None to let uv choose its default.
            uv pins the minor version in ``.python-version``.
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


def mark_created(project: UVRevProject) -> None:
    """Mark the environment as created successfully.

    The marker contains the uvrev version and is ignored by git
    (``.git/info/exclude``, so no tracked file is changed).
    """
    exclude = project.project_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    ignored = exclude.read_text().splitlines() if exclude.exists() else []
    if constants.CREATED_MARKER_FILE not in ignored:
        exclude.write_text("\n".join((*ignored, constants.CREATED_MARKER_FILE)) + "\n")
    project.created_marker_path.write_text(f"{package_version('uvrev')}\n")


def supported_python_versions(
    project: UVRevProject, requires_python: str
) -> tuple[str, ...]:
    """Python versions (major.minor) the environment is resolved for.

    Taken from the setting ``support_python_version`` (comma separated,
    ``UVREV_SUPPORT_PYTHON_VERSION``) or, if not set,
    from the Python versions installed for uv. Only versions matching
    ``requires-python`` are used, as uv rejects environments that don't.

    Args:
        project: Project to run uv in.
        requires_python: ``requires-python`` of the project.

    Returns:
        Sorted versions.
    """
    if configured := project.settings.support_python_version:
        try:
            versions = {Version(v) for v in configured.split(",") if v.strip()}
        except InvalidVersion as e:
            raise click.ClickException(
                "Invalid Python version in the setting support_python_version "
                f"({constants.SUPPORT_PYTHON_VERSION_ENV}={configured!r}): {e}"
            ) from e
    else:
        output = uv(
            "python",
            "list",
            "--only-installed",
            "--output-format",
            "json",
            project=project,
            capture_output=True,
        )
        installed = (Version(entry["version"]) for entry in json.loads(output))
        versions = {Version(f"{v.major}.{v.minor}") for v in installed}
    spec = SpecifierSet(requires_python)
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
    for python in (
        project.venv_path / "bin" / "python",
        project.venv_path / "Scripts" / "python.exe",
    ):
        if python.exists():
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
    return None


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

    # Get previous and current state (files missing in a revision are empty)
    paths = tracked_paths(project)
    committed = committed_files(project, "HEAD", paths)
    old = {
        path: git("show", f"HEAD:{path.name}", project=project, capture_output=True)
        if path.name in committed
        else ""
        for path in paths
    }
    current = {path: path.read_text() if path.exists() else "" for path in paths}
    if old == current:
        return RevisionChange(last_rev, last_rev)

    deps = Diff(
        parse_pyproject_deps(old[project.pyproject_path]),
        parse_pyproject_deps(current[project.pyproject_path]),
    )
    packages = Diff(
        parse_uv_lock_packages(old[project.lock_path]),
        parse_uv_lock_packages(current[project.lock_path]),
    )
    python = Diff(
        parse_python_lock(old[project.python_lock_path]),
        parse_python_lock(current[project.python_lock_path]),
    )

    environments = Diff(
        parse_uv_environments(old[project.pyproject_path]),
        parse_uv_environments(current[project.pyproject_path]),
    )

    # Build changelog
    changelog = create_changelog(deps, packages, python, environments)

    rev_num = Revision(last_rev + 1)

    # Commit changes
    git(
        "add",
        "--",
        *(path.name for path in paths if path.exists()),
        project=project,
    )

    commit_msg = f"(rev {rev_num}) {message}\n{changelog}"
    git("commit", "-m", commit_msg, project=project)

    # Create annotated tag (can be signed)
    git("tag", "-m", commit_msg, "--annotate", f"rev_{rev_num}", project=project)

    return RevisionChange(last_rev, rev_num, packages, deps, python, environments)


def restore_revision(project: UVRevProject, rev_num: Revision) -> RevisionChange:
    """Restore to a specific revision.

    Args:
        project: Project to restore.
        rev_num: Revision number to restore to.

    Returns:
        Changes compared to the previous revision.

    Raises:
        RevisionNotFoundError: If revision does not exist.
    """
    tag_name = f"rev_{rev_num}"

    # Check if tag exists
    try:
        git("rev-parse", tag_name, project=project, capture_output=True)
    except SubprocessFailure:
        raise RevisionNotFoundError(f"Revision {rev_num} does not exist") from None

    # Checkout files from that revision (the exact Python version is not
    # restored, it is recorded again from the recreated virtual environment)
    restored = committed_files(
        project,
        tag_name,
        (project.pyproject_path, project.lock_path, project.python_version_path),
    )
    git("checkout", tag_name, "--", *restored, project=project)

    # Install the restored state, this recreates the virtual environment if the
    # Python version changed. Only then the used interpreter can be compared.
    sync_or_reset(project, "--locked")

    # Create new revision for the restore
    return create_revision(project, f"Restored to revision {rev_num}")


@contextlib.contextmanager
def reset_on_failure(project: UVRevProject) -> Iterator[None]:
    """Reset the files to the last revision, if a command fails.

    Used around commands that change the files, so a failure (e.g. the Python
    version is not available) leaves no state behind that is not part of a
    revision.
    """
    try:
        yield
    except SubprocessFailure:
        changed = (
            project.pyproject_path,
            project.python_version_path,
            project.lock_path,
        )
        reset = committed_files(project, "HEAD", changed)
        git("checkout", "HEAD", "--", *reset, project=project)
        raise


def sync_or_reset(project: UVRevProject, *args: str) -> None:
    """Run ``uv sync``, on failure reset the files to the last revision.

    Args:
        project: Project to sync.
        *args: Additional arguments for ``uv sync``.
    """
    with reset_on_failure(project), writable_venv(project):
        uv("sync", *args, project=project)


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


def add_packages(
    project: UVRevProject,
    packages: tuple[str, ...],
    upgrade_all: bool = False,
    python: str | None = None,
) -> RevisionChange:
    """Add packages to project.

    If a command fails, the files are reset to the last revision.

    Args:
        project: Project to add packages to.
        packages: Requirements to add.
        upgrade_all: Run ``uv sync --upgrade`` after adding the packages.
        python: Python version for ``uv add --python``, pinned afterwards
            with ``uv python pin``.

    Returns:
        Changes compared to the previous revision.
    """
    with reset_on_failure(project), writable_venv(project):
        update_environments(project)
        uv(
            "add",
            "--raw",
            *(("--python", python) if python else ()),
            *packages,
            project=project,
        )
        message = f"Added: {', '.join(packages)}"
        if python:
            uv("python", "pin", python, project=project)
            message += f"; pinned Python {python}"
        if upgrade_all:
            uv("sync", "--upgrade", project=project)
            message += "; upgraded all packages"
    return create_revision(project, message)


def remove_packages(project: UVRevProject, packages: tuple[str, ...]) -> RevisionChange:
    """Remove packages from project.

    Args:
        project: Project to remove packages from.
        packages: Package names to remove.

    Returns:
        Changes compared to the previous revision.
    """
    with reset_on_failure(project), writable_venv(project):
        update_environments(project)
        uv("remove", *packages, project=project)
    return create_revision(project, f"Removed: {', '.join(packages)}")


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


def sync_packages(project: UVRevProject) -> RevisionChange:
    """Sync the virtual environment with the lock file.

    As the lock file is not changed, no revision is created.

    Args:
        project: Project to sync.

    Returns:
        Changes of the installed packages.
    """
    installed_before = list_venv_packages(project)
    python_before = get_python_version(project)
    with writable_venv(project):
        uv("sync", "--frozen", project=project)
    python_after = get_python_version(project)
    rev = get_last_revision_number(project)
    return RevisionChange(
        rev,
        rev,
        Diff(installed_before, list_venv_packages(project)),
        python=Diff(
            {canonicalize_name("python"): python_before} if python_before else {},
            {canonicalize_name("python"): python_after} if python_after else {},
        ),
    )


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


def upgrade_packages(
    project: UVRevProject, packages: tuple[str, ...]
) -> RevisionChange:
    """Upgrade packages or all if no packages specified.

    Args:
        project: Project to upgrade packages in.
        packages: Package names to upgrade (empty for all).

    Returns:
        Changes compared to the previous revision.
    """
    with reset_on_failure(project), writable_venv(project):
        update_environments(project)
        if packages:
            for pkg in packages:
                uv("sync", "--upgrade-package", pkg, project=project)
            message = f"Upgraded: {', '.join(packages)}"
        else:
            uv("sync", "--upgrade", project=project)
            message = "Upgraded all packages"

    return create_revision(project, message)


PYTHON_MINOR_VERSION = re.compile(r"^\d+\.\d+$")


def upgrade_python(project: UVRevProject, version: str) -> RevisionChange:
    """Change the Python minor version of the environment (e.g. 3.12 to 3.14).

    Pins the version with ``uv python pin`` (which checks it against
    ``requires-python``), recreates the virtual environment and updates ``uv.lock``.
    ``requires-python`` is not changed, so downgrades are possible as well.
    If uv fails, the files are reset to the last revision.

    Args:
        project: Project to change the Python version of.
        version: New Python version as ``major.minor``.

    Returns:
        Changes compared to the previous revision.

    Raises:
        ValueError: If the version is not given as ``major.minor``.
    """
    if not PYTHON_MINOR_VERSION.match(version):
        raise ValueError(
            f"Python version must be given as major.minor (e.g. 3.14), not {version!r}"
        )
    old = (
        project.python_version_path.read_text().strip()
        if project.python_version_path.exists()
        else ""
    )

    with reset_on_failure(project), writable_venv(project):
        update_environments(project)
        uv("python", "pin", version, project=project)
        uv("sync", project=project)

    return create_revision(project, python_change_message(old, version))


def python_change_message(old: str, new: str) -> str:
    """Revision message for a change of the Python minor version.

    >>> python_change_message("3.12", "3.14")
    'Upgraded Python: 3.12 → 3.14'
    >>> python_change_message("3.14", "3.12")
    'Downgraded Python: 3.14 → 3.12'
    >>> python_change_message("", "3.14")
    'Upgraded Python: ? → 3.14'
    """
    action = "Downgraded" if old and Version(old) > Version(new) else "Upgraded"
    return f"{action} Python: {old or '?'} → {new}"
