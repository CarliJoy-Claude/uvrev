"""Unit tests of uvrev tool, see test_tool_integration.py for real wheels"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from packaging.utils import NormalizedName
from packaging.version import Version

import uvrev.tool
from uvrev.cli import cli
from uvrev.commands import uv_tool
from uvrev.exceptions import ToolError
from uvrev.tool import (
    RECEIPT_NAME,
    EmbeddedProject,
    ToolRequest,
    ToolSpec,
    check_embedded,
    console_scripts,
    ensure_environment,
    find_dist_info,
    get_cache_dir,
    has_receipt,
    parse_spec,
    read_embedded,
    resolve_version,
    run_tool,
    sync_dependencies,
    tool_directory,
    uv_environment,
)
from uvrev.types import SubprocessFailure

NAME = NormalizedName("my-tool")
REQUEST = ToolRequest(NAME, Version("1.0"), (), None)

PYPROJECT = b"""
[project]
name = "My_Tool"
dynamic = ["version"]

[project.optional-dependencies]
Fast = ["x"]
"""
LOCK = b"""
[[package]]
name = "dep"
version = "1"

[[package]]
name = "my-tool"
source = { editable = "." }
"""


@pytest.fixture(autouse=True)
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setenv("UVREV_CACHE", str(cache))
    return cache


class TestParseSpec:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("tool", ToolSpec(NormalizedName("tool"), None, "tool")),
            ("My.Tool", ToolSpec(NormalizedName("my-tool"), None, "My.Tool")),
            ("tool==1.0", ToolSpec(NormalizedName("tool"), Version("1.0"), "tool")),
            (
                "tool == 1.0+local",
                ToolSpec(NormalizedName("tool"), Version("1.0+local"), "tool"),
            ),
        ],
    )
    def test_valid(self, spec: str, expected: ToolSpec) -> None:
        assert parse_spec(spec) == expected

    @pytest.mark.parametrize(
        "spec",
        [
            "https://example.com/tool.whl",
            "git+https://example.com/tool",
            "tool @ https://example.com/tool.whl",
            "./tool",
            "dist/tool-1.0-py3-none-any.whl",
            "tool-1.0-py3-none-any.whl",
            "tool-1.0.tar.gz",
            "C:\\tool",
        ],
    )
    def test_url_or_path(self, spec: str) -> None:
        with pytest.raises(ToolError, match="URLs and paths are not supported"):
            parse_spec(spec)

    def test_extras(self) -> None:
        with pytest.raises(ToolError, match="use --extra a --extra b$"):
            parse_spec("tool[b,a]==1.0")

    @pytest.mark.parametrize(
        "spec",
        [
            "tool>=1",
            "tool~=1.0",
            "tool===1.0",
            "tool==1.*",
            "tool==1,!=2",
            "tool; python_version > '3'",
            "tool==1; python_version > '3'",
        ],
    )
    def test_unsupported_specifier(self, spec: str) -> None:
        with pytest.raises(ToolError, match="Unsupported package specification"):
            parse_spec(spec)

    def test_invalid(self) -> None:
        with pytest.raises(ToolError, match="Invalid package specification 'a b!'"):
            parse_spec("a b!")


class TestCheckEmbedded:
    def test_valid(self) -> None:
        check_embedded(
            EmbeddedProject(LOCK, PYPROJECT), NAME, (NormalizedName("fast"),)
        )

    def test_invalid_toml(self) -> None:
        with pytest.raises(ToolError, match="embedded uv.lock is not valid TOML"):
            check_embedded(EmbeddedProject(b"[[", PYPROJECT), NAME, ())

    def test_invalid_encoding(self) -> None:
        with pytest.raises(ToolError, match="pyproject.toml is not valid TOML"):
            check_embedded(EmbeddedProject(LOCK, b"\xff"), NAME, ())

    @pytest.mark.parametrize(
        ("pyproject", "message"),
        [
            (b"", "belongs to project 'None', not to 'my-tool'"),
            (b"[project]\nname = 'other'", "belongs to project 'other'"),
        ],
    )
    def test_name(self, pyproject: bytes, message: str) -> None:
        with pytest.raises(ToolError, match=message):
            check_embedded(EmbeddedProject(LOCK, pyproject), NAME, ())

    @pytest.mark.parametrize(
        "lock",
        [
            b"",
            b"[[package]]\nname = 'my-tool'\nsource = { registry = 'x' }",
            b"[[package]]\nname = 'other'\nsource = { editable = '.' }",
        ],
    )
    def test_lock_without_project(self, lock: bytes) -> None:
        with pytest.raises(ToolError, match="embedded uv.lock has no package"):
            check_embedded(EmbeddedProject(lock, PYPROJECT), NAME, ())

    def test_dynamic(self) -> None:
        pyproject = (
            b"[project]\nname = 'my-tool'\n"
            b"dynamic = ['version', 'requires-python', 'optional-dependencies']"
        )
        with pytest.raises(
            ToolError,
            match="declares optional-dependencies, requires-python as dynamic",
        ):
            check_embedded(EmbeddedProject(LOCK, pyproject), NAME, ())

    def test_unknown_extra(self) -> None:
        pyproject = b"[project]\nname = 'my-tool'"
        with pytest.raises(ToolError, match="Unknown extra.*: fast. Available: none"):
            check_embedded(
                EmbeddedProject(LOCK, pyproject), NAME, (NormalizedName("fast"),)
            )


class TestDistInfo:
    def test_find(self, tmp_path: Path) -> None:
        (tmp_path / "other-1.0.dist-info").mkdir()
        (tmp_path / "my_tool-1.0.dist-info").mkdir()
        assert find_dist_info(tmp_path, NAME).name == "my_tool-1.0.dist-info"

    def test_missing(self, tmp_path: Path) -> None:
        (tmp_path / "other-1.0.dist-info").mkdir()
        with pytest.raises(ToolError, match="Expected exactly one .* found 0"):
            find_dist_info(tmp_path, NAME)

    def test_read_partially_embedded(self, tmp_path: Path) -> None:
        folder = tmp_path / "my_tool-1.0.dist-info" / "extra_metadata"
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_bytes(PYPROJECT)
        with pytest.raises(ToolError, match="not contain extra_metadata/uv.lock:"):
            read_embedded(folder.parent)

    def test_read(self, tmp_path: Path) -> None:
        folder = tmp_path / "my_tool-1.0.dist-info" / "extra_metadata"
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_bytes(PYPROJECT)
        (folder / "uv.lock").write_bytes(LOCK)
        assert read_embedded(folder.parent) == EmbeddedProject(LOCK, PYPROJECT)

    def test_no_console_scripts(self, tmp_path: Path) -> None:
        assert console_scripts(tmp_path, NAME) == ()


class TestReceipt:
    @pytest.mark.parametrize(
        "content",
        [
            None,
            "not json",
            "[]",
            json.dumps({"package": "my-tool", "version": "2.0"}),
            json.dumps({"package": "other", "version": "1.0"}),
        ],
    )
    def test_incomplete(self, tmp_path: Path, content: str | None) -> None:
        if content is not None:
            (tmp_path / RECEIPT_NAME).write_text(content)
        assert not has_receipt(tmp_path, REQUEST)

    def test_complete(self, tmp_path: Path) -> None:
        receipt = json.dumps({"package": "my-tool", "version": "1.0"})
        (tmp_path / RECEIPT_NAME).write_text(receipt)
        assert has_receipt(tmp_path, REQUEST)


class TestEnvironment:
    def test_cache_dir(self, cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert get_cache_dir() == cache
        monkeypatch.delenv("UVREV_CACHE")
        assert get_cache_dir().name == "uvrev"

    def test_tool_directory(self, cache: Path) -> None:
        assert tool_directory(REQUEST) == cache / "tools" / "my-tool" / REQUEST.key

    def test_uv_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("UV_FROZEN", "UV_NO_SYNC", "UV_LOCKED", "VIRTUAL_ENV"):
            monkeypatch.setenv(var, "1")
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/elsewhere")
        monkeypatch.setenv("UV_INDEX_URL", "https://index.example.com")

        env = uv_environment()
        assert "UV_PROJECT_ENVIRONMENT" not in env
        assert env["UV_INDEX_URL"] == "https://index.example.com"
        for var in ("UV_FROZEN", "UV_NO_SYNC", "UV_LOCKED", "VIRTUAL_ENV"):
            assert var not in env

        assert uv_environment(Path("/venv"))["UV_PROJECT_ENVIRONMENT"] == "/venv"

    def test_built_by_parallel_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A parallel run built the environment while waiting for the lock"""
        answers = iter((False, True))
        monkeypatch.setattr(uvrev.tool, "has_receipt", lambda *_: next(answers))

        def fail(*_: Any) -> None:
            raise AssertionError("must not build")

        monkeypatch.setattr(uvrev.tool, "build_environment", fail)
        assert ensure_environment(REQUEST, refresh=False) == tool_directory(REQUEST)

    def test_failed_build_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def interrupted(directory: Path, request: ToolRequest) -> None:
            directory.mkdir(parents=True)
            raise KeyboardInterrupt

        monkeypatch.setattr(uvrev.tool, "build_environment", interrupted)
        with pytest.raises(KeyboardInterrupt):
            ensure_environment(REQUEST, refresh=False)
        assert not tool_directory(REQUEST).exists()


class TestUv:
    def test_resolve_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def fake(*args: str, **kwargs: Any) -> str:
            calls.append((args, kwargs))
            return "my-tool==3.1\n"

        monkeypatch.setattr(uvrev.tool, "uv_tool", fake)
        assert resolve_version(NAME, "3.13") == Version("3.1")
        ((args, kwargs),) = calls
        assert args[:3] == ("pip", "compile", "-")
        assert args[-2:] == ("--python", "3.13")
        assert kwargs["stdin"] == "my-tool\n"

    def test_sync_other_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Failures not caused by --locked are passed on"""

        def fake(*args: str, **kwargs: Any) -> str:
            raise SubprocessFailure(
                subprocess.CalledProcessError(2, ("uv",), None, "network down")
            )

        monkeypatch.setattr(uvrev.tool, "uv_tool", fake)
        with pytest.raises(SubprocessFailure, match="exit status 2"):
            sync_dependencies(tmp_path, REQUEST, {})

    def test_uv_tool_capture(self) -> None:
        output = uv_tool("--version", env=uv_environment(), capture_stdout=True)
        assert output.startswith("uv ")

    def test_uv_tool_failure(self) -> None:
        with pytest.raises(SubprocessFailure) as info:
            uv_tool("not-a-command", env=uv_environment(), capture_stderr=True)
        assert "not-a-command" in info.value.error.stderr


class TestRunTool:
    @pytest.mark.parametrize("command", ["", "bin/tool", "..\\tool"])
    def test_invalid_command(self, command: str) -> None:
        with pytest.raises(ToolError, match="COMMAND must be the name"):
            run_tool(command, (), "my-tool==1.0", (), None, False)

    def test_cli_error(self) -> None:
        result = CliRunner().invoke(cli, args=("tool", "run", "./tool"))
        assert result.exit_code == 1
        assert "❌ URLs and paths are not supported" in result.stderr

    @pytest.mark.parametrize(
        "args", [("tool", "--help"), ("tool", "run", "--help")], ids=" ".join
    )
    def test_help(self, args: tuple[str, ...]) -> None:
        result = CliRunner().invoke(cli, args=args)
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_requires_command(self) -> None:
        assert CliRunner().invoke(cli, args=("tool", "run")).exit_code == 2
