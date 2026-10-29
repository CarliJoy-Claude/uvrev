import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from . import settings
from .commands import run_cmd
from .types import UVRevProject


class ChmodNotFoundError(click.ClickException):
    """Protection is enabled, but there is no chmod to change the permissions"""

    def __init__(self) -> None:
        super().__init__(
            "Protecting the .venv is enabled, but the 'chmod' command was not found. "
            "It is needed to change the permissions of the .venv. "
            "Install it, or disable the protection with 'protect = false' in the "
            f"[uvrev] table of {settings.default_config_file()} "
            "or the environment variable UVREV_PROTECT=false."
        )


def chmod(venv: Path, mode: str, project: UVRevProject) -> None:
    """Run `chmod -R <mode> <venv>` (nothing happens if the venv does not exist).

    chmod does not follow symlinks found while traversing, so e.g. the python
    interpreter the venv links to is not changed.

    Raises:
        ChmodNotFoundError: If chmod is not installed.
    """
    if not venv.is_dir():
        return
    if shutil.which("chmod") is None:
        raise ChmodNotFoundError()
    run_cmd("chmod", "-R", mode, venv.name, project=project)


def protect(project: UVRevProject) -> None:
    """Remove write permissions, so the venv can not be changed by accident"""
    chmod(project.venv_path, project.settings.protect_mode, project)


def unprotect(project: UVRevProject) -> None:
    """Make the venv writable again"""
    chmod(project.venv_path, project.settings.unprotect_mode, project)


@contextmanager
def writable_venv(project: UVRevProject) -> Iterator[None]:
    """Make the venv writable within the block, if protection is enabled.

    Afterwards the venv is always protected again, even if the block failed.
    """
    if not project.settings.protect:
        yield
        return
    unprotect(project)
    try:
        yield
    finally:
        protect(project)
