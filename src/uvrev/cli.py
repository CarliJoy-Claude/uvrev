"""The command line interface: the click commands.

Every command parses its options, calls the functions of the other modules and
shows the result: messages on stderr, the JSON report (``--json``) on stdout.

Nothing here changes an environment itself, that is done by ``operations.py``,
``environment.py``, ``repair.py``, ``verify.py`` and ``tool.py``. The shared
options are in ``options.py``, how commands report in ``reporting.py``.
"""

import json
import sys

import click

from . import constants
from .commands import uv
from .environment import configure_git, create_env, list_projects, mark_created
from .exceptions import SubprocessFailure
from .interpreter import get_python_version, python_request
from .marker import read_marker
from .operations import (
    apply_change,
    list_installed_packages,
    plan_add,
    plan_remove,
    plan_restore,
    plan_upgrade,
    plan_upgrade_python,
    split_missing,
    sync_packages,
)
from .options import (
    dry_run_option,
    env_option,
    get_base_dir,
    json_option,
    named_project,
    python_request_callback,
    verify_option,
)
from .parser import same_python_request
from .protection import protect as protect_venv, writable_venv
from .repair import RepairResult, mark_unprotected, repair
from .report import (
    EnvCreateReport,
    ProtectReport,
    RepairSummary,
    VerifyReport,
    VerifySummary,
)
from .reporting import (
    UvrevGroup,
    change_error_report,
    echo_problems,
    get_services,
    get_settings,
    get_settings_or_defaults,
    not_fixed,
    report_header,
    report_outcome,
    verifier,
)
from .revisions import show_history
from .terminal import fatal_error, success, warning
from .tool import run_tool
from .types import Revision, UVRevProject
from .verify import VerifyResult


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


def env_create_report(
    ctx: click.Context, created: bool, error_message: str | None = None
) -> EnvCreateReport:
    # also the report of a failure, which may be that the settings are invalid
    project = UVRevProject(
        get_base_dir() / (ctx.params.get("name") or constants.DEFAULT_ENV),
        get_settings_or_defaults(ctx),
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
    project = named_project(ctx, name)
    created = not project.created_successfully()
    if created:
        create_env(get_base_dir(), project_name, project.settings, python, force)
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
    project = named_project(click.get_current_context(), name)
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
    ctx = click.get_current_context()
    for proj in projects:
        incomplete = not named_project(ctx, proj).created_successfully()
        click.echo(
            f"  • {proj}"
            + (constants.INCOMPLETE_HINT.format(name=proj) if incomplete else "")
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
    change = plan_add(packages, upgrade_all, python)
    outcome = apply_change(project, change, dry_run, verifier(project, verify_after))
    report_outcome(outcome, as_json, dry_run)
    if outcome.change.created:
        success(f"Created revision {outcome.change.revision}")


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
    if ignore_missing:
        packages, missing = split_missing(project, packages)
        if missing:
            warning(f"Not a dependency, skipped: {', '.join(missing)}")
    change = plan_remove(packages)
    outcome = apply_change(project, change, dry_run, verifier(project, verify_after))
    report_outcome(outcome, as_json, dry_run)
    if dry_run:
        return
    if outcome.change.created:
        success(
            f"Removed {' '.join(packages)}.  "
            f"Created revision {outcome.change.revision}."
        )
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
    outcome = sync_packages(project, clean, verifier(project, verify_after))
    report_outcome(outcome, as_json)
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
    change = plan_upgrade(packages, python)
    outcome = apply_change(project, change, dry_run, verifier(project, verify_after))
    report_outcome(outcome, as_json, dry_run)
    if dry_run:
        return
    revision = outcome.change.revision
    if not outcome.change.created:
        success("Everything is already up to date.")
    elif packages:
        success(f"Upgraded {len(packages)} package(s), created revision {revision}")
    else:
        success(f"Upgraded all packages, created revision {revision}")


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
    change = plan_upgrade_python(request)
    outcome = apply_change(project, change, dry_run, verifier(project, verify_after))
    report_outcome(outcome, as_json, dry_run)
    if dry_run:
        return
    if not outcome.change.created:
        success(f"Python {request} is already used.")
    else:
        success(
            f"Changed Python to {request}, created revision {outcome.change.revision}"
        )


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


@cli.command(error_report=change_error_report, report_options=("dry_run",))
@click.argument("revision", type=int)
@dry_run_option
@env_option
@json_option
@verify_option
def restore(
    project: UVRevProject,
    revision: Revision,
    dry_run: bool,
    as_json: bool,
    verify_after: bool | None,
) -> None:
    """Restore project to a specific revision

    \b
    Examples:
        uvrev restore 3
        uvrev restore --dry-run 3    # Show what restoring revision 3 would change
    """
    change = plan_restore(revision)
    outcome = apply_change(project, change, dry_run, verifier(project, verify_after))
    report_outcome(outcome, as_json, dry_run)
    if not dry_run:
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
        message = not_fixed(result.remaining)
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
    click.echo(f"# {constants.config_file()} (and files in UVREV_SETTINGS)")
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
    sys.exit(exit_code)
