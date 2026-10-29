import base64
import csv
import hashlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from packaging.utils import NormalizedName, canonicalize_name

from .commands import uv
from .protection import writable_venv
from .types import SubprocessFailure, UVRevProject

type ProblemKind = Literal["missing", "modified", "not_synced", "untracked"]

# Problems fixed by reinstalling the package
REINSTALL: Final[frozenset[ProblemKind]] = frozenset({"missing", "modified"})
# Problems that are only reported, as fixing them could delete someone's work
WARNINGS: Final[frozenset[ProblemKind]] = frozenset({"untracked"})

# Files in site-packages created with the venv, not by a package
VENV_FILES: Final[frozenset[str]] = frozenset({"_virtualenv.py", "_virtualenv.pth"})

DESCRIPTIONS: Final[Mapping[ProblemKind, str]] = MappingProxyType(
    {
        "missing": "missing",
        "modified": "modified",
        "not_synced": "not in sync with uv.lock",
        "untracked": "not installed by any package",
    }
)


@dataclass(frozen=True)
class Problem:
    """A deviation of the venv from the installed packages"""

    kind: ProblemKind
    path: Path
    package: NormalizedName | None = None

    def __str__(self) -> str:
        """
        >>> print(Problem("modified", Path("a.py"), NormalizedName("pkg")))
        a.py (pkg): modified
        >>> print(Problem("untracked", Path("b.py")))
        b.py: not installed by any package
        """
        package = f" ({self.package})" if self.package else ""
        return f"{self.path}{package}: {DESCRIPTIONS[self.kind]}"

    @property
    def is_warning(self) -> bool:
        return self.kind in WARNINGS


def file_hash(path: Path, algorithm: str) -> str:
    """Hash of a file as written in RECORD files (urlsafe base64 without padding)

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as td:
    ...     path = Path(td, "empty")
    ...     path.touch()
    ...     file_hash(path, "sha256")
    '47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU'
    """
    digest = hashlib.new(algorithm, path.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True)
class RecordEntry:
    """An entry of a RECORD file"""

    path: Path
    algorithm: str | None
    hash: str | None
    size: int | None


def read_record(dist: Distribution) -> tuple[RecordEntry, ...]:
    """Read the RECORD of an installed package.

    Distribution.files is not used, as it omits files that do not exist
    (depending on the python version).
    """
    entries = []
    for row in csv.reader((dist.read_text("RECORD") or "").splitlines()):
        name, hash_value, size = (*row, "", "")[:3]
        algorithm, _, digest = hash_value.partition("=")
        # normpath resolves entries like ../../../bin/script
        path = Path(os.path.normpath(Path(str(dist.locate_file(name)))))
        entries.append(
            RecordEntry(
                path=path,
                algorithm=algorithm if digest else None,
                hash=digest or None,
                size=int(size) if size else None,
            )
        )
    return tuple(entries)


def _check_records(project: UVRevProject) -> Iterator[Problem]:
    """Compare the files of all installed packages with their RECORD"""
    tracked: set[Path] = set()
    site_packages = project.site_packages
    for dist in distributions(path=[str(path) for path in site_packages]):
        package = canonicalize_name(dist.metadata["Name"])
        for entry in read_record(dist):
            path = entry.path
            tracked.add(path)
            if entry.algorithm is None or entry.hash is None:
                # e.g. RECORD itself, nothing to compare
                continue
            try:
                st = path.lstat()
            except FileNotFoundError:
                yield Problem("missing", path, package)
                continue
            if (entry.size is not None and st.st_size != entry.size) or (
                file_hash(path, entry.algorithm) != entry.hash
            ):
                yield Problem("modified", path, package)

    for directory in site_packages:
        for dirpath, dirnames, filenames in os.walk(directory):
            # bytecode is created on install or at runtime, it is not part of RECORD
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for name in filenames:
                path = Path(dirpath, name)
                is_venv_file = name in VENV_FILES and path.parent == directory
                if path not in tracked and not is_venv_file:
                    yield Problem("untracked", path)


def _is_synced(project: UVRevProject) -> bool:
    try:
        uv("sync", "--frozen", "--check", project=project)
    except SubprocessFailure:
        return False
    return True


def find_problems(project: UVRevProject) -> tuple[Problem, ...]:
    """Find all deviations of the venv from the installed packages and uv.lock"""
    problems = list(_check_records(project))
    if not _is_synced(project):
        problems.append(Problem("not_synced", project.lock_path))
    return tuple(problems)


def verify(project: UVRevProject, fix: bool = False) -> tuple[Problem, ...]:
    """Verify the venv and optionally fix it.

    Fixing reinstalls packages with missing or modified files,
    syncs the venv with uv.lock and protects it (if enabled).
    Untracked files are only reported.

    Args:
        project: Project to verify.
        fix: Fix the problems.

    Returns:
        The (remaining) problems.
    """
    problems = find_problems(project)
    if not fix:
        return problems

    reinstall = sorted(
        {
            problem.package
            for problem in problems
            if problem.kind in REINSTALL and problem.package
        }
    )
    with writable_venv(project):
        uv(
            "sync",
            "--frozen",
            *(f"--reinstall-package={package}" for package in reinstall),
            project=project,
        )
    return find_problems(project)
