import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import click
from platformdirs import user_data_path

from uvrev.exceptions import RevisionNotFoundError, ToolError

from . import constants
from .commands import uv
from .operations import (
    PYTHON_MINOR_VERSION,
    add_packages,
    create_env,
    get_last_revision_number,
    list_installed_packages,
    list_projects,
    remove_packages,
    restore_revision,
    show_history,
    sync_packages,
    upgrade_packages,
    upgrade_python,
)
from .report import Report, RevisionChange
from .terminal import fatal_error, success, warning
from .tool import run_tool
from .types import Revision, UVRevProject


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


def env_option_callback(
    _ctx: click.Context | None, _param: click.Parameter | None, value: str | None
) -> UVRevProject:
    if value is None and (venv := os.getenv("VIRTUAL_ENV")) and venv != sys.prefix:
        proj = UVRevProject(Path(venv).parent)
        if proj.exists():
            return proj
        warning(
            f"Currently activated VIRTUAL_ENV ({venv}) is not a uvrev env. Ignoring."
        )

    name = value or constants.DEFAULT_ENV
    proj = UVRevProject(get_base_dir() / name)

    if not proj.exists():
        raise click.BadParameter(
            f"❌ Project '{name}' does not exist. Create it with 'uvrev env create'",
        )

    return proj


def env_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator for environment selection with project lookup"""

    return click.option(
        "--env",
        "-e",
        "project",
        default=None,
        show_default=constants.DEFAULT_ENV,
        callback=env_option_callback,
        help="Project/environment name (env: UVREV_ENV, default: base)",
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


def run_operation(
    project: UVRevProject,
    arguments: tuple[str, ...],
    as_json: bool,
    operation: Callable[[], RevisionChange],
    options: Mapping[str, bool] | None = None,
) -> RevisionChange:
    """Run an operation and print its JSON report if requested.

    On failure the JSON report is still printed (with status "error") and the
    program exits with exit code 1.
    """
    if not as_json:
        return operation()

    command = click.get_current_context().info_name or ""
    try:
        change = operation()
    except (click.ClickException, RevisionNotFoundError) as e:
        message = e.format_message() if isinstance(e, click.ClickException) else str(e)
        try:
            revision: Revision | None = get_last_revision_number(project)
        except click.ClickException:
            revision = None
        click.echo(
            Report.from_error(command, arguments, revision, message, options).to_json()
        )
        fatal_error(message)
    click.echo(Report.from_change(command, arguments, change, options).to_json())
    return change


@click.group()
@click.version_option()
def cli() -> None:
    """uvrev - UV package manager with conda-style revision tracking

    Manage UV projects with full revision history and easy rollback capabilities.
    Each package change is tracked in git with detailed changelogs.
    """


@cli.group()
def env() -> None:
    """Manage environments"""


@env.command("create")
@click.argument("name", required=False)
@click.option(
    "--python",
    default=None,
    help="Python version, if not given uv chooses its default interpreter",
)
def env_create(name: str | None, python: str | None) -> None:
    """Create a new UV project with revision tracking"""
    project_name = name or constants.DEFAULT_ENV

    try:
        project = create_env(get_base_dir(), project_name, python)
        success(f"Project '{project_name}' created at {project.project_path}")
    except FileExistsError as e:
        fatal_error(str(e))


@env.command("list")
def env_list() -> None:
    """List all UV projects"""
    projects = list_projects(get_base_dir())

    if not projects:
        click.echo("No projects found. Create one with 'uvrev env create'")
        return

    click.echo("Projects:")
    for proj in projects:
        click.echo(f"  • {proj}")


@cli.command()
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--upgrade",
    "-U",
    is_flag=True,
    default=False,
    help="Also upgrade all other packages (like 'uvrev upgrade')",
)
@env_option
@json_option
def add(
    project: UVRevProject, packages: tuple[str, ...], upgrade: bool, as_json: bool
) -> None:
    """Add packages to the current project

    Examples:
        uvrev add pandas              # Add pandas
        uvrev add --upgrade pandas    # Add pandas and upgrade everything
    """
    change = run_operation(
        project,
        packages,
        as_json,
        lambda: add_packages(project, packages, upgrade),
        {"upgrade": upgrade},
    )
    if change.created:
        success(f"Created revision {change.revision}")


@cli.command()
@click.argument("packages", nargs=-1, required=True)
@env_option
@json_option
def remove(project: UVRevProject, packages: tuple[str, ...], as_json: bool) -> None:
    """Remove packages from the current project"""
    change = run_operation(
        project, packages, as_json, lambda: remove_packages(project, packages)
    )
    success(f"Removed {' '.join(packages)}.  Created revision {change.revision}.")


@cli.command()
@env_option
@json_option
def sync(project: UVRevProject, as_json: bool) -> None:
    """Sync project dependencies

    With --json the changes of the installed packages are reported.
    """
    run_operation(project, (), as_json, lambda: sync_packages(project))
    success("Sync complete.")


@cli.command()
@click.argument("packages", nargs=-1)
@env_option
@json_option
def upgrade(project: UVRevProject, packages: tuple[str, ...], as_json: bool) -> None:
    """Upgrade packages or all dependencies if no packages specified

    Examples:
        uvrev upgrade           # Upgrade all packages
        uvrev upgrade pandas    # Upgrade only pandas
    """
    change = run_operation(
        project, packages, as_json, lambda: upgrade_packages(project, packages)
    )
    if not change.created:
        success("Everything is already up to date.")
    elif packages:
        success(
            f"Upgraded {len(packages)} package(s), created revision {change.revision}"
        )
    else:
        success(f"Upgraded all packages, created revision {change.revision}")


def python_version_callback(
    _ctx: click.Context | None, _param: click.Parameter | None, value: str
) -> str:
    if not PYTHON_MINOR_VERSION.match(value):
        raise click.BadParameter(
            f"must be given as major.minor (e.g. 3.14), not {value!r}"
        )
    return value


@cli.command("upgrade-python")
@click.argument("version", callback=python_version_callback)
@env_option
@json_option
def upgrade_python_cmd(project: UVRevProject, version: str, as_json: bool) -> None:
    """Change the Python minor version of the environment

    Pins VERSION in .python-version, sets requires-python to it, recreates the
    virtual environment and updates the locked packages. Downgrades work as well.

    Examples:
        uvrev upgrade-python 3.14
    """
    change = run_operation(
        project, (version,), as_json, lambda: upgrade_python(project, version)
    )
    if not change.created:
        success(f"Python {version} is already used.")
    else:
        success(f"Changed Python to {version}, created revision {change.revision}")


@cli.command("list")
@env_option
def list_packages(project: UVRevProject) -> None:
    """List installed packages in the current environment"""
    all_packages = list_installed_packages(project)

    click.echo("Installed packages:")
    for pkg, version, in_pyproject_toml in all_packages:
        click.secho(
            f"  • {pkg}=={version} {'[dep]' if in_pyproject_toml else ''}",
            bold=in_pyproject_toml,
        )


@cli.command()
@env_option
def history(project: UVRevProject) -> None:
    """Show revision history for the current project"""
    output = show_history(project)
    click.echo(output)


@cli.command()
@click.argument("revision", type=int)
@env_option
@json_option
def restore(project: UVRevProject, revision: Revision, as_json: bool) -> None:
    """Restore project to a specific revision"""
    try:
        run_operation(
            project,
            (str(revision),),
            as_json,
            lambda: restore_revision(project, revision),
        )
        success(f"Restored to revision {revision}")
    except RevisionNotFoundError as e:
        fatal_error(str(e))


@cli.command()
@env_option
def path(project: UVRevProject) -> None:
    """Show the path to the current project"""
    click.echo(project.project_path)


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("run_args", nargs=-1, type=click.UNPROCESSED, required=True)
@env_option
def run(project: UVRevProject, run_args: tuple[str, ...]) -> None:
    """Run commands in the project using uv run with locked dependencies

    Examples:
        uvrev run python script.py
        uvrev run pytest
        uvrev run -- myapp --help
    """
    uv("run", "--locked", *run_args, project=project, keep_stdout=True)


@cli.group()
def tool() -> None:
    """Run tools with the dependencies locked in their release"""


@tool.command("run", context_settings={"allow_interspersed_args": False})
@click.option(
    "--from",
    "from_spec",
    metavar="SPEC",
    default=None,
    help=(
        "Package providing COMMAND: 'name', 'name==version', 'name @ URL' or the "
        "path or URL of a wheel (default: COMMAND)"
    ),
)
@click.option(
    "--extra",
    "extras",
    metavar="NAME",
    multiple=True,
    help="Install an extra of the package, can be repeated",
)
@click.option("--python", default=None, help="Python interpreter, passed to uv")
@click.option("--refresh", is_flag=True, help="Rebuild the cached environment")
@click.argument("command")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def tool_run(
    from_spec: str | None,
    extras: tuple[str, ...],
    python: str | None,
    refresh: bool,
    command: str,
    args: tuple[str, ...],
) -> None:
    """Run COMMAND like uvx, with the dependencies pinned in the release's uv.lock

    The release must be a wheel built with hatch-uv-lock, which embeds the
    uv.lock and pyproject.toml of the project. Everything after COMMAND is
    passed to it unchanged.

    \b
    Examples:
        uvrev tool run mytool --help
        uvrev tool run --from mytool==1.2 --extra fast mytool-cli -v
        uvrev tool run --from ./dist/mytool-1.2-py3-none-any.whl mytool
    """
    try:
        exit_code = run_tool(command, args, from_spec, extras, python, refresh)
    except ToolError as e:
        fatal_error(str(e))
    sys.exit(exit_code)
