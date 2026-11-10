import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from platformdirs import user_data_path
from typed_settings.exceptions import TsError

from uvrev.exceptions import RevisionNotFoundError, ToolError

from . import constants, settings as uvrev_settings
from .commands import uv
from .operations import (
    PYTHON_MINOR_VERSION,
    add_packages,
    configure_git,
    create_env,
    get_last_revision_number,
    list_installed_packages,
    list_projects,
    mark_created,
    remove_packages,
    restore_revision,
    show_history,
    sync_packages,
    upgrade_packages,
    upgrade_python,
)
from .protection import (
    protect as protect_venv,
    unprotect as unprotect_venv,
    writable_venv,
)
from .report import Option, Report, RevisionChange
from .settings import Settings, load_settings
from .terminal import error, fatal_error, success, warning
from .tool import ToolServices, run_tool
from .types import Revision, SubprocessFailure, UVRevProject
from .verify import Problem, verify


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


INCOMPLETE_HINT = " (not created successfully, run 'uvrev env fix {name}' if it works)"


def warn_if_incomplete(project: UVRevProject) -> None:
    """Warn if the environment was not created successfully"""
    if not project.created_successfully():
        warning(
            f"Environment {project.project_path} was not created successfully"
            f" (missing {constants.CREATED_MARKER_FILE})."
            f" Run 'uvrev env fix {project.project_path.name}' if it works."
        )


type Verifier = Callable[[UVRevProject, bool], tuple[Problem, ...]]


@dataclass(frozen=True)
class Services:
    """State and services of a cli call

    Created by the cli group, unless given as ``obj`` (e.g. by tests).
    """

    settings: Settings
    # verify(project, fix) of the venv
    verify: Verifier = verify
    # services of uvrev tool run
    tool: ToolServices = field(default_factory=ToolServices)


def get_services(ctx: click.Context | None) -> Services:
    """Services of the cli call, or created now if there is no context"""
    services = ctx.find_object(Services) if ctx else None
    return services or Services(settings_or_exit())


def get_settings(ctx: click.Context | None) -> Settings:
    """Settings loaded by the cli group, or loaded now if there is no context"""
    return get_services(ctx).settings


def settings_or_exit() -> Settings:
    """Load the settings, exit with a readable message if they are invalid"""
    try:
        return load_settings()
    except TsError as e:
        details = "".join(f"\n  {sub}" for sub in getattr(e, "exceptions", ()))
        fatal_error(f"Invalid settings: {e}{details}")


def env_option_callback(
    ctx: click.Context | None, _param: click.Parameter | None, value: str | None
) -> UVRevProject:
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
            f"❌ Project '{name}' does not exist. Create it with 'uvrev env create'",
        )

    warn_if_incomplete(proj)
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


def verify_option[F: Callable[..., Any]](f: F) -> F:
    """Reusable option decorator to verify and fix the venv after a change"""

    return click.option(
        "--verify/--no-verify",
        "verify_after",
        default=None,
        help="Run 'uvrev verify --fix' afterwards "
        "(default: setting verify_after_change)",
    )(f)


def echo_problems(problems: tuple[Problem, ...], err: bool = False) -> None:
    for problem in problems:
        click.secho(
            f"{'⚠️' if problem.is_warning else '❌'} {problem}",
            fg="yellow" if problem.is_warning else "red",
            err=err,
        )


def fix_venv(project: UVRevProject, err: bool = False) -> None:
    """Verify and fix the venv.

    Raises:
        click.ClickException: If problems remain that could not be fixed.
    """
    problems = get_services(click.get_current_context()).verify(project, True)
    echo_problems(problems, err=err)
    if errors := [problem for problem in problems if not problem.is_warning]:
        raise click.ClickException(f"{len(errors)} problem(s) could not be fixed")


def verified(
    project: UVRevProject,
    verify_after: bool | None,
    operation: Callable[[], RevisionChange],
) -> Callable[[], RevisionChange]:
    """Extend an operation by verifying and fixing the venv afterwards (if enabled)"""
    if verify_after is None:
        verify_after = project.settings.verify_after_change
    if not verify_after:
        return operation

    def operation_and_fix() -> RevisionChange:
        change = operation()
        # stdout may contain the JSON report
        fix_venv(project, err=True)
        return change

    return operation_and_fix


def run_operation(
    project: UVRevProject,
    arguments: tuple[str, ...],
    as_json: bool,
    operation: Callable[[], RevisionChange],
    options: Mapping[str, Option] | None = None,
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
@click.pass_context
def cli(ctx: click.Context) -> None:
    """uvrev - UV package manager with conda-style revision tracking

    Manage UV projects with full revision history and easy rollback capabilities.
    Each package change is tracked in git with detailed changelogs.
    """
    if not isinstance(ctx.obj, Services):
        ctx.obj = Services(settings_or_exit())


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
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Delete an existing folder of the environment first, "
    "if the environment was not created successfully",
)
@click.pass_context
def env_create(
    ctx: click.Context, name: str | None, python: str | None, force: bool
) -> None:
    """Create a new UV project with revision tracking

    Running it again for an environment that was created successfully does
    nothing. In this case --python is NOT checked against the environment.

    An environment is created successfully, if it contains the file
    .uvrev_created_successfully, written as the last step.

    \b
    WARNING: --force deletes the folder of the environment if this file is
    missing. This includes environments (and their history) created by uvrev
    versions before 0.4. Run 'uvrev env fix NAME' for those first.
    """
    project_name = name or constants.DEFAULT_ENV
    project = UVRevProject(get_base_dir() / project_name)
    if project.created_successfully():
        success(f"Project '{project_name}' already exists at {project.project_path}")
        return

    try:
        create_env(get_base_dir(), project_name, python, force, get_settings(ctx))
        success(f"Project '{project_name}' created at {project.project_path}")
    except FileExistsError as e:
        fatal_error(str(e))


@env.command("fix")
@click.argument("name", required=False)
def env_fix(name: str | None) -> None:
    """Mark an existing environment as created successfully

    Use it for environments created by uvrev versions before 0.4, so they are
    not deleted by 'uvrev env create --force'.

    It also configures the git repository of the environment like
    'uvrev env create' does (identity of uvrev, no signing).
    """
    project_name = name or constants.DEFAULT_ENV
    project = UVRevProject(get_base_dir() / project_name)
    if not project.exists():
        fatal_error(f"Project '{project_name}' does not exist")
    if (project.project_path / ".git" / "HEAD").is_file():
        configure_git(project)
    else:
        warning(
            f"{project.project_path} is not a git repository, "
            "so revisions can not be created for this environment."
        )
    mark_created(project)
    success(f"Marked project '{project_name}' as created successfully")


@env.command("list")
def env_list() -> None:
    """List all UV projects"""
    projects = list_projects(get_base_dir())

    if not projects:
        click.echo("No projects found. Create one with 'uvrev env create'")
        return

    click.echo("Projects:")
    for proj in projects:
        incomplete = not UVRevProject(get_base_dir() / proj).created_successfully()
        click.echo(
            f"  • {proj}" + (INCOMPLETE_HINT.format(name=proj) if incomplete else "")
        )


@cli.command()
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--upgrade-all",
    is_flag=True,
    default=False,
    help="Add with 'uv add --upgrade' and run 'uv sync --upgrade' afterwards",
)
@click.option(
    "--python",
    default=None,
    help="Python version for 'uv add --python', pinned with 'uv python pin'",
)
@env_option
@json_option
@verify_option
def add(
    project: UVRevProject,
    packages: tuple[str, ...],
    upgrade_all: bool,
    python: str | None,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Add packages to the current project

    \b
    Examples:
        uvrev add pandas                  # Add pandas
        uvrev add --upgrade-all pandas    # Add pandas and upgrade everything
        uvrev add --python 3.14 pandas    # Add pandas and switch to Python 3.14
    """
    change = run_operation(
        project,
        packages,
        as_json,
        verified(
            project,
            verify_after,
            lambda: add_packages(project, packages, upgrade_all, python),
        ),
        {"upgrade_all": upgrade_all, "python": python},
    )
    if change.created:
        success(f"Created revision {change.revision}")


@cli.command()
@click.argument("packages", nargs=-1, required=True)
@env_option
@json_option
@verify_option
def remove(
    project: UVRevProject,
    packages: tuple[str, ...],
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Remove packages from the current project"""
    change = run_operation(
        project,
        packages,
        as_json,
        verified(project, verify_after, lambda: remove_packages(project, packages)),
    )
    success(f"Removed {' '.join(packages)}.  Created revision {change.revision}.")


@cli.command()
@env_option
@json_option
@verify_option
def sync(project: UVRevProject, as_json: bool, verify_after: bool | None) -> None:
    """Sync project dependencies

    With --json the changes of the installed packages are reported.
    """
    run_operation(
        project,
        (),
        as_json,
        verified(project, verify_after, lambda: sync_packages(project)),
    )
    success("Sync complete.")


@cli.command()
@click.argument("packages", nargs=-1)
@env_option
@json_option
@verify_option
def upgrade(
    project: UVRevProject,
    packages: tuple[str, ...],
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Upgrade packages or all dependencies if no packages specified

    Examples:
        uvrev upgrade           # Upgrade all packages
        uvrev upgrade pandas    # Upgrade only pandas
    """
    change = run_operation(
        project,
        packages,
        as_json,
        verified(project, verify_after, lambda: upgrade_packages(project, packages)),
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
@verify_option
def upgrade_python_cmd(
    project: UVRevProject, version: str, as_json: bool, verify_after: bool | None
) -> None:
    """Change the Python minor version of the environment

    Pins VERSION with 'uv python pin', recreates the virtual environment and
    updates the locked packages. requires-python is not changed, so downgrades
    work as well, as long as VERSION matches requires-python.

    Examples:
        uvrev upgrade-python 3.14
    """
    change = run_operation(
        project,
        (version,),
        as_json,
        verified(project, verify_after, lambda: upgrade_python(project, version)),
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
@verify_option
def restore(
    project: UVRevProject, revision: Revision, as_json: bool, verify_after: bool | None
) -> None:
    """Restore project to a specific revision"""
    try:
        run_operation(
            project,
            (str(revision),),
            as_json,
            verified(
                project, verify_after, lambda: restore_revision(project, revision)
            ),
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

    The venv is only synced (and therefore writable) before the command runs,
    the command itself runs with the protected venv.

    Examples:
        uvrev run python script.py
        uvrev run pytest
        uvrev run -- myapp --help
    """
    try:
        uv("sync", "--locked", "--check", project=project)
    except SubprocessFailure:
        with writable_venv(project):
            uv("sync", "--locked", project=project)
    uv("run", "--no-sync", *run_args, project=project, keep_stdout=True)


@cli.command()
@env_option
def protect(project: UVRevProject) -> None:
    """Make the venv read-only, so it can not be changed by accident"""
    protect_venv(project)
    success(f"Protected {project.venv_path}")
    if not project.settings.protect:
        warning(
            "Protection is disabled, so uvrev does not make the venv writable "
            "for changes. Run 'uvrev unprotect' before changing the environment."
        )


@cli.command()
@env_option
def unprotect(project: UVRevProject) -> None:
    """Make the venv writable"""
    unprotect_venv(project)
    success(f"{project.venv_path} is writable")
    if project.settings.protect:
        warning("The next change of the environment protects it again.")


@cli.command("verify")
@click.option("--fix", is_flag=True, help="Fix the problems found")
@env_option
def verify_cmd(project: UVRevProject, fix: bool) -> None:
    """Check the venv against the RECORD files of the installed packages

    Finds missing or modified files, untracked files and packages not in sync
    with uv.lock. With --fix the affected packages are reinstalled and the venv
    is protected again (if enabled), untracked files are only reported.
    """
    if fix:
        fix_venv(project)
        success("Environment is fine.")
        return
    problems = get_services(click.get_current_context()).verify(project, False)
    echo_problems(problems)
    if errors := [problem for problem in problems if not problem.is_warning]:
        error(f"{len(errors)} problem(s) found, fix them with 'uvrev verify --fix'")
        sys.exit(1)
    success("Environment is fine.")


@cli.command()
def settings() -> None:
    """Show the settings and where they are loaded from"""
    click.echo(f"# {uvrev_settings.config_file()} (and files in UVREV_SETTINGS)")
    click.echo("[uvrev]")
    for name, value in vars(get_settings(click.get_current_context())).items():
        click.echo(f"{name} = {toml_value(value)}")


def toml_value(value: object) -> str:
    """
    >>> toml_value(True), toml_value("a-w"), toml_value(("3.12", "3.14"))
    ('true', '"a-w"', '["3.12", "3.14"]')
    """
    if isinstance(value, tuple):
        value = list(value)
    return json.dumps(value)


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
        exit_code = run_tool(
            command,
            args,
            from_spec,
            extras,
            python,
            refresh,
            get_settings(click.get_current_context()),
            get_services(click.get_current_context()).tool,
        )
    except ToolError as e:
        fatal_error(str(e))
    sys.exit(exit_code)
