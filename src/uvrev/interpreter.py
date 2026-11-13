"""The Python of an environment: the pinned request and the interpreter used.

``.python-version`` holds the *request* given to ``uv python pin`` (a version,
range, implementation or path), ``.python-version.lock`` the exact version of
the interpreter of the venv, which is asked directly.

Pinning a request is a step of a change (``operations.py``).
"""

import subprocess
from pathlib import Path

from packaging.version import Version

from . import constants
from .commands import uv
from .exceptions import SubprocessFailure
from .types import UVRevProject


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
            (str(python), "-c", constants.PRINT_PYTHON_VERSION),
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
    if python_request(project) == python_request(scratch):
        return recorded
    try:
        python = uv("python", "find", project=scratch, capture_output=True).strip()
    except SubprocessFailure:
        return recorded
    return f"{interpreter_version(Path(python))}\n"
