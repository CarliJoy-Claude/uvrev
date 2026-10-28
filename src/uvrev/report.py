import json
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .differ import Diff
from .types import Revision

SCHEMA_VERSION: Final[int] = 1


@dataclass(frozen=True)
class RevisionChange:
    """Outcome of a command that may change an environment"""

    previous_revision: Revision
    revision: Revision
    packages: Diff[Version] = field(default_factory=Diff)
    dependencies: Diff[SpecifierSet | None] = field(default_factory=Diff)

    @property
    def created(self) -> bool:
        """True if a new revision was created"""
        return self.revision != self.previous_revision


def build_report(
    command: str,
    arguments: tuple[str, ...],
    change: RevisionChange,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Build the JSON report of a command.

    Args:
        command: Executed subcommand.
        arguments: Arguments given to the subcommand.
        change: Outcome of the command.
        error_message: Error that occurred, marks the report as failed.

    Examples:
        >>> change = RevisionChange(
        ...     Revision(1),
        ...     Revision(2),
        ...     Diff({}, {"numpy": Version("2.1.0")}),
        ...     Diff({}, {"numpy": None}),
        ... )
        >>> report = build_report("add", ("numpy",), change)
        >>> report["status"], report["revision"], report["previous_revision"]
        ('success', 2, 1)
        >>> report["all_changed_packages"], report["all_changed_dependencies"]
        (['numpy'], ['numpy'])
        >>> report["versions"]
        {'packages': {'numpy': {'old': None, 'new': '2.1.0'}}, 'dependencies': {'numpy': {'old': None, 'new': ''}}}
    """
    status: Literal["success", "error"] = "error" if error_message else "success"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "command": command,
        "arguments": list(arguments),
        "revision": change.revision,
        "previous_revision": change.previous_revision,
        "changes": change.packages.changes(),
        "all_changed_packages": list(change.packages.all_changed),
        "dependency_changes": change.dependencies.changes(),
        "all_changed_dependencies": list(change.dependencies.all_changed),
        "versions": {
            "packages": change.packages.versions(),
            "dependencies": change.dependencies.versions(),
        },
        "error_message": error_message,
    }


def build_error_report(
    command: str,
    arguments: tuple[str, ...],
    revision: Revision | None,
    error_message: str,
) -> dict[str, Any]:
    """Build the JSON report of a failed command.

    Args:
        command: Executed subcommand.
        arguments: Arguments given to the subcommand.
        revision: Current (unchanged) revision, None if it could not be determined.
        error_message: Error that occurred.

    Examples:
        >>> report = build_error_report("add", ("nope",), Revision(2), "boom")
        >>> report["status"], report["revision"], report["previous_revision"]
        ('error', 2, 2)
        >>> report["error_message"], report["all_changed_packages"]
        ('boom', [])
        >>> build_error_report("sync", (), None, "boom")["revision"] is None
        True
    """
    report = build_report(
        command, arguments, RevisionChange(Revision(0), Revision(0)), error_message
    )
    report["revision"] = report["previous_revision"] = revision
    return report


def dump_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2)
