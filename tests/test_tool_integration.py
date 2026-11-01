"""Integration tests of ``uvrev tool run`` with real fixture wheels.

The wheels are built with plain hatchling config that embeds uv.lock and
pyproject.toml like hatch-uv-lock does. They are served with UV_FIND_LINKS,
their dependencies come from PyPI.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from click.testing import CliRunner, Result

import uvrev.commands
import uvrev.tool
from uvrev.cli import cli
from uvrev.tool import RECEIPT_NAME

pytestmark = pytest.mark.network

MODULES = ("six", "idna", "iniconfig")

SCRIPT = dedent(
    f"""
    import importlib.util
    import json
    import sys


    def main():
        args = sys.argv[1:]
        modules = {{m: importlib.util.find_spec(m) is not None for m in {MODULES!r}}}
        print(json.dumps({{"args": args, "modules": modules}}))
        code = 0
        for arg in args:
            if arg.startswith("exit="):
                code = int(arg.removeprefix("exit="))
        sys.exit(code)
    """
)

HOOK = dedent(
    """
    from hatchling.metadata.plugin.interface import MetadataHookInterface


    class CustomMetadataHook(MetadataHookInterface):
        def update(self, metadata):
            {body}
    """
)

EMBED = dedent(
    """
    [tool.hatch.build.targets.wheel.extra-metadata]
    "uv.lock" = "uv.lock"
    "pyproject.toml" = "pyproject.toml"
    """
)


def pyproject(
    name: str,
    *,
    version: str = 'version = "1.0"',
    dependencies: str = 'dependencies = ["six"]',
    embed: bool = True,
    hook: bool = False,
) -> str:
    module = name.replace("-", "_")
    content = dedent(
        f"""
        [project]
        name = "{name}"
        {version}
        requires-python = ">=3.12"
        {dependencies}

        [project.optional-dependencies]
        idn = ["idna"]

        [project.scripts]
        {name} = "{module}:main"
        fx-other = "{module}:main"

        [dependency-groups]
        dev = ["iniconfig"]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [tool.hatch.build.targets.wheel]
        packages = ["src/{module}"]
        """
    )
    if hook:
        content += "\n[tool.hatch.metadata.hooks.custom]\n"
    if embed:
        content += EMBED
    return content


def uv_build_env() -> Mapping[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"UV_FROZEN", "UV_LOCKED", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }


def build_wheel(
    root: Path,
    out: Path,
    name: str,
    content: str,
    *,
    hook_body: str | None = None,
    after_lock: str | None = None,
) -> None:
    """Create a project, lock it and build its wheel.

    Args:
        after_lock: pyproject.toml written after locking, to create a stale lock
    """
    project = root / name
    module = project / "src" / name.replace("-", "_")
    module.mkdir(parents=True)
    module.joinpath("__init__.py").write_text(SCRIPT)
    project.joinpath("pyproject.toml").write_text(content)
    if hook_body is not None:
        project.joinpath("hatch_build.py").write_text(HOOK.format(body=hook_body))
    env = uv_build_env()
    subprocess.run(("uv", "lock", "-q"), cwd=project, env=env, check=True)
    if after_lock is not None:
        project.joinpath("pyproject.toml").write_text(after_lock)
    subprocess.run(
        ("uv", "build", "--wheel", "-q", "-o", str(out)),
        cwd=project,
        env=env,
        check=True,
    )


@pytest.fixture(scope="session")
def find_links(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build all fixture wheels once"""
    root = tmp_path_factory.mktemp("projects")
    out = tmp_path_factory.mktemp("wheels")
    build_wheel(root, out, "uvrev-fx-app", pyproject("uvrev-fx-app"))
    build_wheel(
        root, out, "uvrev-fx-noembed", pyproject("uvrev-fx-noembed", embed=False)
    )
    build_wheel(
        root,
        out,
        "uvrev-fx-stale",
        pyproject("uvrev-fx-stale"),
        after_lock=pyproject(
            "uvrev-fx-stale", dependencies='dependencies = ["six", "idna"]'
        ),
    )
    build_wheel(
        root,
        out,
        "uvrev-fx-dyndeps",
        pyproject(
            "uvrev-fx-dyndeps", dependencies='dynamic = ["dependencies"]', hook=True
        ),
        hook_body='metadata["dependencies"] = ["six"]',
    )
    build_wheel(
        root,
        out,
        "uvrev-fx-dynver",
        pyproject("uvrev-fx-dynver", version='dynamic = ["version"]', hook=True),
        hook_body='metadata["version"] = "2.0"',
    )
    return out


@pytest.fixture(autouse=True)
def tool_env(find_links: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Serve the fixture wheels and use an empty cache, returns the cache dir"""
    cache = tmp_path / "cache"
    monkeypatch.setenv("UVREV_CACHE", str(cache))
    monkeypatch.setenv("UV_FIND_LINKS", str(find_links))
    for var in ("UV_PROJECT_ENVIRONMENT", "UV_LOCKED", "UV_NO_SYNC"):
        monkeypatch.delenv(var, raising=False)
    return cache


@pytest.fixture
def uv_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record the arguments of all uv calls of uvrev tool"""
    calls: list[tuple[str, ...]] = []
    original = uvrev.commands.uv_tool

    def spy(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(uvrev.tool, "uv_tool", spy)
    return calls


def syncs(calls: list[tuple[str, ...]]) -> int:
    return sum(1 for call in calls if call[0] == "sync")


def run(*args: str) -> Result:
    return CliRunner().invoke(cli, args=("tool", "run", *args))


def tool_output(capfd: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Output of the fixture script, written to the real stdout"""
    out = capfd.readouterr().out
    result: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
    return result


def tool_dirs(cache: Path, name: str) -> tuple[Path, ...]:
    return tuple(p for p in (cache / "tools" / name).iterdir() if p.is_dir())


class TestHappyPath:
    def test_args_and_exit_code(
        self,
        capfd: pytest.CaptureFixture[str],
        tool_env: Path,
        uv_calls: list[tuple[str, ...]],
    ) -> None:
        """Case 1 and 3: arguments and exit code pass through, dev group skipped"""
        args = ("-x", "--extra", "idn", "--refresh", "--", "exit=3")
        result = run("uvrev-fx-app", *args)

        assert result.exit_code == 3, result.output
        output = tool_output(capfd)
        assert output["args"] == list(args)
        assert output["modules"] == {"six": True, "idna": False, "iniconfig": False}
        # the version was resolved
        assert uv_calls[0][:2] == ("pip", "compile")

        (directory,) = tool_dirs(tool_env, "uvrev-fx-app")
        assert directory.name.startswith("1.0-")
        receipt = json.loads((directory / RECEIPT_NAME).read_text())
        assert receipt["package"] == "uvrev-fx-app"
        assert receipt["version"] == "1.0"
        assert receipt["extras"] == []
        assert receipt["python"] is None
        assert len(receipt["uv_lock_sha256"]) == 64
        assert len(receipt["pyproject_sha256"]) == 64
        assert receipt["uv_version"]
        assert receipt["created"]

    def test_other_script(self, capfd: pytest.CaptureFixture[str]) -> None:
        result = run("--from", "uvrev-fx-app==1.0", "fx-other", "a")
        assert result.exit_code == 0, result.output
        assert tool_output(capfd)["args"] == ["a"]

    def test_unknown_command(self) -> None:
        result = run("--from", "uvrev-fx-app", "nope")
        assert result.exit_code == 1
        assert "'nope' is not a console script of uvrev-fx-app==1.0" in result.stderr
        assert "Available: fx-other, uvrev-fx-app" in result.stderr

    def test_python(self, capfd: pytest.CaptureFixture[str], tool_env: Path) -> None:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        result = run("--python", python, "uvrev-fx-app")
        assert result.exit_code == 0, result.output
        (directory,) = tool_dirs(tool_env, "uvrev-fx-app")
        receipt = json.loads((directory / RECEIPT_NAME).read_text())
        assert receipt["python"] == python


class TestExtras:
    def test_extra(
        self, capfd: pytest.CaptureFixture[str], uv_calls: list[tuple[str, ...]]
    ) -> None:
        """Case 2: --extra installs the extra"""
        result = run("--extra", "IDN", "uvrev-fx-app")
        assert result.exit_code == 0, result.output
        assert tool_output(capfd)["modules"] == {
            "six": True,
            "idna": True,
            "iniconfig": False,
        }
        (sync,) = (call for call in uv_calls if call[0] == "sync")
        assert sync[sync.index("--extra") :][:2] == ("--extra", "idn")

    def test_unknown_extra(
        self, tool_env: Path, uv_calls: list[tuple[str, ...]]
    ) -> None:
        """Case 2: an unknown extra fails before uv sync"""
        result = run("--extra", "idn", "--extra", "nope", "uvrev-fx-app")
        assert result.exit_code == 1
        assert "Unknown extra(s) of 'uvrev-fx-app': nope" in result.stderr
        assert "Available: idn" in result.stderr
        assert syncs(uv_calls) == 0
        # failed builds are removed
        assert tool_dirs(tool_env, "uvrev-fx-app") == ()


class TestRefused:
    def test_not_embedded(self, tool_env: Path) -> None:
        """Case 4: the release was not built with hatch-uv-lock"""
        result = run("uvrev-fx-noembed")
        assert result.exit_code == 1
        assert "not built with hatch-uv-lock" in result.stderr
        assert "extra_metadata/uv.lock, extra_metadata/pyproject.toml" in result.stderr
        assert tool_dirs(tool_env, "uvrev-fx-noembed") == ()

    def test_stale_lock(self, tool_env: Path) -> None:
        """Case 5: the lock was stale when the wheel was built"""
        result = run("uvrev-fx-stale")
        assert result.exit_code == 1
        # uv's own message is shown
        assert "`--locked` was provided" in result.stderr
        assert "the lock was outdated when the release was built" in result.stderr
        assert tool_dirs(tool_env, "uvrev-fx-stale") == ()

    def test_dynamic_dependencies(self, uv_calls: list[tuple[str, ...]]) -> None:
        """Case 6: dynamic dependencies fail before uv sync"""
        result = run("uvrev-fx-dyndeps")
        assert result.exit_code == 1
        assert "declares dependencies as dynamic" in result.stderr
        assert "uv would need the source tree" in result.stderr
        assert syncs(uv_calls) == 0


def test_dynamic_version(capfd: pytest.CaptureFixture[str]) -> None:
    """Case 7: dynamic version from a metadata hook not shipped in the wheel"""
    result = run("--from", "uvrev-fx-dynver==2.0", "uvrev-fx-dynver", "ok")
    assert result.exit_code == 0, result.output
    assert tool_output(capfd)["args"] == ["ok"]


def test_cache_and_refresh(
    capfd: pytest.CaptureFixture[str],
    tool_env: Path,
    uv_calls: list[tuple[str, ...]],
) -> None:
    """Case 8: a second run reuses the cache, --refresh rebuilds"""
    assert run("uvrev-fx-app==1.0").exit_code == 0
    assert syncs(uv_calls) == 1
    (directory,) = tool_dirs(tool_env, "uvrev-fx-app")
    created = json.loads((directory / RECEIPT_NAME).read_text())["created"]
    marker = directory / ".venv" / "marker"
    marker.touch()

    assert run("uvrev-fx-app==1.0", "again").exit_code == 0
    assert syncs(uv_calls) == 1
    assert marker.exists()
    assert tool_output(capfd)["args"] == ["again"]

    assert run("--refresh", "uvrev-fx-app==1.0").exit_code == 0
    assert syncs(uv_calls) == 2
    assert not marker.exists()
    assert tool_dirs(tool_env, "uvrev-fx-app") == (directory,)
    assert json.loads((directory / RECEIPT_NAME).read_text())["created"] >= created


class TestCallerEnvironment:
    """Case 9: the caller's uv variables don't change the result"""

    def test_project_environment_and_frozen(
        self,
        capfd: pytest.CaptureFixture[str],
        tool_env: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        foreign = tmp_path / "foreign"
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(foreign))
        monkeypatch.setenv("UV_FROZEN", "1")
        monkeypatch.setenv("UV_NO_SYNC", "1")

        result = run("uvrev-fx-app")

        assert result.exit_code == 0, result.output
        assert not foreign.exists()
        (directory,) = tool_dirs(tool_env, "uvrev-fx-app")
        assert (directory / ".venv" / "bin" / "uvrev-fx-app").is_file()
        assert tool_output(capfd)["modules"]["six"] is True

    def test_frozen_does_not_skip_lock_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UV_FROZEN", "1")
        result = run("uvrev-fx-stale")
        assert result.exit_code == 1
        assert "the lock was outdated when the release was built" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as fake uv")
def test_killed_during_sync(
    tool_env: Path, tmp_path: Path, uv_calls: list[tuple[str, ...]]
) -> None:
    """Case 10: a run killed during uv sync leaves no receipt, the next one rebuilds"""
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "sync" ]; then
                kill -9 $PPID
                sleep 10
                exit 1
            fi
            exec {real_uv} "$@"
            """
        )
    )
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    killed = subprocess.run(
        (sys.executable, "-c", "from uvrev.cli import cli; cli()")
        + ("tool", "run", "uvrev-fx-app==1.0"),
        env=env,
        check=False,
    )

    assert killed.returncode == -9
    (directory,) = tool_dirs(tool_env, "uvrev-fx-app")
    assert (directory / "uv.lock").is_file()
    assert not (directory / RECEIPT_NAME).exists()

    result = run("uvrev-fx-app==1.0")
    assert result.exit_code == 0, result.output
    assert syncs(uv_calls) == 1
    assert (directory / RECEIPT_NAME).is_file()
