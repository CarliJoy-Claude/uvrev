"""Run the external commands uvrev is built on: ``uv``, ``git`` and others.

The commands are shown before they run, their stdout goes to stderr (so
uvrev's stdout stays parseable) and a failure becomes a ``SubprocessFailure``.
What the commands are used for is decided by the callers.
"""

import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Protocol

import click

from . import constants
from .exceptions import CommandNotFoundError, SubprocessFailure
from .types import UVRevProject


def show_cmd(cmd: tuple[str, ...]) -> None:
    """Display command being executed"""
    click.echo(
        click.style("$ ", fg="red", bold=True)
        + click.style(shlex.join(cmd), fg="magenta"),
        err=True,
    )


@contextmanager
def needs_command(command: str, environ: Mapping[str, str]) -> Iterator[None]:
    """Raise a clear error, if the command started within the block is missing.

    Args:
        command: Name of the command.
        environ: Environment of the started process, its PATH is searched.

    Raises:
        CommandNotFoundError: If the command is not on the PATH.
    """
    try:
        yield
    except FileNotFoundError:
        # also raised for a missing working directory
        if shutil.which(command, path=environ.get("PATH")) is None:
            raise CommandNotFoundError(command) from None
        raise


@cache
def show_cwd_change(project_path: Path) -> None:
    """Show cd command once per project"""
    show_cmd(("cd", str(project_path)))


def run_in(
    cwd: Path,
    cmd: tuple[str, ...],
    env: Mapping[str, str] | None = None,
    stdout: int | None = constants.STDERR_FD,
) -> str:
    """Show a command and run it in a directory.

    Args:
        cwd: Directory to run the command in.
        cmd: Command and its arguments.
        env: Environment of the command, None for the one of uvrev.
        stdout: Where stdout of the command goes: to stderr by default,
            ``subprocess.PIPE`` to capture it, None to keep it on stdout.

    Returns:
        The captured stdout, otherwise an empty string.

    Raises:
        SubprocessFailure: If the command fails.
        CommandNotFoundError: If the command is not installed.
    """
    show_cwd_change(cwd)
    show_cmd(cmd)
    try:
        with needs_command(cmd[0], os.environ if env is None else env):
            result = subprocess.run(
                cmd, cwd=cwd, env=env, stdout=stdout, text=True, check=True
            )
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e
    return result.stdout or ""


def run_cmd(*cmd: str, cwd: Path) -> None:
    """
    Execute a command in a directory, its stdout goes to stderr

    Args:
        *cmd: Command and its arguments
        cwd: Directory to run command in
    """
    run_in(cwd, cmd)


def git(*args: str, project: UVRevProject, capture_output: bool = False) -> str:
    """
    Execute git command in project directory

    Args:
        *args: Git command arguments
        project: Project to run command in
        capture_output: If True, capture and return stdout

    Returns:
        Command stdout if capture_output=True, otherwise an empty string
    """
    stdout = subprocess.PIPE if capture_output else constants.STDERR_FD
    return run_in(project.project_path, ("git", *args), stdout=stdout)


def uv_environ(project: UVRevProject, environ: Mapping[str, str]) -> dict[str, str]:
    """Environment variables for uv commands of a project

    >>> from uvrev.settings import Settings
    >>> env = uv_environ(
    ...     UVRevProject(Path("/project"), Settings()),
    ...     {"VIRTUAL_ENV": "/home/user/.venv", "UV_PYTHON": "3.8", "KEEP_ME": "IN"},
    ... )
    >>> sorted(env.items())
    [('KEEP_ME', 'IN'), ('UV_COMPILE_BYTECODE', 'true')]
    """
    # VIRTUAL_ENV: disable warning about an activated environment
    # UV_PYTHON: the project uses the Python version pinned in .python-version,
    # otherwise uv would recreate the environment with another interpreter
    env = {k: v for k, v in environ.items() if k not in {"VIRTUAL_ENV", "UV_PYTHON"}}
    env["UV_COMPILE_BYTECODE"] = str(project.settings.compile_bytecode).lower()
    return env


def uv(
    *args: str,
    project: UVRevProject,
    keep_stdout: bool = False,
    capture_output: bool = False,
) -> str:
    """
    Execute uv command in project directory

    Args:
        *args: UV command arguments
        project: Project to run command in
        keep_stdout: If True, the command writes to stdout, otherwise to stderr
        capture_output: If True, capture and return stdout

    Returns:
        Command stdout if capture_output=True, otherwise an empty string
    """
    stdout: int | None = constants.STDERR_FD
    if capture_output:
        stdout = subprocess.PIPE
    elif keep_stdout:
        stdout = None
    env = uv_environ(project, os.environ)
    return run_in(project.project_path, ("uv", *args), env, stdout)


class UvRunner(Protocol):
    """Signature of uv_tool, so tests can give another runner"""

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str],
        stdin: str | None = None,
        capture_stdout: bool = False,
        capture_stderr: bool = False,
    ) -> str: ...


def uv_tool(
    *args: str,
    env: Mapping[str, str],
    stdin: str | None = None,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
) -> str:
    """
    Execute uv command for ``uvrev tool``, which is not bound to a project

    Args:
        *args: UV command arguments
        env: Complete environment of the uv process
        stdin: Text given to uv on stdin
        capture_stdout: If True, capture and return stdout, otherwise write to stderr
        capture_stderr: If True, capture stderr, it is still shown after uv finished
            and given in the SubprocessFailure, so the caller can inspect it

    Returns:
        Command stdout if capture_stdout=True, otherwise an empty string

    Raises:
        SubprocessFailure: If uv fails
    """
    cmd = ("uv", *args)
    show_cmd(cmd)
    stdout: int = constants.STDERR_FD
    if capture_stdout:
        stdout = subprocess.PIPE
    stderr: int | None = None
    if capture_stderr:
        stderr = subprocess.PIPE
    with needs_command("uv", env):
        result = subprocess.run(
            cmd,
            env=dict(env),
            input=stdin,
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
    if capture_stderr:
        click.echo(result.stderr, err=True, nl=False)
    if result.returncode != 0:
        raise SubprocessFailure(
            subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        )
    return result.stdout or ""
