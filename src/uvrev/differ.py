from collections.abc import Mapping

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName
from packaging.version import Version


def version2str(ver: Version | SpecifierSet | str | None) -> str:
    """
    Convert a version to a string

    >> version2str(None)
    ''
    >>version2str(SpecifierSet(">=1.2"))
    '>=1.2'
    >> version2str(Version("1.2"))
    '==1.2'
    """
    if ver is None:
        return ""
    elif isinstance(ver, Version):
        return f"=={ver}"
    return str(ver)


def compute_diff(
    old_items: Mapping[NormalizedName, Version | SpecifierSet | None | str],
    new_items: Mapping[NormalizedName, Version | SpecifierSet | None | str],
) -> tuple[str, ...]:
    """Compute diff between two package states.

    Args:
        old_items: Previous package state (name -> version string).
        new_items: New package state (name -> version string).

    Returns:
        Sorted list of change strings (additions, removals, updates).

    Examples:
        >>> old = {'pandas': '>=1.0', 'numpy': ''}
        >>> new = {'pandas': '>=2.0', 'scipy': '>=1.5'}
        >>> diff = compute_diff(old, new)
        >>> diff[0]  # Added scipy
        '    +scipy>=1.5'
        >>> diff[1]  # Removed numpy
        '    -numpy'
        >>> diff[2]  # Updated pandas
        '    ^pandas>=1.0 → pandas>=2.0'

        >>> # No changes case
        >>> old = {'pandas': '>=1.0'}
        >>> new = {'pandas': '>=1.0'}
        >>> compute_diff(old, new)
        ()

        >>> # Only additions
        >>> old = {}
        >>> new = {'pandas': '>=2.0', 'numpy': ''}
        >>> diff = compute_diff(old, new)
        >>> len(diff)
        2
        >>> diff[0]
        '    +numpy'
        >>> diff[1]
        '    +pandas>=2.0'
    """
    changes = []

    # Removed
    for pkg, ver in old_items.items():
        if pkg not in new_items:
            changes.append(f"    -{pkg}{version2str(ver)}")

    # Added
    for pkg, ver in new_items.items():
        if pkg not in old_items:
            changes.append(f"    +{pkg}{version2str(ver)}")

    # Updated
    for pkg, ver in new_items.items():
        if pkg in old_items and old_items[pkg] != ver:
            changes.append(
                f"    ^{pkg}{version2str(old_items[pkg])} → {pkg}{version2str(ver)}"
            )

    return tuple(sorted(changes))


def create_changelog(
    old_deps: Mapping[NormalizedName, SpecifierSet | None],
    new_deps: Mapping[NormalizedName, SpecifierSet | None],
    old_packages: Mapping[NormalizedName, Version],
    new_packages: Mapping[NormalizedName, Version],
) -> str:
    """Create a formatted changelog from dependency and lockfile diffs.

    Args:
        old_deps: Previous dependency constraints.
        new_deps: New dependency constraints.
        old_packages: Previous pinned package versions.
        new_packages: New pinned package versions.

    Returns:
        Formatted changelog string with Dependencies and Pinned Versions sections.

    Examples:
        >>> old_d = {'pandas': None}
        >>> new_d = {'pandas': None, 'numpy': None}
        >>> old_p = {'pandas': Version('1.0.0')}
        >>> new_p = {'pandas': Version('2.0.0'), 'numpy': Version('1.24.0')}
        >>> print(create_changelog(old_d, new_d, old_p, new_p))
        <BLANKLINE>
          Dependencies:
            +numpy
          Pinned Versions:
            +numpy==1.24.0
            ^pandas==1.0.0 → pandas==2.0.0
        <BLANKLINE>

        >>> # No changes case
        >>> old_d = {'pandas': None}
        >>> new_d = {'pandas': None}
        >>> old_p = {'pandas': Version('1.0.0')}
        >>> new_p = {'pandas': Version('1.0.0')}
        >>> print(create_changelog(old_d, new_d, old_p, new_p))
        (no changes)
    """
    # Convert to string representation for diff
    deps_diff = compute_diff(old_deps, new_deps)
    lock_diff = compute_diff(old_packages, new_packages)
    result = ""
    if deps_diff:
        result = "\n  Dependencies:\n"
        result += "\n".join(deps_diff)
    if lock_diff:
        result += "\n  Pinned Versions:\n"
        result += "\n".join(lock_diff) + "\n"
    if result:
        return result
    return "(no changes)"
