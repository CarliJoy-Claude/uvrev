import os
import shlex
import subprocess
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Literal, Protocol, overload

import click

from uvrev.types import SubprocessFailure, UVRevProject

# stdout of internal commands is redirected here, so uvrev's stdout stays parseable
STDERR_FD = 2


def show_cmd(cmd: tuple[str, ...]) -> None:
    """Display command being executed"""
    click.echo(
        click.style("$ ", fg="red", bold=True)
        + click.style(shlex.join(cmd), fg="magenta"),
        err=True,
    )


@cache
def show_cwd_change(project_path: Path) -> None:
    """Show cd command once per project"""
    show_cmd(("cd", str(project_path)))


def run_cmd(*cmd: str, cwd: Path) -> None:
    """
    Execute a command in a directory, its stdout goes to stderr

    Args:
        *cmd: Command and its arguments
        cwd: Directory to run command in
    """
    show_cwd_change(cwd)
    show_cmd(cmd)
    try:
        subprocess.run(cmd, cwd=cwd, stdout=STDERR_FD, check=True)
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e


@overload
def git(
    *args: str,
    project: UVRevProject,
    capture_output: Literal[False] = False,
) -> None: ...


@overload
def git(
    *args: str,
    project: UVRevProject,
    capture_output: Literal[True],
) -> str: ...
def git(
    *args: str,
    project: UVRevProject,
    capture_output: bool = False,
) -> str | None:
    """
    Execute git command in project directory

    Args:
        *args: Git command arguments
        project: Project to run command in
        capture_output: If True, capture and return stdout

    Returns:
        Command stdout if capture_output=True, empty string otherwise
    """
    show_cwd_change(project.project_path)
    cmd = ("git", *args)
    show_cmd(cmd)

    stdout = subprocess.PIPE if capture_output else STDERR_FD
    try:
        result = subprocess.run(
            cmd,
            cwd=project.project_path,
            stdout=stdout,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e
    if capture_output:
        return result.stdout
    return None


def uv_environ(project: UVRevProject, environ: Mapping[str, str]) -> dict[str, str]:
    """Environment variables for uv commands of a project

    >>> env = uv_environ(
    ...     UVRevProject(Path("/project")),
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
    show_cwd_change(project.project_path)
    cmd = ("uv", *args)
    show_cmd(cmd)
    env = uv_environ(project, os.environ)
    stdout: int | None = STDERR_FD
    if capture_output:
        stdout = subprocess.PIPE
    elif keep_stdout:
        stdout = None
    try:
        result = subprocess.run(
            cmd,
            cwd=project.project_path,
            check=True,
            env=env,
            stdout=stdout,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e
    return result.stdout if capture_output else ""


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
    stdout: int = STDERR_FD
    if capture_stdout:
        stdout = subprocess.PIPE
    stderr: int | None = None
    if capture_stderr:
        stderr = subprocess.PIPE
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
