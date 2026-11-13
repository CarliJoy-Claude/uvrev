"""Unit tests of uvrev tool, see test_tool_integration.py for real wheels"""

import hashlib
import json
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from click.testing import CliRunner
from packaging.utils import NormalizedName
from packaging.version import Version

from uvrev import constants
from uvrev.cli import cli
from uvrev.commands import UvRunner, uv_tool
from uvrev.exceptions import SubprocessFailure, ToolError
from uvrev.settings import Settings
from uvrev.tool import (
    EmbeddedProject,
    ToolRequest,
    ToolServices,
    ToolSpec,
    check_embedded,
    console_scripts,
    download,
    ensure_environment,
    fetch_wheel,
    find_dist_info,
    get_cache_dir,
    has_receipt,
    parse_spec,
    read_embedded,
    resolve_version,
    run_tool,
    sync_dependencies,
    tool_directory,
    tool_request,
    uv_environment,
)

SHA = "ab" * 32
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
        ("spec", "requested_name", "source", "remote"),
        [
            (
                "https://example.com/My_Tool-1.0-py3-none-any.whl",
                "my-tool",
                "https://example.com/My_Tool-1.0-py3-none-any.whl",
                True,
            ),
            (
                f"My.Tool @ https://example.com/my_tool-1.0-py3-none-any.whl#sha256={SHA}",
                "My.Tool",
                "https://example.com/my_tool-1.0-py3-none-any.whl",
                True,
            ),
            (
                "file:///wheels/my_tool-1.0-py3-none-any.whl",
                "my-tool",
                "/wheels/my_tool-1.0-py3-none-any.whl",
                False,
            ),
            (
                "dist/my_tool-1.0-py3-none-any.whl",
                "my-tool",
                "dist/my_tool-1.0-py3-none-any.whl",
                False,
            ),
            (
                "my_tool-1.0-py3-none-any.whl",
                "my-tool",
                "my_tool-1.0-py3-none-any.whl",
                False,
            ),
            (
                "my-tool @ ./dist/my_tool-1.0-py3-none-any.whl",
                "my-tool",
                "./dist/my_tool-1.0-py3-none-any.whl",
                False,
            ),
        ],
    )
    def test_wheel(
        self, spec: str, requested_name: str, source: str, remote: bool
    ) -> None:
        parsed = parse_spec(spec)
        assert parsed.name == "my-tool"
        assert parsed.version == Version("1.0")
        assert parsed.requested_name == requested_name
        assert parsed.wheel is not None
        assert parsed.wheel.filename.lower() == "my_tool-1.0-py3-none-any.whl"
        assert parsed.wheel.source == source
        assert parsed.wheel.remote is remote

    @pytest.mark.parametrize(
        ("spec", "message"),
        [
            ("./tool", "is not a wheel"),
            ("tool-1.0.tar.gz", "is not a wheel"),
            ("https://example.com/tool-1.0.zip", "is not a wheel"),
            ("tool @ https://example.com/tool-1.0.tar.gz", "is not a wheel"),
            ("git+https://example.com/tool", "Unsupported URL"),
            ("tool @ git+https://example.com/tool", "Unsupported URL"),
            ("ftp://example.com/tool-1.0-py3-none-any.whl", "Unsupported URL"),
            ("./tool.whl", "Invalid wheel filename 'tool.whl'"),
            (
                "other @ https://example.com/tool-1.0-py3-none-any.whl",
                "is 'tool', not 'other'",
            ),
            ("tool[x] @ https://example.com/tool-1.0-py3-none-any.whl", "--extra x"),
            ("tool @ ", "Invalid package specification"),
        ],
    )
    def test_not_supported(self, spec: str, message: str) -> None:
        with pytest.raises(ToolError, match=re.escape(message)):
            parse_spec(spec)

    def test_extras(self) -> None:
        with pytest.raises(ToolError, match=r"use --extra a --extra b$"):
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
            "tool @ https://example.com/tool-1.0-py3-none-any.whl ; os_name == 'x'",
        ],
    )
    def test_unsupported_specifier(self, spec: str) -> None:
        with pytest.raises(ToolError, match="Unsupported package specification"):
            parse_spec(spec)

    def test_invalid(self) -> None:
        with pytest.raises(ToolError, match="Invalid package specification 'a b!'"):
            parse_spec("a b!")


class TestHash:
    @pytest.mark.parametrize(
        "fragment", ["md5=abc", "sha256=abc", f"sha256={'g' * 64}", "sha256"]
    )
    def test_unsupported(self, fragment: str) -> None:
        with pytest.raises(ToolError, match="Unsupported hash in the URL fragment"):
            parse_spec(f"https://example.com/tool-1.0-py3-none-any.whl#{fragment}")

    def test_parsed(self) -> None:
        wheel = parse_spec(
            f"https://example.com/tool-1.0-py3-none-any.whl#sha256={SHA.upper()}"
        ).wheel
        assert wheel is not None
        assert wheel.sha256 == SHA
        assert wheel.source == "https://example.com/tool-1.0-py3-none-any.whl"

    def test_verified(self, tmp_path: Path) -> None:
        source = tmp_path / "wheels" / "tool-1.0-py3-none-any.whl"
        source.parent.mkdir()
        source.write_bytes(b"wheel")
        digest = hashlib.sha256(b"wheel").hexdigest()
        spec = parse_spec(f"{source.as_uri()}#sha256={digest}")

        request = tool_request(spec, (), None, tmp_path)
        assert request.wheel_sha256 == digest

        wrong = parse_spec(f"{source.as_uri()}#sha256={SHA}")
        with pytest.raises(ToolError, match=f"is {digest}, but {SHA} was expected"):
            tool_request(wrong, (), None, tmp_path)


class TestDownload:
    """Download rules, with a mock transport instead of a network"""

    @staticmethod
    def transport(
        responses: dict[str, httpx2.Response], seen: list[str]
    ) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(str(request.url))
            return responses[str(request.url)]

        return httpx2.MockTransport(handler)

    def test_redirect(self, tmp_path: Path) -> None:
        seen: list[str] = []
        responses = {
            "https://a.example/w.whl": httpx2.Response(
                302, headers={"Location": "https://b.example/w.whl"}
            ),
            "https://b.example/w.whl": httpx2.Response(200, content=b"wheel"),
        }
        target = tmp_path / "w.whl"
        download("https://a.example/w.whl", target, self.transport(responses, seen))
        assert target.read_bytes() == b"wheel"
        assert seen == list(responses)

    def test_refuse_https_to_http(self, tmp_path: Path) -> None:
        seen: list[str] = []
        responses = {
            "https://a.example/w.whl": httpx2.Response(
                302, headers={"Location": "http://b.example/w.whl"}
            ),
        }
        with pytest.raises(
            ToolError, match=re.escape("from https to insecure http://b.example")
        ):
            download(
                "https://a.example/w.whl",
                tmp_path / "w.whl",
                self.transport(responses, seen),
            )
        # the insecure request was never sent
        assert seen == ["https://a.example/w.whl"]

    def test_http_to_http(self, tmp_path: Path) -> None:
        seen: list[str] = []
        responses = {
            "http://a.example/w.whl": httpx2.Response(
                301, headers={"Location": "http://b.example/w.whl"}
            ),
            "http://b.example/w.whl": httpx2.Response(200, content=b"wheel"),
        }
        target = tmp_path / "w.whl"
        download("http://a.example/w.whl", target, self.transport(responses, seen))
        assert target.read_bytes() == b"wheel"

    def test_http_error(self, tmp_path: Path) -> None:
        responses = {"https://a.example/w.whl": httpx2.Response(403)}
        with pytest.raises(ToolError, match=r"Can not download .*'403 Forbidden'"):
            download(
                "https://a.example/w.whl",
                tmp_path / "w.whl",
                self.transport(responses, []),
            )

    @pytest.mark.parametrize(
        ("url", "warned"),
        [
            ("http://a.example/tool-1.0-py3-none-any.whl", True),
            (f"http://a.example/tool-1.0-py3-none-any.whl#sha256={SHA}", False),
            ("https://a.example/tool-1.0-py3-none-any.whl", False),
        ],
    )
    def test_warn_plain_http(
        self,
        url: str,
        warned: bool,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def no_download(url: str, target: Path) -> None:
            pass

        wheel = parse_spec(url).wheel
        assert wheel is not None
        fetch_wheel(wheel, tmp_path, no_download)
        assert ("integrity is not verified" in capsys.readouterr().err) is warned


class TestFetchWheel:
    def test_missing_file(self, tmp_path: Path) -> None:
        wheel = parse_spec(str(tmp_path / "tool-1.0-py3-none-any.whl")).wheel
        assert wheel is not None
        with pytest.raises(ToolError, match="Can not read the wheel"):
            fetch_wheel(wheel, tmp_path)

    def test_download_failure(self, tmp_path: Path) -> None:
        # port 9 (discard) is closed: the connection is refused without network
        wheel = parse_spec("http://127.0.0.1:9/tool-1.0-py3-none-any.whl").wheel
        assert wheel is not None
        with pytest.raises(ToolError, match="Can not download the wheel"):
            fetch_wheel(wheel, tmp_path)


class TestCheckEmbedded:
    def test_valid(self) -> None:
        check_embedded(
            EmbeddedProject(LOCK, PYPROJECT), NAME, (NormalizedName("fast"),)
        )

    def test_invalid_toml(self) -> None:
        with pytest.raises(
            ToolError, match=re.escape("embedded uv.lock is not valid TOML")
        ):
            check_embedded(EmbeddedProject(b"[[", PYPROJECT), NAME, ())

    def test_invalid_encoding(self) -> None:
        with pytest.raises(
            ToolError, match=re.escape("pyproject.toml is not valid TOML")
        ):
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
        with pytest.raises(
            ToolError, match=re.escape("embedded uv.lock has no package")
        ):
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
        with pytest.raises(
            ToolError,
            match=re.escape("Unknown extra(s) of 'my-tool': fast. Available: none"),
        ):
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
        with pytest.raises(ToolError, match=r"Expected exactly one .* found 0"):
            find_dist_info(tmp_path, NAME)

    def test_read_partially_embedded(self, tmp_path: Path) -> None:
        folder = tmp_path / "my_tool-1.0.dist-info" / "extra_metadata"
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_bytes(PYPROJECT)
        with pytest.raises(
            ToolError, match=re.escape("not contain extra_metadata/uv.lock:")
        ):
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
            (tmp_path / constants.RECEIPT_NAME).write_text(content)
        assert not has_receipt(tmp_path, REQUEST)

    def test_complete(self, tmp_path: Path) -> None:
        receipt = json.dumps({"package": "my-tool", "version": "1.0"})
        (tmp_path / constants.RECEIPT_NAME).write_text(receipt)
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

    def test_built_by_parallel_run(self) -> None:
        """A parallel run built the environment while waiting for the lock"""

        @contextmanager
        def lock_held_by_parallel_run(path: Path) -> Iterator[None]:
            # the parallel run finished its build while we waited
            directory = tool_directory(REQUEST)
            directory.mkdir(parents=True)
            receipt = {"package": NAME, "version": "1.0", "wheel_sha256": None}
            (directory / constants.RECEIPT_NAME).write_text(json.dumps(receipt))
            yield

        def fail(*_: Any) -> None:
            raise AssertionError("must not build")

        services = ToolServices(lock=lock_held_by_parallel_run, build=fail)
        directory = ensure_environment(
            REQUEST, refresh=False, settings=Settings(), services=services
        )
        assert directory == tool_directory(REQUEST)

    def test_failed_build_is_removed(self) -> None:
        def interrupted(
            directory: Path, request: ToolRequest, settings: Settings, uv: UvRunner
        ) -> None:
            directory.mkdir(parents=True)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            ensure_environment(
                REQUEST,
                refresh=False,
                settings=Settings(),
                services=ToolServices(build=interrupted),
            )
        assert not tool_directory(REQUEST).exists()


class TestUv:
    def test_resolve_version(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def fake(*args: str, **kwargs: Any) -> str:
            calls.append((args, kwargs))
            return "my-tool==3.1\n"

        assert resolve_version(NAME, "3.13", fake) == Version("3.1")
        ((args, kwargs),) = calls
        assert args[:3] == ("pip", "compile", "-")
        assert args[-2:] == ("--python", "3.13")
        assert kwargs["stdin"] == "my-tool\n"

    def test_sync_other_failure(self, tmp_path: Path) -> None:
        """Failures not caused by --locked are passed on"""

        def fake(*args: str, **kwargs: Any) -> str:
            raise SubprocessFailure(
                subprocess.CalledProcessError(2, ("uv",), None, "network down")
            )

        with pytest.raises(SubprocessFailure, match="exit status 2"):
            sync_dependencies(tmp_path, REQUEST, {}, fake)

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
            run_tool(command, (), "my-tool==1.0", (), None, False, Settings())

    def test_cli_error(self) -> None:
        result = CliRunner().invoke(cli, args=("tool", "run", "./tool"))
        assert result.exit_code == 1
        assert "❌ './tool' is not a wheel (.whl)" in result.stderr

    @pytest.mark.parametrize(
        "args", [("tool", "--help"), ("tool", "run", "--help")], ids=" ".join
    )
    def test_help(self, args: tuple[str, ...]) -> None:
        result = CliRunner().invoke(cli, args=args)
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_requires_command(self) -> None:
        assert CliRunner().invoke(cli, args=("tool", "run")).exit_code == 2
