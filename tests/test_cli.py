from pathlib import Path
from typing import Protocol

import pytest
from click.testing import CliRunner, Result

import uvrev.cli


class Invoker(Protocol):
    def __call__(self, *args: str) -> Result: ...


@pytest.fixture
def invoke() -> Invoker:
    """Create a CLI test runner."""
    runner = CliRunner()

    def invoke(*args: str) -> Result:
        return runner.invoke(uvrev.cli.cli, args=args)

    return invoke


@pytest.fixture(autouse=True)
def tmp_basedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base_dir = tmp_path / "basedir"
    monkeypatch.setenv("UVREV_BASE", str(base_dir))
    return base_dir


class TestCLIDefinition:
    """Tests for CLI commands."""

    # Commands require arguments
    @pytest.mark.parametrize(
        "command",
        [
            ["add"],
            ["remove"],
            ["restore"],
            ["run"],
        ],
    )
    def test_requires_arguments(self, invoke: Invoker, command: list[str]) -> None:
        """Commands require arguments."""
        result = invoke(*command)

        assert result.exit_code != 0

    # Env list in empty directory
    def test_env_list_empty(
        self, invoke: Invoker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env list shows message when empty."""
        result = invoke("env", "list")

        assert result.exit_code == 0
        assert "No projects found" in result.output

    # Help text is available
    @pytest.mark.parametrize(
        "command",
        [
            ["--help"],
            ["add", "--help"],
            ["remove", "--help"],
            ["env", "--help"],
            ["env", "create", "--help"],
        ],
        ids=lambda x: " ".join(x),
    )
    def test_help_available(self, invoke: Invoker, command: list[str]) -> None:
        """Help text is available."""
        result = invoke(*command)

        assert result.exit_code == 0
        assert "Usage:" in result.output or "Show this message" in result.output
