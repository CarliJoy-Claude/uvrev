import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from platformdirs import user_data_path

from uvrev.exceptions import RevisionNotFoundError

from . import constants
from .commands import uv
from .operations import (
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
)
from .report import Report, RevisionChange
from .terminal import fatal_error, success, warning
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
    ctx: click.Context | None, param: click.Parameter | None, value: str | None
) -> UVRevProject:
    if value is None and (venv := os.getenv("VIRTUAL_ENV")) and venv != sys.prefix:
        proj = UVRevProject(Path(venv).parent)
        if proj.exists():
            return proj
        else:
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
        click.echo(Report.from_error(command, arguments, revision, message).to_json())
        fatal_error(message)
    click.echo(Report.from_change(command, arguments, change).to_json())
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
    pass


@env.command("create")
@click.argument("name", required=False)
@click.option(
    "--python",
    default=constants.DEFAULT_PYTHON,
    show_default=True,
    help="Python version",
)
def env_create(name: str | None, python: str) -> None:
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
@env_option
@json_option
def add(project: UVRevProject, packages: tuple[str, ...], as_json: bool) -> None:
    """Add packages to the current project"""
    change = run_operation(
        project, packages, as_json, lambda: add_packages(project, packages)
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
