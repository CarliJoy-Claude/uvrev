import contextlib
from pathlib import Path

from packaging.utils import NormalizedName
from packaging.version import Version

from . import constants
from .commands import git, uv
from .differ import create_changelog
from .exceptions import RevisionNotFoundError
from .io import read_pyproject, write_pyproject
from .parser import parse_pyproject_deps, parse_uv_lock_packages
from .types import Revision, SubprocessFailure, UVRevProject


def create_env(base_dir: Path, name: str, python: str) -> UVRevProject:
    """Create a new UV project with revision tracking.

    Args:
        base_dir: Base directory to create project in.
        name: Name of the project.
        python: Python version to use.

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
        "--python",
        python,
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


def get_next_revision_number(project: UVRevProject) -> Revision:
    """Get the next revision number.

    Uses git tag -l to list all numeric tags, sorts them, and returns the highest + 1.

    Args:
        project: Project to get revision number for.

    Returns:
        Next revision number (0 if no revisions exist).
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

    return Revision(last_rev + 1)


def create_revision(
    project: UVRevProject, message: str = "Update packages"
) -> Revision | None:
    """Create a new revision (commit + tag).

    Args:
        project: Project to create revision in.
        message: Commit message.

    Returns:
        New revision number or None if nothing was changed
    """
    # Get previous and current state
    old_pyproject = git(
        "show", "HEAD:pyproject.toml", project=project, capture_output=True
    )
    old_lock = git("show", "HEAD:uv.lock", project=project, capture_output=True)
    current_pyproject = project.pyproject_path.read_text()
    current_lock = project.lock_path.read_text()
    if old_pyproject == current_pyproject and old_lock == current_lock:
        return None

    old_deps = parse_pyproject_deps(old_pyproject)
    old_packages = parse_uv_lock_packages(old_lock)
    new_deps = parse_pyproject_deps(current_pyproject)
    new_packages = parse_uv_lock_packages(current_lock)

    # Build changelog
    changelog = create_changelog(old_deps, new_deps, old_packages, new_packages)

    # Get next revision number (which is also the version)
    rev_num = get_next_revision_number(project)

    # Commit changes
    git("add", "pyproject.toml", "uv.lock", project=project)

    commit_msg = f"(rev {rev_num}) {message}\n{changelog}"
    git("commit", "-m", commit_msg, project=project)

    # Create lightweight tag
    git("tag", "-m", commit_msg, "--annotate", f"rev_{rev_num}", project=project)

    return Revision(rev_num)


def restore_revision(project: UVRevProject, rev_num: Revision) -> None:
    """Restore to a specific revision.

    Args:
        project: Project to restore.
        rev_num: Revision number to restore to.

    Raises:
        ValueError: If revision does not exist.
    """
    tag_name = f"rev_{rev_num}"

    # Check if tag exists
    try:
        git("rev-parse", tag_name, project=project, capture_output=True)
    except SubprocessFailure:
        raise RevisionNotFoundError(f"Revision {rev_num} does not exist") from None

    # Checkout files from that revision
    git("checkout", tag_name, "--", "pyproject.toml", "uv.lock", project=project)

    # Run uv sync
    uv("sync", "--locked", project=project)

    # Create new revision for the restore
    create_revision(project, f"Restored to revision {rev_num}")


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


def add_packages(project: UVRevProject, packages: tuple[str, ...]) -> int | None:
    """Add packages to project.

    Args:
        project: Project to add packages to.
        packages: Package names to add.

    Returns:
        New revision number.
    """
    uv("add", "--raw", *packages, project=project)
    return create_revision(project, f"Added: {', '.join(packages)}")


RemoveCount = int


def remove_packages(
    project: UVRevProject, packages: tuple[str, ...]
) -> Revision | None:
    """Remove packages from project.

    Args:
        project: Project to remove packages from.
        packages: Package names to remove.

    Returns:
        Tuple of (new_revision_number, number_of_packages_removed).
    """
    # Use uv to remove packages
    uv("remove", *packages, project=project)

    # Create revision
    rev_num = create_revision(project, f"Removed: {', '.join(packages)}")
    return rev_num


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
) -> Revision | None:
    """Upgrade packages or all if no packages specified.

    Args:
        project: Project to upgrade packages in.
        packages: Package names to upgrade (empty for all).

    Returns:
        New revision number.
    """
    if packages:
        for pkg in packages:
            uv("sync", "--upgrade-package", pkg, project=project)
        message = f"Upgraded: {', '.join(packages)}"
    else:
        uv("sync", "--upgrade", project=project)
        message = "Upgraded all packages"

    return create_revision(project, message)
