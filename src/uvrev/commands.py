import os
import shlex
import subprocess
from functools import cache
from pathlib import Path
from typing import Literal, overload

import click

from uvrev.types import SubprocessFailure, UVRevProject


def show_cmd(cmd: tuple[str, ...]) -> None:
    """Display command being executed"""
    click.echo(
        click.style("$ ", fg="red", bold=True)
        + click.style(shlex.join(cmd), fg="magenta")
    )


@cache
def show_cwd_change(project_path: Path) -> None:
    """Show cd command once per project"""
    show_cmd(("cd", str(project_path)))


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

    stdout = subprocess.PIPE if capture_output else None
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


def uv(*args: str, project: UVRevProject) -> None:
    """
    Execute uv command in project directory

    Args:
        *args: UV command arguments
        project: Project to run command in
        capture_output: If True, capture and return stdout

    Returns:
        Command stdout if capture_output=True, empty string otherwise
    """
    show_cwd_change(project.project_path)
    cmd = ("uv", *args)
    show_cmd(cmd)
    env = os.environ.copy()
    if "VIRTUAL_ENV" in env:
        # disable VIRTUAL_ENV warning
        del env["VIRTUAL_ENV"]
    try:
        subprocess.run(cmd, cwd=project.project_path, check=True, env=env)
    except subprocess.CalledProcessError as e:
        raise SubprocessFailure(e) from e
