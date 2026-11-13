from pathlib import Path
from typing import Protocol

import pytest

from uvrev import commands
from uvrev.commands import uv, uv_environ
from uvrev.exceptions import SubprocessFailure
from uvrev.settings import Settings
from uvrev.types import UVRevProject


class Command(Protocol):
    def __call__(self, *args: str, project: UVRevProject) -> None: ...


class TestFailing:
    @pytest.mark.parametrize(
        "cmd",
        [pytest.param(commands.git, id="git"), pytest.param(commands.uv, id="uv")],
    )
    def test_fail(self, cmd: Command, tmp_path: Path) -> None:
        proj = UVRevProject(tmp_path, Settings())
        with pytest.raises(SubprocessFailure):
            cmd("not-a-valid-command", project=proj)


class TestUV:
    def test_environ(self) -> None:
        """
        uv gets the environment without VIRTUAL_ENV (it confuses uv) and UV_PYTHON
        """
        environ = {
            "VIRTUAL_ENV": "/home/user/.venv",
            "UV_PYTHON": "3.8",
            "KEEP_ME": "IN",
        }
        project = UVRevProject(Path("/"), Settings(compile_bytecode=False))

        env = uv_environ(project, environ)

        assert "VIRTUAL_ENV" not in env
        assert "UV_PYTHON" not in env
        assert env["KEEP_ME"] == "IN"
        assert env["UV_COMPILE_BYTECODE"] == "false"
        # the given environment is not changed
        assert environ["VIRTUAL_ENV"] == "/home/user/.venv"

    def test_virtual_env_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A really activated other venv does not change the project's venv"""
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "other" / ".venv"))
        project = UVRevProject(tmp_path, Settings())

        output = uv("--version", project=project, capture_output=True)

        assert output.startswith("uv ")
