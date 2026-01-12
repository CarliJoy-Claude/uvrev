import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NewType

import click
from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

Revision = NewType("Revision", int)


@dataclass(frozen=True)
class UVRevProject:
    """Represents a UV project with revision tracking"""

    project_path: Path

    @property
    def pyproject_path(self) -> Path:
        return self.project_path / "pyproject.toml"

    @property
    def lock_path(self) -> Path:
        return self.project_path / "uv.lock"

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


class SubprocessFailure(click.ClickException):
    """
    SubprocessFailure Exception that will quit the program without stacktrace
    """

    def __init__(self, error: subprocess.CalledProcessError) -> None:
        if isinstance(error.cmd, tuple):  # pragma: no branch
            error.cmd = shlex.join(error.cmd)
        super().__init__(str(error))
