import tomllib
from collections.abc import Mapping

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from .types import NormalizedRequirement


def parse_pyproject_deps(
    toml_content: str,
) -> Mapping[NormalizedName, SpecifierSet | None]:
    """Parse dependencies from pyproject.toml content.

    Args:
        toml_content: TOML file content as string.

    Returns:
        Dictionary mapping canonical package names to their version specifiers.

    Examples:
        >>> content = '''
        ... [project]
        ... dependencies = ["pandas>=2.0", "numpy", "scipy~=1.5.0"]
        ... '''
        >>> deps = parse_pyproject_deps(content)
        >>> 'pandas' in deps
        True
        >>> deps['numpy'] is None
        True
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
    return {req.name: req.specifier or None for req in requirements}


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
