import subprocess
from pathlib import Path
from typing import Protocol
from unittest import mock

import pytest

from uvrev import commands
from uvrev.types import SubprocessFailure, UVRevProject


class Command(Protocol):
    def __call__(self, *args: str, project: UVRevProject): ...


class TestFailing:
    @pytest.mark.parametrize(
        "cmd",
        [pytest.param(commands.git, id="git"), pytest.param(commands.uv, id="uv")],
    )
    def test_fail(self, cmd: Command, tmp_path: Path) -> None:
        proj = UVRevProject(tmp_path)
        with pytest.raises(SubprocessFailure):
            cmd("not-a-valid-command", project=proj)


class TestUV:
    def test_remove_virtualenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        When running uv, remove the VIRTUAL_ENV parameter as it potentiell confusing uv
        """
        run_mock: mock.MagicMock = mock.create_autospec(subprocess.run)
        monkeypatch.setattr(commands.subprocess, "run", run_mock)
        monkeypatch.setattr(subprocess, "run", run_mock)
        monkeypatch.setenv("VIRTUAL_ENV", "/home/user/.venv")
        monkeypatch.setenv("KEEP_ME", "IN")

        commands.uv("--help", project=UVRevProject(Path("/")))

        run_mock.assert_called_once()
        env = run_mock.call_args.kwargs["env"]
        assert "VIRTUAL_ENV" not in env
        assert env["KEEP_ME"] == "IN"
