"""Verify that a venv contains exactly what its packages installed, and fix it.

A valid venv only contains the files in the ``RECORD`` of its packages (with
the recorded hash), the files of the venv itself and bytecode of an existing
source, and it is in sync with ``uv.lock``. Every file is hashed, so this does
not depend on timestamps (the fast check by ctime is in ``repair.py``).
"""

import base64
import csv
import hashlib
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import Literal

from packaging.utils import NormalizedName, canonicalize_name

from . import constants
from .commands import show_cmd, uv
from .exceptions import SubprocessFailure
from .marker import remove_marker
from .protection import writable_venv
from .types import UVRevProject

type ProblemKind = Literal["missing", "modified", "not_synced", "untracked"]


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
        return f"{self.path}{package}: {constants.PROBLEM_DESCRIPTIONS[self.kind]}"


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


def is_venv_file(project: UVRevProject, path: Path) -> bool:
    """True for the files a venv consists of itself (see ``constants.VENV_ROOT_FILES``).

    >>> from uvrev.settings import Settings
    >>> project = UVRevProject(Path("/env"), Settings())
    >>> is_venv_file(project, Path("/env/.venv/pyvenv.cfg"))
    True
    >>> is_venv_file(project, Path("/env/.venv/bin/python3.14"))
    True
    >>> is_venv_file(project, Path("/env/.venv/bin/activate.fish"))
    True
    >>> is_venv_file(project, Path("/env/.venv/bin/python-evil"))
    False
    >>> is_venv_file(project, Path("/env/.venv/lib/pyvenv.cfg"))
    False
    """
    parts = path.relative_to(project.venv_path.resolve()).parts
    if len(parts) == 1:
        return parts[0] in constants.VENV_ROOT_FILES
    if len(parts) == 2 and parts[0] in constants.SCRIPT_DIRECTORIES:  # noqa: PLR2004
        name = parts[1]
        return name in constants.VENV_SCRIPTS or bool(
            constants.VENV_INTERPRETER.fullmatch(name)
        )
    return (
        path.name in constants.VENV_SITE_FILES and path.parent in project.site_packages
    )


def bytecode_source(path: Path) -> Path | None:
    """The source of a bytecode file in ``__pycache__``, None for other files.

    >>> bytecode_source(Path("pkg/__pycache__/mod.cpython-314.pyc"))
    PosixPath('pkg/mod.py')
    >>> bytecode_source(Path("pkg/__pycache__/mod.cpython-314.opt-1.pyc"))
    PosixPath('pkg/mod.py')
    >>> bytecode_source(Path("pkg/mod.pyc")) is None
    True
    """
    if path.suffix != ".pyc" or path.parent.name != "__pycache__":
        return None
    return path.parent.parent / f"{path.name.split('.')[0]}.py"


def venv_files(project: UVRevProject) -> Iterator[Path]:
    """All files of the venv (links to folders like lib64 are not followed)"""
    for directory, _, filenames in os.walk(project.venv_path.resolve()):
        for name in filenames:
            yield Path(directory, name)


def installed_records(
    project: UVRevProject,
) -> tuple[tuple[NormalizedName, RecordEntry], ...]:
    """The RECORD entries of all installed packages, each with its package"""
    site_packages = [str(path) for path in project.site_packages]
    return tuple(
        (canonicalize_name(dist.metadata["Name"]), entry)
        for dist in distributions(path=site_packages)
        for entry in read_record(dist)
    )


def tracked_files(project: UVRevProject) -> Mapping[Path, NormalizedName]:
    """The files installed by the packages (their RECORD) and their package"""
    return {entry.path: package for package, entry in installed_records(project)}


def is_untracked(
    project: UVRevProject, path: Path, tracked: Mapping[Path, NormalizedName]
) -> bool:
    """True if a file must not be in a valid venv.

    Allowed are the files of the packages, the files of the venv itself and
    bytecode of an existing source (it is not part of the packages).
    """
    if path in tracked or is_venv_file(project, path):
        return False
    source = bytecode_source(path)
    return source is None or not source.exists()


def _check_records(project: UVRevProject) -> Iterator[Problem]:
    """Compare the files of all installed packages with their RECORD"""
    records = installed_records(project)
    for package, entry in records:
        path = entry.path
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

    tracked = {entry.path: package for package, entry in records}
    for path in venv_files(project):
        if is_untracked(project, path, tracked):
            yield Problem("untracked", path)


def _is_synced(project: UVRevProject) -> bool:
    try:
        uv("sync", "--frozen", "--check", project=project)
    except SubprocessFailure:
        return False
    return True


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verifying (and fixing) a venv"""

    # found before fixing
    problems: tuple[Problem, ...] = ()
    # left after fixing, the same as problems if nothing was fixed
    remaining: tuple[Problem, ...] = ()
    # True if fixing repaired at least one problem
    fixed: bool = False


# verify(project, fix) of a venv, so callers and tests can give another one
type Verifier = Callable[[UVRevProject, bool], VerifyResult]


def find_problems(project: UVRevProject) -> tuple[Problem, ...]:
    """Find all deviations of the venv from the installed packages and uv.lock"""
    problems = list(_check_records(project))
    if not _is_synced(project):
        problems.append(Problem("not_synced", project.lock_path))
    return tuple(problems)


def sync_bytecode(project: UVRevProject) -> tuple[Path, ...]:
    """Bring the bytecode of the venv in sync with its sources.

    Bytecode without source is removed. The rest is compiled (if the setting
    ``compile_bytecode`` is enabled), as a protected venv can not cache it later.
    Python only compiles files whose bytecode is missing or outdated.

    Returns:
        The removed files.
    """
    removed = []
    for path in venv_files(project):
        source = bytecode_source(path)
        if source is not None and not source.exists():
            path.unlink()
            removed.append(path)
    python = project.interpreter
    if project.settings.compile_bytecode and python is not None:
        cmd = (str(python), "-m", "compileall", "-q", *map(str, project.site_packages))
        show_cmd(cmd)
        # files that can not be compiled (e.g. templates, code for another
        # Python version) are reported by compileall, but are no failure
        subprocess.run(cmd, stdout=constants.STDERR_FD, check=False)
    return tuple(removed)


def fix_problems(
    project: UVRevProject, problems: tuple[Problem, ...]
) -> tuple[tuple[NormalizedName, ...], tuple[Path, ...]]:
    """Bring the venv into a valid state, the venv itself is kept.

    - Untracked files are removed.
    - Packages with missing or modified files are reinstalled in place.
      uv hardlinks the files of a package from its cache, so a file changed in
      the venv is changed in the cache as well (and in all venvs using it).
      Reinstalling alone would link the changed file again, so the cache entries
      of the affected packages are removed first.
      See https://github.com/astral-sh/uv/issues/22979
    - The venv is synced with uv.lock.
    - The bytecode is brought in sync with the sources.
    - The venv is protected (if enabled).

    Returns:
        The reinstalled packages and the removed files.
    """
    reinstall = tuple(
        sorted(
            {
                problem.package
                for problem in problems
                if problem.kind in constants.REINSTALL_PROBLEMS and problem.package
            }
        )
    )
    removed = []
    with writable_venv(project):
        for problem in problems:
            if problem.kind == "untracked":
                problem.path.unlink(missing_ok=True)
                removed.append(problem.path)
        if reinstall:
            # --force: without it uv waits for other uv processes using the cache,
            # e.g. forever if uvrev itself was started with uv run or uvx
            uv("cache", "clean", "--force", *reinstall, project=project)
        uv(
            "sync",
            "--frozen",
            *(f"--reinstall-package={package}" for package in reinstall),
            project=project,
        )
        removed.extend(sync_bytecode(project))
    return reinstall, tuple(removed)


def verify(
    project: UVRevProject,
    fix: bool = False,
    find: Callable[[UVRevProject], tuple[Problem, ...]] = find_problems,
) -> VerifyResult:
    """Verify the venv and optionally fix it.

    Every file is compared with the hash in the RECORD of its package, so this
    does not depend on timestamps. Fixing is described in ``fix_problems``.
    If the venv is valid afterwards, it is no longer marked as unprotected.

    Args:
        project: Project to verify.
        fix: Fix the problems.
        find: Finds the problems of the venv.

    Returns:
        The problems found and those remaining after fixing.
    """
    problems = find(project)
    if not fix:
        return VerifyResult(problems, problems)

    fix_problems(project, problems)
    remaining = find(project)
    if not remaining:
        remove_marker(project.unprotected_marker_path)
    return VerifyResult(problems, remaining, fixed=remaining != problems)
