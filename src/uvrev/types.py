"""Basic types used by all modules: the project of an environment and its paths.

``UVRevProject`` knows where the files of an environment are, not what is in
them. Also home of ``Revision``.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NewType

from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

from . import constants
from .settings import Settings

Revision = NewType("Revision", int)


@dataclass(frozen=True)
class UVRevProject:
    """Represents a UV project with revision tracking"""

    project_path: Path
    settings: Settings = field(default_factory=Settings, compare=False)

    @property
    def pyproject_path(self) -> Path:
        return self.project_path / "pyproject.toml"

    @property
    def lock_path(self) -> Path:
        return self.project_path / "uv.lock"

    @property
    def python_version_path(self) -> Path:
        """Requested Python version (major.minor), used by uv"""
        return self.project_path / constants.PYTHON_VERSION_FILE

    @property
    def python_lock_path(self) -> Path:
        """Exact version of the Python interpreter of the environment"""
        return self.project_path / constants.PYTHON_VERSION_LOCK_FILE

    @property
    def venv_path(self) -> Path:
        return self.project_path / ".venv"

    @property
    def site_packages(self) -> tuple[Path, ...]:
        """The site-packages directories of the venv (empty if not created yet)

        lib64 is usually a symlink to lib, so the real paths are deduplicated.
        """
        found = (
            *self.venv_path.glob("lib*/python*/site-packages"),
            *self.venv_path.glob("Lib/site-packages"),
        )
        return tuple(dict.fromkeys(path.resolve() for path in found))

    @property
    def interpreter(self) -> Path | None:
        """The Python interpreter of the venv, None if the venv does not exist"""
        for python in (
            self.venv_path / "bin" / "python",
            self.venv_path / "Scripts" / "python.exe",
        ):
            if python.exists():
                return python
        return None

    @property
    def created_marker_path(self) -> Path:
        """Exists if the environment was created completely"""
        return self.project_path / constants.CREATED_MARKER_FILE

    @property
    def unprotected_marker_path(self) -> Path:
        """Exists while the venv is unprotected by ``uvrev unprotect``"""
        return self.project_path / constants.UNPROTECTED_MARKER_FILE

    def created_successfully(self) -> bool:
        return self.created_marker_path.exists()

    def exists(self) -> bool:
        """
        Check if project exists

        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as td:
        ...     p = UVRevProject(Path(td))
        ...     p.exists()
        False
        """
        if not self.project_path.exists() or not self.pyproject_path.exists():
            return False
        data = tomllib.loads(self.pyproject_path.read_text())
        return data.get("tool", {}).get("uv", {}).get("package") is False


class NormalizedRequirement(Requirement):
    """Requirement with normalized name"""

    name: NormalizedName

    def __init__(self, requirement_string: str):
        super().__init__(requirement_string)
        self.name = canonicalize_name(self.name)
