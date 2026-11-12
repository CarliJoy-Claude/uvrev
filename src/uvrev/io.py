"""Read and write the files of an environment that uvrev edits itself.

``pyproject.toml`` is edited with tomlkit, which keeps comments and formatting.
Files that are only read are parsed in ``parser.py``.
"""

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


def exclude_from_git(project: UVRevProject, *names: str) -> None:
    """Let git ignore files of uvrev in the folder of an environment.

    Uses ``.git/info/exclude``, so no tracked file is changed.
    Existing entries are kept, names are added only once.
    """
    exclude = project.project_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    ignored = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [name for name in names if name not in ignored]
    if missing:
        exclude.write_text("\n".join((*ignored, *missing)) + "\n")
