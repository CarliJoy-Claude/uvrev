import shutil
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import click

from . import settings as uvrev_settings
from .commands import run_cmd
from .settings import Settings
from .types import UVRevProject


class ChmodNotFoundError(click.ClickException):
    """Protection is enabled, but there is no chmod to change the permissions"""

    def __init__(self, command: str) -> None:
        super().__init__(
            f"Protecting the .venv is enabled, but the '{command}' command was not "
            "found. "
            "It is needed to change the permissions of the .venv. "
            "Install it, or disable the protection with 'protect = false' in the "
            f"[uvrev] table of {uvrev_settings.config_file()} "
            "or the environment variable UVREV_PROTECT=false."
        )


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
        raise ChmodNotFoundError(command)
    run_cmd(command, "-R", mode, venv.name, cwd=venv.parent)


def make_directories_writable(path: Path) -> None:
    """Make the folders below path (and path itself) accessible for the owner.

    Only folders are changed. Deleting a file needs no permission on the file
    itself, only on its folder. Files can be shared with other environments
    through hardlinks (and with the uv cache), their permissions must stay.
    Symlinks are not followed.
    """
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        return
    path.chmod(stat.S_IMODE(mode) | stat.S_IRWXU)
    for child in path.iterdir():
        make_directories_writable(child)


def remove_protected_tree(path: Path) -> None:
    """Delete a folder, also a protected venv (see ``make_directories_writable``).

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as directory:
    ...     venv = Path(directory, "venv")
    ...     (venv / "lib").mkdir(parents=True)
    ...     (venv / "lib" / "module.py").touch()
    ...     for item in (venv / "lib" / "module.py", venv / "lib", venv):
    ...         item.chmod(0o555 if item.is_dir() else 0o444)
    ...     remove_protected_tree(venv)
    ...     venv.exists()
    False
    """
    make_directories_writable(path)
    shutil.rmtree(path)


def protect_venv(venv: Path, settings: Settings) -> None:
    """Remove write permissions, so the venv can not be changed by accident"""
    chmod(venv, settings.protect_mode, settings.chmod_command)


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
