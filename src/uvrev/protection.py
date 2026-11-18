"""Protect a venv against accidental changes by removing write permissions.

``chmod`` makes the venv read-only or writable. A protected venv can still be
used: only the lock file of uv and the configuration of the venv stay writable,
see ``keep_writable``. ``writable()`` is for the changes uvrev makes itself:
writable within the block, protected afterwards.

Unprotecting for a hotfix by the user, and repairing afterwards, is in
``repair.py`` and ``marker.py``.
"""

import shutil
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from . import constants
from .commands import run_cmd
from .exceptions import ChmodNotFoundError
from .settings import Settings
from .types import UVRevProject


def chmod(venv: Path, mode: str, command: str) -> None:
    """Run `<command> -R <mode> <venv>` (nothing happens if the venv does not exist).

    chmod does not follow symlinks found while traversing, so e.g. the python
    interpreter the venv links to is not changed.

    Raises:
        ChmodNotFoundError: If chmod is not installed.
    """
    if not venv.is_dir():
        return
    if shutil.which(command) is None:
        raise ChmodNotFoundError(command, constants.config_file())
    run_cmd(command, "-R", mode, venv.name, cwd=venv.parent)


def keep_writable(venv: Path) -> None:
    """Let the owner write the files of a protected venv that uv writes to use it.

    - ``.lock``: uv locks a venv before it uses it, also to only read it
      (``uv sync --check``), and opens ``.venv/.lock`` for writing to do so.
      If it can not, uv goes on without the lock (it only warns with
      ``--verbose``) and could read a venv that another uv is changing.
    - ``pyvenv.cfg``: uv rewrites it, e.g. if the interpreter changed.

    The folder stays read-only, so no file can be added, removed or renamed.
    Links are not followed.
    """
    for name in constants.WRITABLE_VENV_FILES:
        path = venv / name
        if path.is_file() and not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


def protect_venv(venv: Path, settings: Settings) -> None:
    """Remove write permissions, so the venv can not be changed by accident.

    Only the lock file of uv and the configuration of the venv stay writable for
    the owner, see ``keep_writable``.
    """
    chmod(venv, settings.protect_mode, settings.chmod_command)
    keep_writable(venv)


def unprotect_venv(venv: Path, settings: Settings) -> None:
    """Make the venv writable again"""
    chmod(venv, settings.unprotect_mode, settings.chmod_command)


@contextmanager
def writable(venv: Path, settings: Settings) -> Iterator[None]:
    """Make the venv writable within the block, if protection is enabled.

    Afterwards the venv is always protected again, even if the block failed.
    The venv may be created, recreated or deleted within the block.

    This is for changes made by uvrev itself. It never marks the environment
    as unprotected (see ``marker.py``), and keeps an existing mark as it is.
    """
    if not settings.protect:
        yield
        return
    unprotect_venv(venv, settings)
    try:
        yield
    finally:
        protect_venv(venv, settings)


def protect(project: UVRevProject) -> None:
    """Remove write permissions from the venv of the project"""
    protect_venv(project.venv_path, project.settings)


def unprotect(project: UVRevProject) -> None:
    """Make the venv of the project writable again"""
    unprotect_venv(project.venv_path, project.settings)


def writable_venv(project: UVRevProject) -> AbstractContextManager[None]:
    """Make the venv of the project writable within the block (see writable)"""
    return writable(project.venv_path, project.settings)
