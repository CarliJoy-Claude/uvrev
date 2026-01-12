import tomllib

from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version

from .types import NormalizedRequirement


def parse_pyproject_deps(
    toml_content: str,
) -> dict[NormalizedName, SpecifierSet | None]:
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
    deps = {}

    for dep_str in data.get("project", {}).get("dependencies", []):
        req = NormalizedRequirement(dep_str)
        deps[req.name] = req.specifier if req.specifier else None

    return deps


def parse_uv_lock_packages(toml_content: str) -> dict[NormalizedName, Version]:
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
    packages = {}

    for pkg in data.get("package", []):
        if "name" in pkg and "version" in pkg:
            canonical_name = canonicalize_name(pkg["name"])
            packages[canonical_name] = Version(pkg["version"])

    return packages
