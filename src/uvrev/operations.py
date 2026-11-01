import contextlib
import re
import subprocess
from collections.abc import Mapping
from importlib.metadata import distributions
from pathlib import Path

from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from . import constants
from .commands import git, uv
from .differ import Diff, create_changelog
from .exceptions import RevisionNotFoundError
from .io import read_pyproject, write_pyproject
from .parser import parse_pyproject_deps, parse_python_lock, parse_uv_lock_packages
from .report import RevisionChange
from .types import Revision, SubprocessFailure, UVRevProject


def create_env(base_dir: Path, name: str, python: str | None = None) -> UVRevProject:
    """Create a new UV project with revision tracking.

    Args:
        base_dir: Base directory to create project in.
        name: Name of the project.
        python: Python version to use, None to let uv choose its default.
            uv pins the minor version in ``.python-version``.

    Returns:
        Created UVRevProject instance.

    Raises:
        ValueError: If project already exists.
    """
    target = base_dir.joinpath(name)
    if target.exists():
        raise FileExistsError(f"Folder for project '{name}' ({target}) already exists.")
    project = UVRevProject(target)
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
    uv("sync", project=project)
    update_python_lock(project)

    # Create initial revision
    git("add", ".", project=project)
    git("commit", "-m", constants.INITIAL_COMMIT_MSG, project=project)
    git("tag", "rev_0", project=project)

    return project


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

    # Build changelog
    changelog = create_changelog(deps, packages, python)

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

    return RevisionChange(last_rev, rev_num, packages, deps, python)


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


def sync_or_reset(project: UVRevProject, *args: str) -> None:
    """Run ``uv sync``, on failure reset the files to the last revision.

    Used after the files were changed (restore, Python version change), so a
    failure (e.g. the Python version is not available) leaves no state behind
    that does not match the virtual environment, which uv did not change.

    Args:
        project: Project to sync.
        *args: Additional arguments for ``uv sync``.
    """
    try:
        uv("sync", *args, project=project)
    except SubprocessFailure:
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


def add_packages(project: UVRevProject, packages: tuple[str, ...]) -> RevisionChange:
    """Add packages to project.

    Args:
        project: Project to add packages to.
        packages: Package names to add.

    Returns:
        Changes compared to the previous revision.
    """
    uv("add", "--raw", *packages, project=project)
    return create_revision(project, f"Added: {', '.join(packages)}")


def remove_packages(project: UVRevProject, packages: tuple[str, ...]) -> RevisionChange:
    """Remove packages from project.

    Args:
        project: Project to remove packages from.
        packages: Package names to remove.

    Returns:
        Changes compared to the previous revision.
    """
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
    venv = project.project_path / ".venv"
    site_packages = (
        *venv.glob("lib*/python*/site-packages"),
        *venv.glob("Lib/site-packages"),
    )
    return {
        canonicalize_name(dist.metadata["Name"]): Version(dist.version)
        for dist in distributions(path=[str(path) for path in site_packages])
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

    Pins the version in ``.python-version``, sets ``requires-python`` to it,
    recreates the virtual environment and updates ``uv.lock``.
    Downgrades are possible as well.
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

    data = read_pyproject(project)
    data.setdefault("project", {})["requires-python"] = f">={version}"
    write_pyproject(project, data)
    project.python_version_path.write_text(f"{version}\n")
    sync_or_reset(project)

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
