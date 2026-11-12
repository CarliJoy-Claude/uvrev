"""The marker of an unprotected environment"""

import os
import signal
import stat
import time
from pathlib import Path

import pytest

from uvrev.marker import (
    Marker,
    read_marker,
    remove_marker,
    touch,
    uninterrupted,
    wait_for_clock,
    write_marker,
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".unprotected"


class TestReadWrite:
    def test_missing(self, path: Path) -> None:
        assert read_marker(path) is None

    def test_roundtrip(self, path: Path) -> None:
        write_marker(path, Marker("pre-chmod", 5))
        assert read_marker(path) == Marker("pre-chmod", 5)

        # an existing (read-only) marker is replaced
        write_marker(path, Marker("post-chmod", 7))
        assert read_marker(path) == Marker("post-chmod", 7)
        assert not path.with_name(".unprotected.tmp").exists()

    def test_read_only(self, path: Path) -> None:
        write_marker(path, Marker("forced"))

        assert stat.S_IMODE(path.stat().st_mode) == 0o444

    @pytest.mark.parametrize("content", ["", "garbage", "1\n2\n3\n", "\xff\xfe"])
    def test_undefined(self, path: Path, content: str) -> None:
        """An empty or damaged marker is no defined state"""
        path.write_bytes(content.encode("latin-1"))

        marker = read_marker(path)

        assert marker == Marker("undefined")
        assert marker is not None
        assert not marker.is_defined

    def test_leftover_temporary_file(self, path: Path) -> None:
        """A read-only temporary file of a killed uvrev does not block writing"""
        leftover = path.with_name(".unprotected.tmp")
        leftover.write_text("")
        leftover.chmod(0o444)

        write_marker(path, Marker("forced", 3))

        assert read_marker(path) == Marker("forced", 3)

    def test_remove(self, path: Path) -> None:
        write_marker(path, Marker("forced"))

        remove_marker(path)
        remove_marker(path)

        assert read_marker(path) is None


class TestClock:
    def test_touch_creates(self, path: Path) -> None:
        stamp = touch(path)

        assert path.exists()
        assert stamp == path.lstat().st_ctime_ns

    def test_touch_read_only_marker(self, path: Path) -> None:
        """The time in the content counts, touching the marker changes nothing"""
        write_marker(path, Marker("post-chmod", 5))

        touch(path)

        assert read_marker(path) == Marker("post-chmod", 5)

    def test_wait_on_real_filesystem(self, path: Path) -> None:
        """After waiting, a change is later than the stamp (strictly)"""
        other = path.with_name("module.py")
        other.write_text("x = 1")
        for _ in range(50):
            stamp = touch(path)

            wait_for_clock(path, stamp)
            other.write_text("x = 2")

            assert other.lstat().st_ctime_ns > stamp

    def test_wait_coarse_clock(self, path: Path) -> None:
        """A filesystem storing only seconds: wait until the next second"""
        clock = iter((7, 7, 7, 8))
        waited: list[float] = []

        rounds = wait_for_clock(path, 7, lambda _: next(clock), waited.append)

        assert rounds == 3
        assert waited == [0.1, 0.1, 0.1]

    def test_no_wait_fine_clock(self, path: Path) -> None:
        waited: list[float] = []

        assert wait_for_clock(path, 7, lambda _: 8, waited.append) == 0
        assert waited == []


def interrupt_self() -> None:
    os.kill(os.getpid(), signal.SIGINT)
    # the signal is handled between two bytecodes
    time.sleep(1)


class TestUninterrupted:
    def test_signals_are_ignored(self) -> None:
        with uninterrupted():
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            # give the signals time to arrive
            time.sleep(0.05)
            survived = True

        assert survived

    def test_restored_afterwards(self) -> None:
        before = signal.getsignal(signal.SIGINT)

        with uninterrupted():
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN

        assert signal.getsignal(signal.SIGINT) is before
        with pytest.raises(KeyboardInterrupt):
            interrupt_self()

    def test_restored_after_failure(self) -> None:
        before = signal.getsignal(signal.SIGTERM)

        with pytest.raises(RuntimeError), uninterrupted():
            raise RuntimeError("chmod failed")

        assert signal.getsignal(signal.SIGTERM) is before
