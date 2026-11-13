"""The JSON reports printed with ``--json`` and the data they are built from.

The dataclasses define the schema (the field order is the order in the JSON),
the README documents it. They only convert results into JSON compatible values:
when a report is printed is decided in ``cli.py``.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, Self, TypedDict

from packaging.utils import NormalizedName

from . import constants
from .differ import Changes, Diff, Names, VersionChange
from .repair import RepairResult
from .revisions import RevisionChange
from .types import Revision
from .verify import Problem, VerifyResult

# value of a command line option in the report
type Option = bool | str | None


@dataclass(frozen=True)
class Versions:
    """Old and new values of the changed packages and dependencies"""

    packages: Mapping[NormalizedName, VersionChange]
    dependencies: Mapping[NormalizedName, VersionChange]


@dataclass(frozen=True)
class EnvironmentsChange:
    """Old and new markers of ``tool.uv.environments``"""

    old: tuple[str, ...]
    new: tuple[str, ...]

    @classmethod
    def from_diff(cls, diff: Diff[None]) -> Self | None:
        """None if the environments did not change.

        >>> EnvironmentsChange.from_diff(Diff({"a": None}, {"a": None, "b": None}))
        EnvironmentsChange(old=('a',), new=('a', 'b'))
        >>> EnvironmentsChange.from_diff(Diff()) is None
        True
        """
        if not diff:
            return None
        return cls(old=tuple(diff.old), new=tuple(diff.new))


@dataclass(frozen=True)
class ProblemEntry:
    """A problem found by ``uvrev verify``"""

    kind: str
    path: str
    package: str | None
    # always False: kept for the consumers of the report, there are no
    # problems anymore that are only reported
    is_warning: bool = False

    @classmethod
    def from_problem(cls, problem: Problem) -> Self:
        return cls(
            kind=problem.kind,
            path=str(problem.path),
            package=problem.package,
        )


@dataclass(frozen=True)
class VerifySummary:
    """Result of verifying a venv, as shown in the reports

    >>> from pathlib import Path
    >>> problem = Problem("modified", Path("a.py"), NormalizedName("pkg"))
    >>> summary = VerifySummary.from_result(VerifyResult((problem,), (), fixed=True))
    >>> summary.problems
    (ProblemEntry(kind='modified', path='a.py', package='pkg', is_warning=False),)
    >>> summary.remaining_problems, summary.fixed
    ((), True)
    """

    # found before fixing
    problems: tuple[ProblemEntry, ...]
    # left after fixing, the same as problems without --fix
    remaining_problems: tuple[ProblemEntry, ...]
    # True if at least one problem was repaired
    fixed: bool

    @classmethod
    def from_result(cls, result: VerifyResult) -> Self:
        return cls(
            problems=tuple(map(ProblemEntry.from_problem, result.problems)),
            remaining_problems=tuple(map(ProblemEntry.from_problem, result.remaining)),
            fixed=result.fixed,
        )


@dataclass(frozen=True)
class RepairSummary:
    """Repair of an unprotected environment, as shown in the reports

    >>> from pathlib import Path
    >>> result = RepairResult("fast", (NormalizedName("six"),), (Path("x.py"),))
    >>> RepairSummary.from_result(result)
    RepairSummary(mode='fast', packages=('six',), removed_files=('x.py',))
    >>> RepairSummary.from_result(None) is None
    True
    """

    # fast: by the files changed since unprotecting, full: by the hash of every file
    mode: str
    # reinstalled packages
    packages: tuple[str, ...]
    # removed files that no package installed
    removed_files: tuple[str, ...]

    @classmethod
    def from_result(cls, result: RepairResult | None) -> Self | None:
        if result is None:
            return None
        return cls(
            mode=result.mode,
            packages=tuple(result.packages),
            removed_files=tuple(map(str, result.removed_files)),
        )


class Header(TypedDict):
    """The fields all reports start with, as arguments for a ``CommandReport``"""

    schema_version: int
    status: Literal["success", "error"]
    command: str
    arguments: tuple[str, ...]
    options: Mapping[str, Option]


@dataclass(frozen=True)
class CommandReport:
    """JSON report of a command, the field order is the order in the JSON.

    The subclasses add the fields of their command, ``error_message`` is last.
    """

    schema_version: int
    status: Literal["success", "error"]
    command: str
    arguments: tuple[str, ...]
    options: Mapping[str, Option]

    def to_json(self) -> str:
        """Serialize the report, tuples become JSON arrays."""
        return json.dumps(asdict(self), indent=2)


@dataclass(frozen=True)
class Report(CommandReport):
    """The change report: JSON report of a command that changes an environment"""

    revision: Revision | None
    previous_revision: Revision | None
    changes: Changes
    all_changed_packages: Names
    dependency_changes: Changes
    all_changed_dependencies: Names
    versions: Versions
    # exact version of the interpreter, None if it did not change
    python: VersionChange | None
    # pinned Python request (.python-version), None if it did not change
    python_request: VersionChange | None
    environments: EnvironmentsChange | None
    # repair of the unprotected environment before the change, None if it
    # was not marked as unprotected
    repair: RepairSummary | None
    # result of verifying the venv after the change, None if it did not run
    verify: VerifySummary | None
    error_message: str | None

    @classmethod
    def from_change(
        cls,
        command: str,
        arguments: tuple[str, ...],
        change: RevisionChange,
        options: Mapping[str, Option] | None = None,
        verify: VerifyResult | None = None,
        error_message: str | None = None,
        repair: RepairResult | None = None,
    ) -> Self:
        """Build the report of a command that changed the environment.

        Args:
            command: Executed subcommand.
            arguments: Arguments given to the subcommand.
            change: Outcome of the command.
            options: Options given to the subcommand, e.g. ``{"upgrade_all": True}``.
            verify: Result of verifying (and fixing) the venv afterwards.
            repair: Repair of the unprotected environment before the change.
            error_message: Error that occurred after the change (e.g. the venv
                could not be fixed), marks the report as failed.

        Examples:
            >>> from packaging.version import Version
            >>> change = RevisionChange(
            ...     Revision(1),
            ...     Revision(2),
            ...     Diff({}, {"numpy": Version("2.1.0")}),
            ...     Diff({}, {"numpy": None}),
            ... )
            >>> report = Report.from_change("add", ("numpy",), change)
            >>> report.status, report.revision, report.previous_revision
            ('success', 2, 1)
            >>> report.all_changed_packages, report.all_changed_dependencies
            (('numpy',), ('numpy',))
            >>> report.versions.packages
            {'numpy': VersionChange(old=None, new='2.1.0')}
            >>> report.python is None
            True
            >>> report.options
            {}
            >>> Report.from_change("add", ("numpy",), change, {"upgrade_all": True}).options
            {'upgrade_all': True}
            >>> report.verify is None
            True
            >>> failed = Report.from_change(
            ...     "add", ("numpy",), change, verify=VerifyResult(), error_message="boom"
            ... )
            >>> failed.status, failed.revision, failed.verify.fixed
            ('error', 2, False)
        """
        return cls(
            schema_version=constants.SCHEMA_VERSION,
            status="error" if error_message else "success",
            command=command,
            arguments=arguments,
            options=dict(options or {}),
            revision=change.revision,
            previous_revision=change.previous_revision,
            changes=change.packages.changes(),
            all_changed_packages=change.packages.all_changed,
            dependency_changes=change.dependencies.changes(),
            all_changed_dependencies=change.dependencies.all_changed,
            versions=Versions(
                packages=change.packages.versions(),
                dependencies=change.dependencies.versions(),
            ),
            python=change.python.versions().get(constants.PYTHON_NAME),
            python_request=change.python_request.versions().get(constants.PYTHON_NAME),
            environments=EnvironmentsChange.from_diff(change.environments),
            repair=RepairSummary.from_result(repair),
            verify=VerifySummary.from_result(verify) if verify else None,
            error_message=error_message,
        )

    @classmethod
    def from_error(
        cls,
        command: str,
        arguments: tuple[str, ...],
        revision: Revision | None,
        error_message: str,
        options: Mapping[str, Option] | None = None,
    ) -> Self:
        """Build the report of a failed command.

        Args:
            command: Executed subcommand.
            arguments: Arguments given to the subcommand.
            revision: Current (unchanged) revision, None if it could not be determined.
            error_message: Error that occurred.
            options: Options given to the subcommand, e.g. ``{"upgrade_all": True}``.

        Examples:
            >>> report = Report.from_error("add", ("nope",), Revision(2), "boom")
            >>> report.status, report.revision, report.previous_revision
            ('error', 2, 2)
            >>> report.error_message, report.all_changed_packages
            ('boom', ())
            >>> Report.from_error("sync", (), None, "boom").revision is None
            True

            The complete report:

            >>> print(Report.from_error("sync", (), None, "boom").to_json())
            {
              "schema_version": 2,
              "status": "error",
              "command": "sync",
              "arguments": [],
              "options": {},
              "revision": null,
              "previous_revision": null,
              "changes": {
                "added": [],
                "updated": [],
                "downgraded": [],
                "removed": []
              },
              "all_changed_packages": [],
              "dependency_changes": {
                "added": [],
                "updated": [],
                "downgraded": [],
                "removed": []
              },
              "all_changed_dependencies": [],
              "versions": {
                "packages": {},
                "dependencies": {}
              },
              "python": null,
              "python_request": null,
              "environments": null,
              "repair": null,
              "verify": null,
              "error_message": "boom"
            }
        """
        unchanged = RevisionChange(Revision(0), Revision(0))
        report = cls.from_change(
            command, arguments, unchanged, options, error_message=error_message
        )
        return replace(report, revision=revision, previous_revision=revision)


@dataclass(frozen=True)
class EnvCreateReport(CommandReport):
    """Report of ``uvrev env create``

    >>> report = EnvCreateReport(
    ...     2, "success", "create", ("demo",), {}, True, "/envs/demo", "3.14.1", "3.14", None
    ... )
    >>> json.loads(report.to_json())["created"]
    True
    """

    # False if the environment existed already
    created: bool
    # path of the project of the environment
    path: str
    # exact version of the Python interpreter, None if unknown
    python: str | None
    # pinned Python request (.python-version), None if unknown
    python_request: str | None
    error_message: str | None


@dataclass(frozen=True)
class ProtectReport(CommandReport):
    """Report of ``uvrev protect`` and ``uvrev unprotect``"""

    # protect: True if the environment was marked as unprotected
    # unprotect: True if it was not marked as unprotected (in a defined state)
    changed: bool
    # state afterwards, None if unknown (the command failed)
    protected: bool | None
    # path of the venv
    path: str
    # unprotect: state of the mark (post-chmod or forced), otherwise None
    state: str | None
    # protect: repair of the unprotected environment, otherwise None
    repair: RepairSummary | None
    error_message: str | None


@dataclass(frozen=True)
class VerifyReport(CommandReport):
    """Report of ``uvrev verify``"""

    problems: tuple[ProblemEntry, ...]
    remaining_problems: tuple[ProblemEntry, ...]
    fixed: bool
    error_message: str | None
