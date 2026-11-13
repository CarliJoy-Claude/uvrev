"""The exceptions of uvrev: failures with a message for the user.

All of them are ``click.ClickException``s, so ``ReportingCommand``
(``reporting.py``) reports them and click ends the program with exit code 1
and without a traceback. Everything else that is raised is a bug.

This module must not import anything of uvrev (``terminal`` is imported late).
"""

import shlex
import subprocess
from pathlib import Path
from typing import IO, Any

import click


class UvrevError(click.ClickException):
    """A failure found by uvrev itself, shown like its other messages"""

    def show(self, file: IO[Any] | None = None) -> None:  # noqa: ARG002
        # imported late: this module must not depend on other modules of uvrev
        from .terminal import error  # noqa: PLC0415

        # like click, but in the style of uvrev: always to stderr
        error(self.format_message())


class RevisionNotFoundError(UvrevError):
    """The revision to restore does not exist"""


class EnvironmentExistsError(UvrevError):
    """The folder of an environment exists, but it was not created successfully"""


class ToolError(UvrevError):
    """A tool can not be installed or run by ``uvrev tool run``"""


class SubprocessFailure(click.ClickException):
    """
    SubprocessFailure Exception that will quit the program without stacktrace
    """

    def __init__(self, error: subprocess.CalledProcessError) -> None:
        if isinstance(error.cmd, tuple):  # pragma: no branch
            error.cmd = shlex.join(error.cmd)
        self.error = error
        super().__init__(str(error))


class CommandNotFoundError(click.ClickException):
    """A command uvrev needs (uv, git) is not installed"""

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(
            f"The '{command}' command was not found. "
            f"uvrev needs {command}, install it and make sure it is on the PATH."
        )


class ChmodNotFoundError(click.ClickException):
    """Protection is enabled, but there is no chmod to change the permissions"""

    def __init__(self, command: str, config_file: Path) -> None:
        super().__init__(
            f"Protecting the .venv is enabled, but the '{command}' command was not "
            "found. "
            "It is needed to change the permissions of the .venv. "
            "Install it, or disable the protection with 'protect = false' in the "
            f"[uvrev] table of {config_file} "
            "or the environment variable UVREV_PROTECT=false."
        )


class VenvChangedError(click.ClickException):
    """A file of the venv itself was changed, it can not be repaired in place"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"{path} was changed or is missing. It belongs to the virtual "
            "environment itself and is not part of a package, so it can not be "
            "restored. Run 'uvrev sync --clean' to create the environment anew."
        )
