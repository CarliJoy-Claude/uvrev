"""Compare two states of names with a value (packages, dependencies, Python).

``Diff`` groups the names into added, updated, downgraded and removed,
``create_changelog`` renders diffs as the text of a revision.
Pure functions: the states are read and parsed elsewhere (``parser.py``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName
from packaging.version import Version

type DiffValue = Version | SpecifierSet | str | None
type Names = tuple[NormalizedName, ...]


@dataclass(frozen=True)
class Changes:
    """Changed names grouped by kind of change, every name is in one group only.

    ``updated`` contains upgraded versions and changed specifiers,
    ``downgraded`` the versions that decreased (never specifiers).
    """

    added: Names
    updated: Names
    downgraded: Names
    removed: Names


@dataclass(frozen=True)
class VersionChange:
    """Old and new value of a changed name (None if absent)"""

    old: str | None
    new: str | None


def version2str(ver: DiffValue) -> str:
    """
    Convert a version to a string usable in a requirement

    >>> version2str(None)
    ''
    >>> version2str(SpecifierSet(">=1.2"))
    '>=1.2'
    >>> version2str(Version("1.2"))
    '==1.2'
    """
    if ver is None:
        return ""
    if isinstance(ver, Version):
        return f"=={ver}"
    return str(ver)


def value2str(ver: DiffValue) -> str:
    """
    Convert a version or specifier to a plain string (used for JSON)

    >>> value2str(None)
    ''
    >>> value2str(SpecifierSet(">=1.2"))
    '>=1.2'
    >>> value2str(Version("1.2"))
    '1.2'
    """
    if ver is None:
        return ""
    return str(ver)


@dataclass(frozen=True)
class Diff[T: DiffValue]:
    """Difference between two package states (name -> version or specifier).

    Examples:
        >>> old = {'pandas': '>=1.0', 'numpy': ''}
        >>> new = {'pandas': '>=2.0', 'scipy': '>=1.5'}
        >>> diff = Diff(old, new)
        >>> diff.added, diff.updated, diff.removed
        (('scipy',), ('pandas',), ('numpy',))
        >>> diff.all_changed
        ('numpy', 'pandas', 'scipy')
        >>> bool(diff)
        True
        >>> bool(Diff(old, old))
        False

        The states are copied, so the diff can not be changed afterwards:

        >>> diff.old["numpy"] = ">=1"
        Traceback (most recent call last):
        ...
        TypeError: 'mappingproxy' object does not support item assignment
    """

    old: Mapping[NormalizedName, T] = field(default_factory=dict)
    new: Mapping[NormalizedName, T] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass: bypass __setattr__ to store read-only copies
        object.__setattr__(self, "old", MappingProxyType(dict(self.old)))
        object.__setattr__(self, "new", MappingProxyType(dict(self.new)))

    @property
    def added(self) -> Names:
        return tuple(sorted(pkg for pkg in self.new if pkg not in self.old))

    @property
    def removed(self) -> Names:
        return tuple(sorted(pkg for pkg in self.old if pkg not in self.new))

    @property
    def updated(self) -> Names:
        return tuple(
            sorted(
                pkg
                for pkg, ver in self.new.items()
                if pkg in self.old and self.old[pkg] != ver
            )
        )

    @property
    def downgraded(self) -> Names:
        """Updated names whose version decreased.

        Only versions (``uv.lock``) can be compared, a changed specifier
        (``pyproject.toml``) is never a downgrade.

        >>> diff = Diff(
        ...     {'a': Version('2.0'), 'b': Version('1.0'), 'c': SpecifierSet('>=2')},
        ...     {'a': Version('1.0'), 'b': Version('2.0'), 'c': SpecifierSet('>=1')},
        ... )
        >>> diff.downgraded, diff.upgraded, diff.updated
        (('a',), ('b', 'c'), ('a', 'b', 'c'))
        """
        return tuple(
            pkg
            for pkg in self.updated
            if isinstance(old := self.old[pkg], Version)
            and isinstance(new := self.new[pkg], Version)
            and new < old
        )

    @property
    def upgraded(self) -> Names:
        """Updated names that were not downgraded"""
        downgraded = set(self.downgraded)
        return tuple(pkg for pkg in self.updated if pkg not in downgraded)

    @property
    def all_changed(self) -> Names:
        return tuple(sorted((*self.added, *self.updated, *self.removed)))

    def __bool__(self) -> bool:
        return bool(self.all_changed)

    def lines(self) -> tuple[str, ...]:
        """Human readable change lines, sorted.

        Examples:
            >>> old = {'pandas': '>=1.0', 'numpy': ''}
            >>> new = {'pandas': '>=2.0', 'scipy': '>=1.5'}
            >>> for line in Diff(old, new).lines():
            ...     print(line)
                +scipy>=1.5
                -numpy
                ^pandas>=1.0 → pandas>=2.0

            Downgraded versions are marked with ``v``:

            >>> old = {'pandas': Version('2.0'), 'numpy': Version('1.0')}
            >>> new = {'pandas': Version('1.0'), 'numpy': Version('2.0')}
            >>> for line in Diff(old, new).lines():
            ...     print(line)
                ^numpy==1.0 → numpy==2.0
                vpandas==2.0 → pandas==1.0

            >>> Diff({'pandas': '>=1.0'}, {'pandas': '>=1.0'}).lines()
            ()
        """
        changes = [f"    -{pkg}{version2str(self.old[pkg])}" for pkg in self.removed]
        changes += [f"    +{pkg}{version2str(self.new[pkg])}" for pkg in self.added]
        changes += [self._update_line("^", pkg) for pkg in self.upgraded]
        changes += [self._update_line("v", pkg) for pkg in self.downgraded]
        return tuple(sorted(changes))

    def _update_line(self, marker: str, pkg: NormalizedName) -> str:
        old, new = version2str(self.old[pkg]), version2str(self.new[pkg])
        return f"    {marker}{pkg}{old} → {pkg}{new}"

    def changes(self) -> Changes:
        """Changed names grouped by kind of change.

        >>> Diff({'a': '1'}, {'b': '1'}).changes()
        Changes(added=('b',), updated=(), downgraded=(), removed=('a',))
        >>> Diff(
        ...     {'a': Version('2'), 'b': Version('1')},
        ...     {'a': Version('1'), 'b': Version('2')},
        ... ).changes()
        Changes(added=(), updated=('b',), downgraded=('a',), removed=())
        """
        return Changes(
            added=self.added,
            updated=self.upgraded,
            downgraded=self.downgraded,
            removed=self.removed,
        )

    def versions(self) -> Mapping[NormalizedName, VersionChange]:
        """Old and new value of every changed name (None if absent).

        >>> for pkg, change in Diff(
        ...     {'a': Version('1.0')}, {'a': Version('2.0'), 'b': None}
        ... ).versions().items():
        ...     print(pkg, change)
        a VersionChange(old='1.0', new='2.0')
        b VersionChange(old=None, new='')
        """
        return {
            pkg: VersionChange(
                old=value2str(self.old[pkg]) if pkg in self.old else None,
                new=value2str(self.new[pkg]) if pkg in self.new else None,
            )
            for pkg in self.all_changed
        }


def create_changelog(
    dependencies: Diff[SpecifierSet | None],
    packages: Diff[Version],
    python: Diff[Version] | None = None,
    environments: Diff[None] | None = None,
) -> str:
    """Create a formatted changelog from dependency and lockfile diffs.

    Args:
        dependencies: Diff of the dependency constraints (pyproject.toml).
        packages: Diff of the pinned package versions (uv.lock).
        python: Diff of the Python interpreter version (``python`` -> version).
        environments: Diff of the markers in ``tool.uv.environments``.

    Returns:
        Formatted changelog string with Environments, Python, Dependencies and
        Pinned Versions sections, each only if something changed.

    Examples:
        >>> deps = Diff({'pandas': None}, {'pandas': None, 'numpy': None})
        >>> pkgs = Diff(
        ...     {'pandas': Version('2.0.0'), 'six': Version('1.0')},
        ...     {'pandas': Version('1.0.0'), 'numpy': Version('1.24.0')},
        ... )
        >>> python = Diff({'python': Version('3.12.3')}, {'python': Version('3.14.0')})
        >>> print(create_changelog(deps, pkgs, python))
        <BLANKLINE>
          Python:
            ^python==3.12.3 → python==3.14.0
          Dependencies:
            +numpy
          Pinned Versions:
            +numpy==1.24.0
            -six==1.0
            vpandas==2.0.0 → pandas==1.0.0
        <BLANKLINE>

        >>> envs = Diff({"python_version == '3.12'": None},
        ...             {"python_version == '3.14'": None})
        >>> print(create_changelog(Diff(), Diff(), environments=envs))
        <BLANKLINE>
          Environments:
            +python_version == '3.14'
            -python_version == '3.12'
        <BLANKLINE>

        >>> # No changes case
        >>> print(create_changelog(Diff(), Diff()))
        (no changes)
    """
    sections: tuple[tuple[str, Diff[Any]], ...] = (
        ("Environments", environments or Diff[None]()),
        ("Python", python or Diff[Version]()),
        ("Dependencies", dependencies),
        ("Pinned Versions", packages),
    )
    result = "".join(
        f"\n  {title}:\n" + "\n".join(lines)
        for title, diff in sections
        if (lines := diff.lines())
    )
    if result:
        return result + "\n"
    return "(no changes)"
