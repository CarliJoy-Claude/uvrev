from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName
from packaging.version import Version

type DiffValue = Version | SpecifierSet | None | str
type Names = tuple[NormalizedName, ...]


@dataclass(frozen=True)
class Changes:
    """Changed names grouped by kind of change"""

    added: Names
    updated: Names
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
    elif isinstance(ver, Version):
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

            >>> Diff({'pandas': '>=1.0'}, {'pandas': '>=1.0'}).lines()
            ()
        """
        changes = [f"    -{pkg}{version2str(self.old[pkg])}" for pkg in self.removed]
        changes += [f"    +{pkg}{version2str(self.new[pkg])}" for pkg in self.added]
        changes += [
            f"    ^{pkg}{version2str(self.old[pkg])} → {pkg}{version2str(self.new[pkg])}"
            for pkg in self.updated
        ]
        return tuple(sorted(changes))

    def changes(self) -> Changes:
        """Changed names grouped by kind of change.

        >>> Diff({'a': '1'}, {'b': '1'}).changes()
        Changes(added=('b',), updated=(), removed=('a',))
        """
        return Changes(added=self.added, updated=self.updated, removed=self.removed)

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
) -> str:
    """Create a formatted changelog from dependency and lockfile diffs.

    Args:
        dependencies: Diff of the dependency constraints (pyproject.toml).
        packages: Diff of the pinned package versions (uv.lock).

    Returns:
        Formatted changelog string with Dependencies and Pinned Versions sections.

    Examples:
        >>> deps = Diff({'pandas': None}, {'pandas': None, 'numpy': None})
        >>> pkgs = Diff(
        ...     {'pandas': Version('1.0.0')},
        ...     {'pandas': Version('2.0.0'), 'numpy': Version('1.24.0')},
        ... )
        >>> print(create_changelog(deps, pkgs))
        <BLANKLINE>
          Dependencies:
            +numpy
          Pinned Versions:
            +numpy==1.24.0
            ^pandas==1.0.0 → pandas==2.0.0
        <BLANKLINE>

        >>> # No changes case
        >>> print(create_changelog(Diff(), Diff()))
        (no changes)
    """
    result = ""
    if deps_diff := dependencies.lines():
        result = "\n  Dependencies:\n"
        result += "\n".join(deps_diff)
    if lock_diff := packages.lines():
        result += "\n  Pinned Versions:\n"
        result += "\n".join(lock_diff) + "\n"
    if result:
        return result
    return "(no changes)"
