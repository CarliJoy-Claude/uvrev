"""The options shared by the commands and how their values are found.

Most important is ``--env``: which environment a command works on
(``find_project``). Errors found here are stored and raised when the command
is invoked, so they are reported like its other failures (``reporting.py``).
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from platformdirs import user_data_path

from . import constants
from .parser import absolute_python_request
from .reporting import get_settings, get_settings_or_defaults
from .terminal import warning
from .types import UVRevProject


def get_base_dir() -> Path:
    """Get the base directory for uvrev projects.

    Returns:
        Path UVREV_BASE or user data path for uvrev
    """
    if custom_base := os.getenv("UVREV_BASE"):
        base = Path(custom_base)
    else:
        base = user_data_path("uvrev")
    base.mkdir(parents=True, exist_ok=True)
    return base


def warn_if_incomplete(project: UVRevProject) -> None:
    """Warn if the environment was not created successfully"""
    if not project.created_successfully():
        warning(
            f"Environment {project.project_path} was not created successfully"
            f" (missing {constants.CREATED_MARKER_FILE})."
            f" Run 'uvrev env fix {project.project_path.name}' if it works."
        )


def named_project(ctx: click.Context | None, name: str | None) -> UVRevProject:
    """The environment with the given name (default: ``base``), existing or not"""
    return UVRevProject(
        get_base_dir() / (name or constants.DEFAULT_ENV), get_settings(ctx)
    )


def find_project(
    ctx: click.Context | None, param: click.Parameter | None, value: str | None
) -> UVRevProject:
    """Find the environment for --env (see README, Environment Selection)"""
    settings = get_settings(ctx)
    if value is None and (venv := os.getenv("VIRTUAL_ENV")) and venv != sys.prefix:
        proj = UVRevProject(Path(venv).parent, settings)
        if proj.exists():
            warn_if_incomplete(proj)
            return proj
        warning(
            f"Currently activated VIRTUAL_ENV ({venv}) is not a uvrev env. Ignoring."
        )

    name = value or constants.DEFAULT_ENV
    proj = UVRevProject(get_base_dir() / name, settings)

    if not proj.exists():
        raise click.BadParameter(
            f"Project '{name}' does not exist. Create it with 'uvrev env create'",
            ctx=ctx,
            param=param,
        )

    warn_if_incomplete(proj)
    return proj


def env_option_callback(
    ctx: click.Context | None, param: click.Parameter | None, value: str | None
) -> UVRevProject:
    try:
        return find_project(ctx, param, value)
    except click.ClickException as e:
        if ctx is None:
            raise
        # raised by ReportingCommand.invoke, the command is not executed
        ctx.meta[constants.DEFERRED_ERROR] = e
        # a placeholder: the command is not executed, only its failure reported
        return UVRevProject(
            Path(value or constants.DEFAULT_ENV), get_settings_or_defaults(ctx)
        )


def env_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator for environment selection with project lookup"""

    return click.option(
        "--env",
        "-e",
        "project",
        default=None,
        envvar=constants.ENV_VARIABLE,
        callback=env_option_callback,
        help="Project/environment name (default: environment variable UVREV_ENV, "
        "then the activated uvrev environment, then 'base')",
    )(f)


def json_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator to print a JSON report of the changes"""

    return click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Print a JSON report of the changes to stdout, logs go to stderr",
    )(f)


def verify_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator to verify and fix the venv after a change"""

    return click.option(
        "--verify/--no-verify",
        "verify_after",
        default=None,
        help="Run 'uvrev verify --fix' afterwards "
        "(default: setting verify_after_change)",
    )(f)


def dry_run_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator to only report what a command would change"""

    return click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Only report what would change: the environment, its files and "
        "its revisions are not touched",
    )(f)


def python_request_callback(
    _ctx: click.Context | None, _param: click.Parameter | None, value: str | None
) -> str | None:
    """A relative path of an interpreter refers to the working directory"""
    return absolute_python_request(value, Path.cwd()) if value else value
