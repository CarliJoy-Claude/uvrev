"""How the commands report: the JSON reports, failures and the result of a change.

``ReportingCommand`` is the class of all commands: it prints a report for
every failure (with ``--json``). The ``report_*`` functions build the parts
of a report from the click context, ``report_outcome`` shows what a command
did to an environment. ``Services`` holds what a cli call works with, so
tests can replace it.

The commands themselves are in ``cli.py``, the schema of the reports in
``report.py``.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

import click
from typed_settings.exceptions import TsError

from . import constants
from .operations import (
    Outcome,
)
from .report import (
    Header,
    Option,
    Report,
)
from .revisions import changelog_of, get_last_revision_number
from .settings import Settings, load_settings
from .terminal import fatal_error, success
from .tool import ToolServices
from .types import Revision, UVRevProject
from .verify import Problem, Verifier, verify


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


def get_settings_or_defaults(ctx: click.Context | None) -> Settings:
    """Settings for the report of a failure, which may be that they are invalid.

    Only for what is shown about a failed command (e.g. the path of the
    environment): the command itself always gets the loaded settings.
    """
    try:
        return get_settings(ctx)
    except click.ClickException:
        return Settings()


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
        except click.ClickException as e:
            # a failure with a message for the user
            self.fail(ctx, e)
        except Exception as e:
            # a bug: still report it, the traceback is shown as usual
            self.echo_error_report(ctx, f"{type(e).__name__}: {e}")
            raise

    def invoke_command(self, ctx: click.Context) -> Any:
        """Run the command, unless parsing its options found an error"""
        if deferred := ctx.meta.pop(constants.DEFERRED_ERROR, None):
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

    def fail(self, ctx: click.Context, e: click.ClickException) -> NoReturn:
        message = e.format_message()
        if self.echo_error_report(ctx, message):
            fatal_error(message)
        # shown by click, with the exit code of the exception
        raise e


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


def report_header(ctx: click.Context, error_message: str | None) -> Header:
    """The common fields of a report, as arguments for a CommandReport"""
    return Header(
        schema_version=constants.SCHEMA_VERSION,
        status="error" if error_message else "success",
        command=report_command(ctx),
        arguments=report_arguments(ctx),
        options=report_options(ctx),
    )


def not_fixed(problems: tuple[Problem, ...]) -> str:
    """The error message for problems of a venv that are left after fixing"""
    return f"{len(problems)} problem(s) could not be fixed"


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


def echo_problems(problems: tuple[Problem, ...], err: bool = False) -> None:
    for problem in problems:
        click.secho(f"❌ {problem}", fg="red", err=err)


def verifier(project: UVRevProject, verify_after: bool | None) -> Verifier | None:
    """The service that verifies and fixes the venv after a change.

    Args:
        project: Project with the settings.
        verify_after: Value of ``--verify`` / ``--no-verify``, None to use the
            setting ``verify_after_change``.

    Returns:
        None if the venv is not to be verified.
    """
    if verify_after is None:
        verify_after = project.settings.verify_after_change
    return get_services(click.get_current_context()).verify if verify_after else None


def report_outcome(outcome: Outcome, as_json: bool, dry_run: bool = False) -> None:
    """Show what a command did to an environment.

    The problems left after verifying the venv are shown and the JSON report
    is printed with --json. Failures of the command itself are reported by
    ReportingCommand.

    If the venv could not be fixed, the report contains the change (which is
    done) with status "error" and the program exits with exit code 1.

    In a dry run the changelog of the predicted change is shown instead.
    """
    ctx = click.get_current_context()
    if dry_run:
        click.echo(changelog_of(outcome.change), err=True)
        success("Dry run: nothing was changed.")
    message = None
    if outcome.verified is not None:
        # stdout may contain the JSON report
        echo_problems(outcome.verified.remaining, err=True)
        if outcome.verified.remaining:
            message = not_fixed(outcome.verified.remaining)
    if as_json:
        report = Report.from_change(
            report_command(ctx),
            report_arguments(ctx),
            outcome.change,
            report_options(ctx),
            outcome.verified,
            message,
            outcome.repaired,
        )
        click.echo(report.to_json())
    if message:
        fatal_error(message)
