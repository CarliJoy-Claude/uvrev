import os
import sys
from pathlib import Path

import click
from platformdirs import user_data_path

from uvrev.exceptions import RevisionNotFoundError

from . import constants
from .commands import uv
from .operations import (
    add_packages,
    create_env,
    list_installed_packages,
    list_projects,
    remove_packages,
    restore_revision,
    show_history,
    upgrade_packages,
)
from .terminal import success, warning
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
            warning("Currently activated VIRTUAL_ENV is not a uvrev env. Ignoring.")

    name = value or constants.DEFAULT_ENV
    proj = UVRevProject(get_base_dir() / name)

    if not proj.exists():
        raise click.BadParameter(
            f"❌ Project '{name}' does not exist. Create it with 'uvrev env create'",
        )

    return proj


def env_option(f):
    """Reusable option decorator for environment selection with project lookup"""

    return click.option(
        "--env",
        "-e",
        "env",
        default=None,
        show_default=constants.DEFAULT_ENV,
        callback=env_option_callback,
        help="Project/environment name (env: UVREV_ENV, default: base)",
    )(f)


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
@click.option("--python", default="3.12", show_default=True, help="Python version")
def env_create(name: str | None, python: str) -> None:
    """Create a new UV project with revision tracking"""
    project_name = name or constants.DEFAULT_ENV

    try:
        project = create_env(get_base_dir(), project_name, python)
        success(f"Project '{project_name}' created at {project.project_path}")
    except FileExistsError as e:
        raise click.ClickException(str(e)) from e


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
def add(env: UVRevProject, packages: tuple[str, ...]) -> None:
    """Add packages to the current project"""
    rev_num = add_packages(env, packages)
    if rev_num:
        success(f"Created revision {rev_num}")


@cli.command()
@click.argument("packages", nargs=-1, required=True)
@env_option
def remove(env: UVRevProject, packages: tuple[str, ...]) -> None:
    """Remove packages from the current project"""
    rev_num, removed_count = remove_packages(env, packages)
    if removed_count:
        success(f"Removed {removed_count} package(s), created revision {rev_num}")
    else:
        warning("No package(s) were removed")


@cli.command()
@env_option
def sync(env: UVRevProject) -> None:
    """Sync project dependencies"""
    uv("sync", "--frozen", project=env)
    success("Sync complete.")


@cli.command()
@click.argument("packages", nargs=-1)
@env_option
def upgrade(env: UVRevProject, packages: tuple[str, ...]) -> None:
    """Upgrade packages or all dependencies if no packages specified

    Examples:
        uvrev upgrade           # Upgrade all packages
        uvrev upgrade pandas    # Upgrade only pandas
    """
    rev_num = upgrade_packages(env, packages)
    if packages:
        success(f"Upgraded {len(packages)} package(s), created revision {rev_num}")
    else:
        success(f"Upgraded all packages, created revision {rev_num}")


@cli.command("list")
@env_option
def list_packages(env: UVRevProject) -> None:
    """List installed packages in the current environment"""
    all_packages = list_installed_packages(env)

    if not all_packages:
        click.echo("No packages installed")
        return

    click.echo("Installed packages:")
    for pkg, version, in_pyproject_toml in all_packages:
        click.secho(f"  {pkg}=={version}", bold=in_pyproject_toml)


@cli.command()
@env_option
def history(env: UVRevProject) -> None:
    """Show revision history for the current project"""
    output = show_history(env)

    if not output.strip():
        click.echo("No history found")
        return

    click.echo(output)


@cli.command()
@click.argument("revision", type=int)
@env_option
def restore(env: UVRevProject, revision: Revision) -> None:
    """Restore project to a specific revision"""
    try:
        restore_revision(env, revision)
        success(f"Restored to revision {revision}")
    except RevisionNotFoundError as e:
        raise click.ClickException(str(e)) from None


@cli.command()
@env_option
def path(env: UVRevProject) -> None:
    """Show the path to the current project"""
    click.echo(env.project_path)


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("run_args", nargs=-1, type=click.UNPROCESSED, required=True)
@env_option
def run(env: UVRevProject, run_args: tuple[str, ...]) -> None:
    """Run commands in the project using uv run with locked dependencies

    Examples:
        uvrev run python script.py
        uvrev run pytest
        uvrev run -- myapp --help
    """
    uv("run", "--locked", *run_args, project=env)


if __name__ == "__main__":
    cli()
