"""Unprotect an environment for a hotfix and repair it afterwards.

``uvrev unprotect`` is meant for hotfixes: it must be fast and always work.
So it only makes the venv writable and remembers when (see ``marker.py``).

The next command that protects the environment brings the venv back into a
valid state first: it only contains the files of the packages locked in
uv.lock, with the content of their wheels. The venv and the uv cache are kept,
so running processes are not disturbed.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click
from packaging.utils import NormalizedName

from . import constants
from .exceptions import VenvChangedError
from .io import exclude_from_git
from .marker import (
    Marker,
    read_marker,
    remove_marker,
    touch,
    uninterrupted,
    wait_for_clock,
    write_marker,
)
from .protection import unprotect
from .terminal import warning
from .types import UVRevProject
from .verify import (
    Problem,
    find_problems,
    fix_problems,
    is_untracked,
    is_venv_file,
    tracked_files,
)


@dataclass(frozen=True)
class RepairResult:
    """What was done to bring an unprotected venv into a valid state"""

    # fast: files changed since unprotecting (by their ctime) were repaired
    # full: every file was compared with the hash of its package
    mode: Literal["fast", "full"]
    # reinstalled packages
    packages: tuple[NormalizedName, ...]
    # removed files that no package installed
    removed_files: tuple[Path, ...]


def mark_unprotected(project: UVRevProject) -> tuple[bool, Marker]:
    """Make the venv writable for a hotfix and mark the environment.

    Nothing is scanned, so it is fast. It can not be interrupted with Ctrl+C.

    - Already unprotected (marker with a defined state): nothing is done, so
      the remembered time is kept. chmod would change the ctime of all files.
    - Marker with an undefined state (an earlier unprotect was killed, or the
      marker was damaged): the venv is made writable, but there is no time
      that can be trusted. The state is ``forced``.
    - Otherwise the marker is written before chmod (``pre-chmod``) and again
      afterwards (``post-chmod``) with the time of the filesystem. Then it
      waits until the clock of the filesystem moved on, see ``wait_for_clock``.

    Returns:
        Whether something was done, and the marker.
    """
    path = project.unprotected_marker_path
    existing = read_marker(path)
    if existing is not None and existing.is_defined:
        return False, existing

    with uninterrupted():
        exclude_from_git(project, constants.UNPROTECTED_MARKER_FILE)
        if existing is not None:
            marker = Marker("forced", touch(path))
            write_marker(path, marker)
            unprotect(project)
            return True, marker
        write_marker(path, Marker("pre-chmod", touch(path)))
        unprotect(project)
        marker = Marker("post-chmod", touch(path))
        write_marker(path, marker)
        wait_for_clock(path, marker.stamp)
    return True, marker


def changed_since(project: UVRevProject, stamp: int) -> tuple[Problem, ...]:
    """Find what was changed in the venv since it was unprotected.

    A file or folder was changed if its ctime is later than the stamp. The
    ctime can not be set by a user and is updated by every change of the
    content, the permissions or the name. No file is read, so this is fast.

    - A changed file of a package: the package is reinstalled (``modified``).
    - A changed folder got or lost entries: its files are checked for
      untracked ones, and all files of the packages are checked for existence.
    - A file that no package installed: it is removed (``untracked``).
    - Bytecode of an existing source is fine, it is compiled again later.

    Raises:
        VenvChangedError: If a file of the venv itself was changed.
    """
    venv = project.venv_path.resolve()
    tracked = tracked_files(project)
    problems = []
    entries_changed = False
    for directory, _, filenames in os.walk(venv):
        folder_changed = Path(directory).lstat().st_ctime_ns > stamp
        entries_changed = entries_changed or folder_changed
        for name in filenames:
            path = Path(directory, name)
            changed = path.lstat().st_ctime_ns > stamp
            if not changed and not folder_changed:
                continue
            if path in tracked:
                if changed:
                    problems.append(Problem("modified", path, tracked[path]))
            elif is_venv_file(project, path):
                if changed and name not in constants.VOLATILE_VENV_FILES:
                    raise VenvChangedError(path)
            elif is_untracked(project, path, tracked):
                problems.append(Problem("untracked", path))

    if entries_changed:
        if project.interpreter is None:
            raise VenvChangedError(venv / "bin" / "python")
        if not (venv / "pyvenv.cfg").is_file():
            raise VenvChangedError(venv / "pyvenv.cfg")
        problems.extend(
            Problem("missing", path, package)
            for path, package in tracked.items()
            if not os.path.lexists(path)
        )
    return tuple(problems)


def repair(project: UVRevProject) -> RepairResult | None:
    """Bring the venv of an unprotected environment into a valid state.

    - ``post-chmod``: only what changed since unprotecting is repaired (fast).
    - Any other state: there is no time that can be trusted, so every file
      is compared with the hash of its package like ``uvrev verify --fix``.
      This can not see changes of the files of the venv itself, or of a
      RECORD that match a changed file, so the user is told about
      ``uvrev sync --clean``.

    Afterwards the venv is protected (if enabled) and the mark is removed.
    If it fails, the environment stays marked.

    Returns:
        What was done, None if the environment is not marked as unprotected.

    Raises:
        VenvChangedError: If a file of the venv itself was changed (fast repair).
    """
    path = project.unprotected_marker_path
    marker = read_marker(path)
    if marker is None:
        return None
    click.echo(f"Repairing the unprotected environment ({marker.state})", err=True)
    if marker.state == "post-chmod":
        result = RepairResult(
            "fast", *fix_problems(project, changed_since(project, marker.stamp))
        )
    else:
        result = RepairResult("full", *fix_problems(project, find_problems(project)))
    remove_marker(path)
    if result.mode == "full":
        warning(
            "The environment was unprotected without a reliable time, so every "
            "file was compared with the hash recorded by its package. This can "
            "not detect changes of the virtual environment itself (pyvenv.cfg, "
            "the scripts to activate it) or of the recorded hashes. "
            "Run 'uvrev sync --clean' to be sure."
        )
    return result
