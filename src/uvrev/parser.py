import tomllib
from collections.abc import Mapping
from typing import Any, Final

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from .types import NormalizedRequirement

# "any version": how a requirement without version is given to uv, as uv keeps
# an existing constraint for a bare name. It is the same as no constraint.
ANY_VERSION: Final[str] = ">=0"
# a file name like pkg-1.0-py3-none-any.whl parses as requirement, but is a path
ARCHIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".whl",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
)


def any_version(requirement: str) -> str:
    """Add ``>=0`` to a requirement without version specifier.

    Extras and markers are kept. Everything else is returned unchanged:
    requirements with a specifier or URL, paths and unparsable input.

    >>> any_version("six")
    'six>=0'
    >>> any_version("httpie[socks]")
    'httpie[socks]>=0'
    >>> any_version("six; python_version < '3.13'")
    "six>=0; python_version < '3.13'"
    >>> any_version("six==1.15.0"), any_version("six>=0")
    ('six==1.15.0', 'six>=0')
    >>> any_version("six @ https://example.com/six-1.0-py3-none-any.whl")
    'six @ https://example.com/six-1.0-py3-none-any.whl'
    >>> any_version("./six"), any_version("six-1.0-py3-none-any.whl")
    ('./six', 'six-1.0-py3-none-any.whl')
    >>> any_version("git+https://example.com/six.git")
    'git+https://example.com/six.git'
    """
    if requirement.lower().endswith(ARCHIVE_SUFFIXES):
        return requirement
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return requirement
    if parsed.specifier or parsed.url:
        return requirement
    # the text is changed instead of str(parsed), to keep it as the user wrote it
    name, separator, marker = requirement.partition(";")
    return f"{name.strip()}{ANY_VERSION}" + (f"; {marker.strip()}" if separator else "")


def _canonical_pyproject(toml_content: str) -> Mapping[str, Any]:
    """pyproject.toml with ``name>=0`` dependencies written as ``name``"""
    data = tomllib.loads(toml_content)
    dependencies = data.get("project", {}).get("dependencies")
    if dependencies is not None:
        canonical = []
        for dependency in dependencies:
            requirement = Requirement(dependency)
            if str(requirement.specifier) == ANY_VERSION:
                requirement.specifier = SpecifierSet()
            canonical.append(str(requirement))
        data["project"]["dependencies"] = canonical
    return data


def _canonical_lock(toml_content: str) -> Mapping[str, Any]:
    """uv.lock with ``specifier = ">=0"`` removed from the requirements"""
    data = tomllib.loads(toml_content)
    for package in data.get("package", ()):
        for requirement in package.get("metadata", {}).get("requires-dist", ()):
            if requirement.get("specifier") == ANY_VERSION:
                del requirement["specifier"]
    return data


def same_except_any_version(
    old_pyproject: str, new_pyproject: str, old_lock: str, new_lock: str
) -> bool:
    """True if the files only differ in ``name`` vs ``name>=0`` requirements.

    Both mean "any version", so such a difference is no change of the environment.

    >>> old = '[project]\\ndependencies = ["six", "pandas>=2"]'
    >>> new = '[project]\\ndependencies = ["six>=0", "pandas>=2"]'
    >>> old_lock = '[[package]]\\nname = "x"\\n[package.metadata]\\nrequires-dist = [{ name = "six" }]'
    >>> new_lock = old_lock.replace('"six" }', '"six", specifier = ">=0" }')
    >>> same_except_any_version(old, new, old_lock, new_lock)
    True
    >>> same_except_any_version(old, new.replace(">=2", ">=3"), old_lock, new_lock)
    False
    >>> same_except_any_version(old, new, old_lock, new_lock.replace('"x"', '"y"'))
    False
    >>> same_except_any_version("", "", "", "")
    True
    """
    return _canonical_pyproject(old_pyproject) == _canonical_pyproject(
        new_pyproject
    ) and _canonical_lock(old_lock) == _canonical_lock(new_lock)


def parse_pyproject_deps(
    toml_content: str,
) -> Mapping[NormalizedName, SpecifierSet | None]:
    """Parse dependencies from pyproject.toml content.

    Args:
        toml_content: TOML file content as string.

    Returns:
        Dictionary mapping canonical package names to their version specifiers.
        None for no constraint, which includes ``>=0`` (see ``ANY_VERSION``).

    Examples:
        >>> content = '''
        ... [project]
        ... dependencies = ["pandas>=2.0", "numpy", "scipy~=1.5.0", "six>=0"]
        ... '''
        >>> deps = parse_pyproject_deps(content)
        >>> 'pandas' in deps
        True
        >>> deps['numpy'] is None, deps['six'] is None
        (True, True)
        >>> str(deps['pandas'])
        '>=2.0'
        >>> str(deps['scipy'])
        '~=1.5.0'

        >>> # Empty content
        >>> parse_pyproject_deps('')
        {}

        >>> # No dependencies
        >>> parse_pyproject_deps('[project]\\nname = "test"')
        {}
    """
    if not toml_content.strip():
        return {}

    data = tomllib.loads(toml_content)
    requirements = (
        NormalizedRequirement(dep_str)
        for dep_str in data.get("project", {}).get("dependencies", ())
    )
    return {
        req.name: None if str(req.specifier) in {"", ANY_VERSION} else req.specifier
        for req in requirements
    }


def parse_uv_lock_packages(toml_content: str) -> Mapping[NormalizedName, Version]:
    """Parse packages from uv.lock content.

    Args:
        toml_content: TOML file content as string.

    Returns:
        Dictionary mapping canonical package names to their installed versions.

    Examples:
        >>> content = '''
        ... [[package]]
        ... name = "pandas"
        ... version = "2.0.0"
        ...
        ... [[package]]
        ... name = "NumPy"
        ... version = "1.24.3"
        ...
        ... [[package]]
        ... name = "ignored"
        ... '''
        >>> pkgs = parse_uv_lock_packages(content)
        >>> 'pandas' in pkgs
        True
        >>> 'numpy' in pkgs
        True
        >>> str(pkgs['pandas'])
        '2.0.0'
        >>> str(pkgs['numpy'])
        '1.24.3'
        >>> "ignore" not in pkgs
        True
        >>> # Empty content
        >>> parse_uv_lock_packages('')
        {}
    """
    if not toml_content.strip():
        return {}

    data = tomllib.loads(toml_content)
    return {
        canonicalize_name(pkg["name"]): Version(pkg["version"])
        for pkg in data.get("package", ())
        if "name" in pkg and "version" in pkg
    }


def parse_python_lock(content: str) -> Mapping[NormalizedName, Version]:
    """Parse the Python version lock file (contains only the exact version).

    Args:
        content: File content as string.

    Returns:
        Mapping of ``python`` to the interpreter version, empty if not recorded.

    Examples:
        >>> parse_python_lock("3.12.3\\n")
        {'python': <Version('3.12.3')>}
        >>> parse_python_lock("")
        {}
    """
    if not (version := content.strip()):
        return {}
    return {canonicalize_name("python"): Version(version)}


def parse_uv_environments(toml_content: str) -> Mapping[NormalizedName, None]:
    """Parse ``tool.uv.environments`` from pyproject.toml content.

    The markers are used as keys, so they can be compared with a ``Diff``
    like packages (they are not normalized, ``NormalizedName`` is only the type).

    Examples:
        >>> parse_uv_environments('''
        ... [tool.uv]
        ... environments = ["python_version == '3.12'", "python_version == '3.14'"]
        ... ''')
        {"python_version == '3.12'": None, "python_version == '3.14'": None}
        >>> parse_uv_environments('''
        ... [tool.uv]
        ... environments = "sys_platform == 'linux'"
        ... ''')
        {"sys_platform == 'linux'": None}
        >>> parse_uv_environments("")
        {}
    """
    if not toml_content.strip():
        return {}
    data = tomllib.loads(toml_content)
    environments = data.get("tool", {}).get("uv", {}).get("environments", ())
    if isinstance(environments, str):
        environments = (environments,)
    return {NormalizedName(marker): None for marker in environments}
