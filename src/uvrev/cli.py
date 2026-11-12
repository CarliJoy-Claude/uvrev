"""The command line interface: the click commands and how they report.

Every command parses its options, calls the functions of the other modules and
shows the result: messages on stderr, the JSON report (``--json``) on stdout.
``ReportingCommand`` makes sure every failure is reported as well.

Nothing here changes an environment itself, that is done by ``operations.py``,
``repair.py``, ``verify.py`` and ``tool.py``.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol

import click
from platformdirs import user_data_path
from typed_settings.exceptions import TsError

from uvrev.exceptions import RevisionNotFoundError, ToolError

from . import constants, settings as uvrev_settings
from .commands import uv
from .marker import read_marker
from .operations import (
    add_packages,
    changelog_of,
    configure_git,
    create_env,
    get_last_revision_number,
    get_python_version,
    list_installed_packages,
    list_projects,
    mark_created,
    python_request,
    remove_packages,
    restore_revision,
    show_history,
    sync_packages,
    upgrade_packages,
    upgrade_python,
)
from .parser import absolute_python_request, same_python_request
from .protection import protect as protect_venv, writable_venv
from .repair import RepairResult, mark_unprotected, repair
from .report import (
    SCHEMA_VERSION,
    EnvCreateReport,
    Option,
    ProtectReport,
    RepairSummary,
    Report,
    RevisionChange,
    VerifyReport,
    VerifySummary,
)
from .settings import Settings, load_settings
from .terminal import fatal_error, success, warning
from .tool import ToolServices, run_tool
from .types import Revision, SubprocessFailure, UVRevProject
from .verify import Problem, VerifyResult, verify


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


type Verifier = Callable[[UVRevProject, bool], VerifyResult]


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
    """Services of the cli call.

    Created when needed first (unless given as ``obj``), so a command that
    fails to load the settings can report it like any other failure.
    """
    root = ctx.find_root() if ctx else None
    if root is not None and isinstance(root.obj, Services):
        return root.obj
    services = Services(load_valid_settings())
    if root is not None:
        root.obj = services
    return services


def get_settings(ctx: click.Context | None) -> Settings:
    """Settings loaded by the cli group, or loaded now if there is no context"""
    return get_services(ctx).settings


def load_valid_settings() -> Settings:
    """Load the settings.

    Raises:
        click.ClickException: With a readable message, if they are invalid.
    """
    try:
        return load_settings()
    except TsError as e:
        details = "".join(f"\n  {sub}" for sub in getattr(e, "exceptions", ()))
        raise click.ClickException(f"Invalid settings: {e}{details}") from None


class JsonReport(Protocol):
    """A report printed with --json"""

    def to_json(self) -> str: ...


# builds the report of a failed command from its context and the error message
type ErrorReporter = Callable[[click.Context, str], JsonReport]

# key in click.Context.meta: error found while parsing the options, raised when
# the command is invoked, so it is reported like the failures of the command
DEFERRED_ERROR = "uvrev.deferred_error"

# failures with a message for the user, everything else is a bug
KNOWN_ERRORS = (click.ClickException, RevisionNotFoundError, FileExistsError)


class ReportingCommand(click.Command):
    """Command that reports every failure.

    With --json a report with status "error" is printed to stdout for every
    failure after the arguments were parsed and the exit code is 1.
    Wrong usage of the command line (exit code 2) prints no report.

    Args:
        error_report: Builds the report of a failure, for commands with --json.
        report_options: Names of the parameters shown as ``options`` in the report.
    """

    def __init__(
        self,
        *args: Any,
        error_report: ErrorReporter | None = None,
        report_options: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.error_report = error_report
        self.report_options = report_options

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return self.invoke_command(ctx)
        except KNOWN_ERRORS as e:
            self.fail(ctx, e)
        except Exception as e:
            # a bug: still report it, the traceback is shown as usual
            self.echo_error_report(ctx, f"{type(e).__name__}: {e}")
            raise

    def invoke_command(self, ctx: click.Context) -> Any:
        """Run the command, unless parsing its options found an error"""
        if deferred := ctx.meta.pop(DEFERRED_ERROR, None):
            raise deferred
        # invalid settings stop every command, also those not using them
        get_services(ctx)
        return super().invoke(ctx)

    def echo_error_report(self, ctx: click.Context, message: str) -> bool:
        """Print the report of a failure, if --json was given.

        Returns:
            True if the report was printed.
        """
        if not ctx.params.get("as_json") or self.error_report is None:
            return False
        click.echo(self.error_report(ctx, message).to_json())
        return True

    def fail(self, ctx: click.Context, e: Exception) -> NoReturn:
        if isinstance(e, click.ClickException):
            message = e.format_message()
            if not self.echo_error_report(ctx, message):
                # shown by click, with the exit code of the exception
                raise e
        else:
            message = str(e)
            self.echo_error_report(ctx, message)
        fatal_error(message)


class UvrevGroup(click.Group):
    """Group whose commands and subgroups report every failure"""

    command_class = ReportingCommand
    group_class = type


def report_arguments(ctx: click.Context) -> tuple[str, ...]:
    """The arguments (not options) given to the command, for the report"""
    values: list[str] = []
    for param in ctx.command.params:
        if not isinstance(param, click.Argument) or param.name is None:
            continue
        value = ctx.params.get(param.name)
        if isinstance(value, tuple):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return tuple(values)


def report_options(ctx: click.Context) -> Mapping[str, Option]:
    """The options of the command shown in the report"""
    names = getattr(ctx.command, "report_options", ())
    return {name: ctx.params.get(name) for name in names}


def report_command(ctx: click.Context) -> str:
    """Name of the command in the report, e.g. ``add`` or ``env create``"""
    names: list[str] = []
    current: click.Context | None = ctx
    while current is not None and current.parent is not None:
        names.append(current.info_name or "")
        current = current.parent
    return " ".join(reversed(names))


def report_header(ctx: click.Context, error_message: str | None) -> dict[str, Any]:
    """The common fields of a report, as arguments for a CommandReport"""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error" if error_message else "success",
        "command": report_command(ctx),
        "arguments": report_arguments(ctx),
        "options": report_options(ctx),
    }


def change_error_report(ctx: click.Context, message: str) -> Report:
    """Report of a failed command that changes an environment"""
    project: UVRevProject = ctx.params["project"]
    try:
        revision: Revision | None = get_last_revision_number(project)
    except (click.ClickException, OSError):
        # broken or missing environment
        revision = None
    return Report.from_error(
        report_command(ctx),
        report_arguments(ctx),
        revision,
        message,
        report_options(ctx),
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
        ctx.meta[DEFERRED_ERROR] = e
        return UVRevProject(Path(value or constants.DEFAULT_ENV))


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


def echo_problems(problems: tuple[Problem, ...], err: bool = False) -> None:
    for problem in problems:
        click.secho(f"❌ {problem}", fg="red", err=err)


def run_operation(
    project: UVRevProject,
    as_json: bool,
    verify_after: bool | None,
    operation: Callable[[], RevisionChange],
    dry_run: bool = False,
    repair_first: bool = True,
) -> RevisionChange:
    """Run an operation that changes an environment.

    An environment that is marked as unprotected (``uvrev unprotect``) is
    repaired first, unless the operation creates the venv anew anyway
    (``repair_first``) or nothing may be changed (``dry_run``).

    Afterwards the venv is verified and fixed, if requested (``--verify`` or the
    setting ``verify_after_change``), and the JSON report is printed with --json.
    Failures of the operation are reported by ReportingCommand.

    If the venv can not be fixed, the report contains the change (which is
    done) with status "error" and the program exits with exit code 1.

    In a dry run the operation only predicts the change: the venv is not
    verified and the changelog of the change is shown instead.
    """
    repaired = repair(project) if repair_first and not dry_run else None
    change = operation()
    ctx = click.get_current_context()
    if dry_run:
        verify_after = False
        click.echo(changelog_of(change), err=True)
        success("Dry run: nothing was changed.")
    elif verify_after is None:
        verify_after = project.settings.verify_after_change
    result = get_services(ctx).verify(project, True) if verify_after else None
    message = None
    if result is not None:
        # stdout may contain the JSON report
        echo_problems(result.remaining, err=True)
        if result.remaining:
            message = f"{len(result.remaining)} problem(s) could not be fixed"
    if as_json:
        report = Report.from_change(
            report_command(ctx),
            report_arguments(ctx),
            change,
            report_options(ctx),
            result,
            message,
            repaired,
        )
        click.echo(report.to_json())
    if message:
        fatal_error(message)
    return change


@click.group(cls=UvrevGroup)
@click.version_option()
def cli() -> None:
    """uvrev - UV package manager with conda-style revision tracking

    Manage UV projects with full revision history and easy rollback capabilities.
    Each package change is tracked in git with detailed changelogs.
    """


@cli.group()
def env() -> None:
    """Manage environments"""


def python_request_callback(
    _ctx: click.Context | None, _param: click.Parameter | None, value: str | None
) -> str | None:
    """A relative path of an interpreter refers to the working directory"""
    return absolute_python_request(value, Path.cwd()) if value else value


def env_create_report(
    ctx: click.Context, created: bool, error_message: str | None = None
) -> EnvCreateReport:
    project = UVRevProject(
        get_base_dir() / (ctx.params.get("name") or constants.DEFAULT_ENV)
    )
    version = get_python_version(project)
    return EnvCreateReport(
        **report_header(ctx, error_message),
        created=created,
        path=str(project.project_path),
        python=str(version) if version else None,
        python_request=python_request(project),
        error_message=error_message,
    )


def env_create_error_report(ctx: click.Context, message: str) -> EnvCreateReport:
    return env_create_report(ctx, created=False, error_message=message)


@env.command(
    "create",
    error_report=env_create_error_report,
    report_options=("python", "force"),
)
@click.argument("name", required=False)
@click.option(
    "--python",
    default=None,
    callback=python_request_callback,
    help="Python request (e.g. 3.14, '>=3.13,<3.15', cpython-3.14 or the path "
    "of an interpreter like /usr/bin/python3.14), if not given uv chooses its "
    "default interpreter",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Delete an existing folder of the environment first, "
    "if the environment was not created successfully",
)
@json_option
@click.pass_context
def env_create(
    ctx: click.Context, name: str | None, python: str | None, force: bool, as_json: bool
) -> None:
    """Create a new UV project with revision tracking

    Running it again for an environment that was created successfully does
    nothing. In this case --python is NOT applied: a warning is shown if the
    environment has another Python pinned, and --json reports the pinned request
    (python_request) and the version of its interpreter (python).

    An environment is created successfully, if it contains the file
    .uvrev_created_successfully, written as the last step.

    \b
    WARNING: --force deletes the folder of the environment if this file is
    missing. This includes environments (and their history) created by uvrev
    versions before 0.4. Run 'uvrev env fix NAME' for those first.
    """
    project_name = name or constants.DEFAULT_ENV
    project = UVRevProject(get_base_dir() / project_name)
    created = not project.created_successfully()
    if created:
        create_env(get_base_dir(), project_name, python, force, get_settings(ctx))
        success(f"Project '{project_name}' created at {project.project_path}")
    else:
        success(f"Project '{project_name}' already exists at {project.project_path}")
        pinned = python_request(project)
        if python and not same_python_request(python, pinned):
            warning(
                f"Project '{project_name}' has Python {pinned} pinned, "
                f"--python {python} is ignored. "
                f"Change it with 'uvrev upgrade-python {python}'."
            )
    if as_json:
        click.echo(env_create_report(ctx, created).to_json())


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


@cli.command(
    error_report=change_error_report,
    report_options=("upgrade_all", "python", "dry_run"),
)
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
    callback=python_request_callback,
    help="Python request for 'uv python pin' (e.g. 3.14, '>=3.13,<3.15', "
    "cpython-3.14 or the path of an interpreter like /usr/bin/python3.14), "
    "pinned before the packages are added",
)
@dry_run_option
@env_option
@json_option
@verify_option
def add(
    project: UVRevProject,
    packages: tuple[str, ...],
    upgrade_all: bool,
    python: str | None,
    dry_run: bool,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Add packages to the current project

    \b
    Examples:
        uvrev add pandas                  # Add pandas
        uvrev add --upgrade-all pandas    # Add pandas and upgrade everything
        uvrev add --python 3.14 pandas    # Add pandas and switch to Python 3.14
        uvrev add --python /usr/bin/python3.14 pandas    # ... to this interpreter
        uvrev add --dry-run pandas        # Show what adding pandas would change
    """
    change = run_operation(
        project,
        as_json,
        verify_after,
        lambda: add_packages(project, packages, upgrade_all, python, dry_run),
        dry_run,
    )
    if change.created:
        success(f"Created revision {change.revision}")


@cli.command(
    error_report=change_error_report, report_options=("ignore_missing", "dry_run")
)
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--ignore-missing",
    is_flag=True,
    default=False,
    help="Skip packages that are no dependencies instead of failing",
)
@dry_run_option
@env_option
@json_option
@verify_option
def remove(
    project: UVRevProject,
    packages: tuple[str, ...],
    ignore_missing: bool,
    dry_run: bool,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Remove packages from the current project

    \b
    Examples:
        uvrev remove pandas
        uvrev remove --ignore-missing pandas numpy   # fine if they are not installed
    """
    change = run_operation(
        project,
        as_json,
        verify_after,
        lambda: remove_packages(project, packages, ignore_missing, dry_run),
        dry_run,
    )
    if dry_run:
        return
    if change.created:
        success(f"Removed {' '.join(packages)}.  Created revision {change.revision}.")
    else:
        success("Nothing to remove.")


@cli.command(error_report=change_error_report, report_options=("clean",))
@click.option(
    "--clean",
    is_flag=True,
    default=False,
    help="Start anew: delete the complete uv cache and the virtual environment "
    "first. Everything is downloaded again, running processes lose their files.",
)
@env_option
@json_option
@verify_option
def sync(
    project: UVRevProject, clean: bool, as_json: bool, verify_after: bool | None
) -> None:
    """Sync project dependencies

    With --json the changes of the installed packages are reported.

    --clean is the way out if an environment can not be repaired: it deletes
    the cache of uv (all of it) and the virtual environment and installs
    everything again.
    """
    run_operation(
        project,
        as_json,
        verify_after,
        lambda: sync_packages(project, clean),
        repair_first=not clean,
    )
    success("Sync complete.")


@cli.command(error_report=change_error_report, report_options=("python", "dry_run"))
@click.argument("packages", nargs=-1)
@click.option(
    "--python",
    default=None,
    callback=python_request_callback,
    help="Python request for 'uv python pin' (e.g. 3.14, '>=3.13,<3.15', "
    "cpython-3.14 or the path of an interpreter like /usr/bin/python3.14), "
    "pinned before the packages are upgraded",
)
@dry_run_option
@env_option
@json_option
@verify_option
def upgrade(
    project: UVRevProject,
    packages: tuple[str, ...],
    python: str | None,
    dry_run: bool,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Upgrade packages or all dependencies if no packages specified

    \b
    Examples:
        uvrev upgrade                  # Upgrade all packages
        uvrev upgrade pandas           # Upgrade only pandas
        uvrev upgrade --python 3.14    # Upgrade all packages and switch to Python 3.14
        uvrev upgrade --python /usr/bin/python3.14    # ... to this interpreter
    """
    change = run_operation(
        project,
        as_json,
        verify_after,
        lambda: upgrade_packages(project, packages, dry_run, python),
        dry_run,
    )
    if dry_run:
        return
    if not change.created:
        success("Everything is already up to date.")
    elif packages:
        success(
            f"Upgraded {len(packages)} package(s), created revision {change.revision}"
        )
    else:
        success(f"Upgraded all packages, created revision {change.revision}")


@cli.command(
    "upgrade-python", error_report=change_error_report, report_options=("dry_run",)
)
@click.argument("request", metavar="REQUEST", callback=python_request_callback)
@dry_run_option
@env_option
@json_option
@verify_option
def upgrade_python_cmd(
    project: UVRevProject,
    request: str,
    dry_run: bool,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Change the Python of the environment without upgrading packages

    Pins REQUEST with 'uv python pin', recreates the virtual environment and
    updates the locked packages if needed. REQUEST is everything uv accepts:
    a version (3.14, 3.14.1), a range ('>=3.13,<3.15'), an implementation
    (cpython-3.14, pypy@3.11) or the path of an interpreter.
    requires-python is not changed, so downgrades work as well, as long as
    REQUEST matches requires-python.

    To also upgrade the packages, use 'uvrev upgrade --python REQUEST'.

    \b
    Examples:
        uvrev upgrade-python 3.14
        uvrev upgrade-python '>=3.13,<3.15'
        uvrev upgrade-python /usr/bin/python3.14    # path of an interpreter
    """
    change = run_operation(
        project,
        as_json,
        verify_after,
        lambda: upgrade_python(project, request, dry_run),
        dry_run,
    )
    if dry_run:
        return
    if not change.created:
        success(f"Python {request} is already used.")
    else:
        success(f"Changed Python to {request}, created revision {change.revision}")


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


@cli.command(error_report=change_error_report)
@click.argument("revision", type=int)
@env_option
@json_option
@verify_option
def restore(
    project: UVRevProject, revision: Revision, as_json: bool, verify_after: bool | None
) -> None:
    """Restore project to a specific revision"""
    run_operation(
        project, as_json, verify_after, lambda: restore_revision(project, revision)
    )
    success(f"Restored to revision {revision}")


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

    An environment that is unprotected for a hotfix ('uvrev unprotect') is
    neither synced nor repaired: the command runs with the venv as it is.

    \b
    Examples:
        uvrev run python script.py
        uvrev run pytest
        uvrev run -- myapp --help
    """
    if read_marker(project.unprotected_marker_path) is None:
        try:
            uv("sync", "--locked", "--check", project=project)
        except SubprocessFailure:
            with writable_venv(project):
                uv("sync", "--locked", project=project)
    uv("run", "--no-sync", *run_args, project=project, keep_stdout=True)


def protect_report(
    ctx: click.Context,
    changed: bool,
    protected: bool | None,
    state: str | None = None,
    repaired: RepairResult | None = None,
    error_message: str | None = None,
) -> ProtectReport:
    project: UVRevProject = ctx.params["project"]
    return ProtectReport(
        **report_header(ctx, error_message),
        changed=changed,
        protected=protected,
        path=str(project.venv_path),
        state=state,
        repair=RepairSummary.from_result(repaired),
        error_message=error_message,
    )


def protect_error_report(ctx: click.Context, message: str) -> ProtectReport:
    return protect_report(ctx, changed=False, protected=None, error_message=message)


@cli.command(error_report=protect_error_report)
@env_option
@json_option
@click.pass_context
def protect(ctx: click.Context, project: UVRevProject, as_json: bool) -> None:
    """Make the venv read-only, so it can not be changed by accident

    An environment that was unprotected with 'uvrev unprotect' is repaired
    first: afterwards its venv only contains the files of the locked packages.
    Packages with changed files are reinstalled, other files are removed.
    """
    repaired = repair(project)
    protect_venv(project)
    success(f"Protected {project.venv_path}")
    if repaired is not None:
        success(
            f"Repaired: {len(repaired.packages)} package(s) reinstalled"
            f"{' (' + ', '.join(repaired.packages) + ')' if repaired.packages else ''}"
            f", {len(repaired.removed_files)} file(s) removed"
        )
    if not project.settings.protect:
        warning(
            "Protection is disabled, so uvrev does not make the venv writable "
            "for changes. Run 'uvrev unprotect' before changing the environment."
        )
    if as_json:
        report = protect_report(
            ctx, changed=repaired is not None, protected=True, repaired=repaired
        )
        click.echo(report.to_json())


@cli.command(error_report=protect_error_report)
@env_option
@json_option
@click.pass_context
def unprotect(ctx: click.Context, project: UVRevProject, as_json: bool) -> None:
    """Make the venv writable for a hotfix

    It is fast and can not be interrupted with Ctrl+C. The environment is
    marked as unprotected (.unprotected). The next 'uvrev protect', or the
    next command that changes the environment, repairs it first: packages
    whose files were changed since now are reinstalled, and files that no
    package installed are removed. 'uvrev run' leaves it as it is.
    """
    changed, marker = mark_unprotected(project)
    if changed:
        success(f"{project.venv_path} is writable")
    else:
        success(f"{project.venv_path} is already unprotected")
    if marker.state == "forced":
        warning(
            "It is not known since when the venv is writable (an earlier "
            "'uvrev unprotect' did not finish, or .unprotected was changed). "
            "The next 'uvrev protect' has to compare every file with its package, "
            "which is slower. Run 'uvrev sync --clean' afterwards to be sure."
        )
    others = [
        name
        for name in list_projects(get_base_dir())
        if name != project.project_path.name
    ]
    if others:
        warning(
            "There are other environments: "
            f"{', '.join(sorted(others))}. Files of the same package are shared, "
            "so a file changed here is changed there as well and stays changed "
            "there. Run 'uvrev verify --fix' for them afterwards."
        )
    if project.settings.protect:
        warning("The next change of the environment repairs and protects it again.")
    else:
        warning("The next change of the environment repairs it again.")
    if as_json:
        report = protect_report(ctx, changed, protected=False, state=marker.state)
        click.echo(report.to_json())


def verify_report(
    ctx: click.Context, result: VerifyResult, error_message: str | None = None
) -> VerifyReport:
    summary = VerifySummary.from_result(result)
    return VerifyReport(
        **report_header(ctx, error_message),
        problems=summary.problems,
        remaining_problems=summary.remaining_problems,
        fixed=summary.fixed,
        error_message=error_message,
    )


def verify_error_report(ctx: click.Context, message: str) -> VerifyReport:
    return verify_report(ctx, VerifyResult(), message)


@cli.command("verify", error_report=verify_error_report, report_options=("fix",))
@click.option("--fix", is_flag=True, help="Fix the problems found")
@env_option
@json_option
@click.pass_context
def verify_cmd(
    ctx: click.Context, project: UVRevProject, fix: bool, as_json: bool
) -> None:
    """Check the venv against the RECORD files of the installed packages

    Finds missing or modified files, files that no package installed and
    packages not in sync with uv.lock. With --fix the affected packages are
    removed from the uv cache and reinstalled, files that no package installed
    are removed, and the venv is protected again (if enabled).

    The exit code is 1 if problems remain.
    """
    result = get_services(ctx).verify(project, fix)
    # stdout is reserved for the report
    echo_problems(result.remaining, err=as_json)
    message = None
    if result.remaining and fix:
        message = f"{len(result.remaining)} problem(s) could not be fixed"
    elif result.remaining:
        message = (
            f"{len(result.remaining)} problem(s) found, "
            "fix them with 'uvrev verify --fix'"
        )
    if as_json:
        click.echo(verify_report(ctx, result, message).to_json())
    if message:
        fatal_error(message)
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
