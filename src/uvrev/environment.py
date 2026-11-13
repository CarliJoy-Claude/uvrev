"""Create environments and keep their configuration up to date.

An environment is a uv project (``package = false``) in its own git repository,
resolved for the supported Python versions (``tool.uv.environments``).

Changing the packages of an existing environment is in ``operations.py``.
"""

import getpass
import platform
import shutil
import socket
import sys
from collections.abc import Callable
from importlib.metadata import version as package_version
from pathlib import Path

import tomlkit
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from . import constants
from .commands import git, uv
from .exceptions import EnvironmentExistsError
from .interpreter import update_python_lock
from .io import exclude_from_git, read_pyproject, write_pyproject
from .parser import (
    parse_uv_environments,
)
from .protection import unprotect, writable_venv
from .settings import Settings
from .types import UVRevProject


def create_env(
    base_dir: Path,
    name: str,
    settings: Settings,
    python: str | None = None,
    force: bool = False,
) -> UVRevProject:
    """Create a new UV project with revision tracking.

    Creating an environment that was created successfully before does nothing,
    the Python version is not checked in this case.

    Args:
        base_dir: Base directory to create project in.
        name: Name of the project.
        settings: Settings of uvrev.
        python: Python request (a version, range, implementation or the path
            of an interpreter), None to let uv choose its default.
            uv pins it in ``.python-version``.
        force: Delete the folder of the environment first, if it was not
            created successfully (no ``.uvrev_created_successfully``).

    Returns:
        Created UVRevProject instance.

    Raises:
        EnvironmentExistsError: If the folder exists but the environment was not
            created successfully and ``force`` is not given.
    """
    target = base_dir.joinpath(name)
    project = UVRevProject(target, settings)
    if project.created_successfully():
        return project
    if target.exists():
        if not force:
            raise EnvironmentExistsError(
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
        if p.is_dir() and (p / constants.PYPROJECT_FILE).exists()
    )
