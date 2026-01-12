import tomlkit

from .types import UVRevProject


def read_pyproject(project: UVRevProject) -> tomlkit.TOMLDocument:
    """Read pyproject.toml using tomlkit.

    Args:
        project: Project to read from.

    Returns:
        Dictionary containing pyproject.toml content.
    """
    return tomlkit.loads(project.pyproject_path.read_text())


def write_pyproject(project: UVRevProject, data: tomlkit.TOMLDocument) -> None:
    """Write pyproject.toml using tomlkit to preserve comments.

    Args:
        project: Project to write to.
        data: Dictionary to write as TOML.
    """
    project.pyproject_path.write_text(tomlkit.dumps(data))
