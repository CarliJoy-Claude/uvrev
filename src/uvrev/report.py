import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Final, Literal, Self

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName
from packaging.version import Version

from .differ import Changes, Diff, Names, VersionChange
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


@dataclass(frozen=True)
class Versions:
    """Old and new values of the changed packages and dependencies"""

    packages: Mapping[NormalizedName, VersionChange]
    dependencies: Mapping[NormalizedName, VersionChange]


@dataclass(frozen=True)
class Report:
    """JSON report of a command, the field order is the order in the JSON"""

    schema_version: int
    status: Literal["success", "error"]
    command: str
    arguments: tuple[str, ...]
    revision: Revision | None
    previous_revision: Revision | None
    changes: Changes
    all_changed_packages: Names
    dependency_changes: Changes
    all_changed_dependencies: Names
    versions: Versions
    error_message: str | None

    @classmethod
    def from_change(
        cls, command: str, arguments: tuple[str, ...], change: RevisionChange
    ) -> Self:
        """Build the report of a successful command.

        Args:
            command: Executed subcommand.
            arguments: Arguments given to the subcommand.
            change: Outcome of the command.

        Examples:
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
        """
        return cls(
            schema_version=SCHEMA_VERSION,
            status="success",
            command=command,
            arguments=arguments,
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
            error_message=None,
        )

    @classmethod
    def from_error(
        cls,
        command: str,
        arguments: tuple[str, ...],
        revision: Revision | None,
        error_message: str,
    ) -> Self:
        """Build the report of a failed command.

        Args:
            command: Executed subcommand.
            arguments: Arguments given to the subcommand.
            revision: Current (unchanged) revision, None if it could not be determined.
            error_message: Error that occurred.

        Examples:
            >>> report = Report.from_error("add", ("nope",), Revision(2), "boom")
            >>> report.status, report.revision, report.previous_revision
            ('error', 2, 2)
            >>> report.error_message, report.all_changed_packages
            ('boom', ())
            >>> Report.from_error("sync", (), None, "boom").revision is None
            True
        """
        no_changes = Diff[Version]()
        return cls(
            schema_version=SCHEMA_VERSION,
            status="error",
            command=command,
            arguments=arguments,
            revision=revision,
            previous_revision=revision,
            changes=no_changes.changes(),
            all_changed_packages=(),
            dependency_changes=no_changes.changes(),
            all_changed_dependencies=(),
            versions=Versions(packages={}, dependencies={}),
            error_message=error_message,
        )

    def to_json(self) -> str:
        """Serialize the report, tuples become JSON arrays.

        >>> print(Report.from_error("sync", (), None, "boom").to_json())
        {
          "schema_version": 1,
          "status": "error",
          "command": "sync",
          "arguments": [],
          "revision": null,
          "previous_revision": null,
          "changes": {
            "added": [],
            "updated": [],
            "removed": []
          },
          "all_changed_packages": [],
          "dependency_changes": {
            "added": [],
            "updated": [],
            "removed": []
          },
          "all_changed_dependencies": [],
          "versions": {
            "packages": {},
            "dependencies": {}
          },
          "error_message": "boom"
        }
        """
        return json.dumps(asdict(self), indent=2)
