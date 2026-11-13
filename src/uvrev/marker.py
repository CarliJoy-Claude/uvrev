"""The marker of an unprotected environment (``.unprotected``).

``uvrev unprotect`` makes the venv writable for a hotfix and must be fast, so
nothing is scanned. Instead the marker remembers *when* the venv became
writable. The next command that protects the environment only has to look for
files changed since then (their ctime) to bring the venv back into a valid state.

The content of the marker is the time and a state::

    <ctime in ns>
    <state>

- ``pre-chmod``: written before the venv is made writable. If uvrev dies now,
  it is unknown which files were writable since when: the state is undefined.
- ``post-chmod``: the venv is writable, the time was taken after chmod finished.
  Files with a later ctime were changed by someone else.
- ``forced``: the venv is writable, but there is no time that can be trusted.

Only the content counts, never the timestamps of the marker itself, so touching
it does no harm. The time comes from the filesystem (the ctime of the marker
when it was touched), as ``time.time()`` is not aligned with file timestamps.
"""

import os
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from . import constants

type MarkerState = Literal["pre-chmod", "post-chmod", "forced", "undefined"]


@dataclass(frozen=True)
class Marker:
    """Content of the marker of an unprotected environment"""

    state: MarkerState
    # ctime in ns: files with a later ctime were changed since unprotecting
    stamp: int = 0

    @property
    def is_defined(self) -> bool:
        """True if it is known how to get the venv into a valid state again

        >>> Marker("post-chmod", 1).is_defined, Marker("forced").is_defined
        (True, True)
        >>> Marker("pre-chmod", 1).is_defined, Marker("undefined").is_defined
        (False, False)
        """
        return self.state in {"post-chmod", "forced"}


def parse_marker(content: str) -> Marker:
    """Parse the content of a marker, everything unexpected is ``undefined``.

    >>> parse_marker("1700000000000000000\\npost-chmod\\n")
    Marker(state='post-chmod', stamp=1700000000000000000)
    >>> parse_marker("12\\nforced\\n")
    Marker(state='forced', stamp=12)
    >>> parse_marker(""), parse_marker("yesterday\\npost-chmod\\n")
    (Marker(state='undefined', stamp=0), Marker(state='undefined', stamp=0))
    >>> parse_marker("12\\nsomething-else\\n")
    Marker(state='undefined', stamp=0)
    """
    lines = content.split()
    known = get_args(MarkerState.__value__)
    if len(lines) != 2 or not lines[0].isdecimal() or lines[1] not in known:  # noqa: PLR2004
        return Marker("undefined")
    return Marker(lines[1], int(lines[0]))  # type: ignore[arg-type]


def read_marker(path: Path) -> Marker | None:
    """Read the marker, None if the environment is not marked as unprotected"""
    try:
        return parse_marker(path.read_text(errors="replace"))
    except FileNotFoundError:
        return None


def write_marker(path: Path, marker: Marker) -> None:
    """Write the marker atomically and read-only.

    It is replaced by a complete file, so it is never empty or half written,
    and an accidental write by someone else is denied.
    """
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.write_text(f"{marker.stamp}\n{marker.state}\n")
    temporary.chmod(0o444)
    temporary.replace(path)


def remove_marker(path: Path) -> None:
    path.unlink(missing_ok=True)


def touch(path: Path) -> int:
    """Touch the marker and return its new ctime in ns: "now" for the filesystem.

    The owner may touch a read-only file. A missing marker is created.
    """
    if not path.exists():
        path.touch()
    os.utime(path)
    return path.lstat().st_ctime_ns


def wait_for_clock(
    path: Path,
    stamp: int,
    now: Callable[[Path], int] = touch,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait until the clock of the filesystem is later than the stamp.

    Afterwards every change of a file gets a ctime later than the stamp. Without
    waiting, a change made right after unprotecting could get the same ctime as
    the stamp and would not be found: timestamps have a resolution of up to a
    second and are taken from a clock that is only updated every few ms.

    Args:
        path: The marker.
        stamp: Time written into the marker.
        now: Returns the current time of the filesystem.
        sleep: Waits the given seconds.

    Returns:
        How often it had to wait.

    Examples:
        >>> times = iter((5, 5, 5, 6))
        >>> waited = []
        >>> wait_for_clock(Path("marker"), 5, lambda _: next(times), waited.append)
        3
        >>> waited
        [0.1, 0.1, 0.1]
    """
    rounds = 0
    while now(path) == stamp:
        rounds += 1
        sleep(constants.CLOCK_INTERVAL)
    return rounds


@contextmanager
def uninterrupted() -> Iterator[None]:
    """Ignore Ctrl+C and requests to terminate within the block.

    Started commands (chmod) inherit it, so they are not stopped either.
    ``kill -9``, a crash or a power loss can not be prevented, which is why
    the marker has a state.
    """
    signals = tuple(
        getattr(signal, name)
        for name in constants.INTERRUPTING_SIGNALS
        if hasattr(signal, name)
    )
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
