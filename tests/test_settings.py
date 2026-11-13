import sys
from pathlib import Path

import pytest
from conftest import Invoker
from typed_settings.exceptions import TsError

from uvrev import constants, settings
from uvrev.settings import Settings, load_settings


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestLoadSettings:
    def test_defaults(self) -> None:
        """Without a settings file the defaults are used"""
        assert load_settings() == Settings()

    def test_file(self, isolate_settings: Path) -> None:
        write(
            isolate_settings,
            '[uvrev]\nprotect_mode = "go-w"\nunprotect_mode = "ug+w"\n',
        )

        assert load_settings() == Settings(protect_mode="go-w", unprotect_mode="ug+w")

    def test_env_wins(
        self, isolate_settings: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write(isolate_settings, "[uvrev]\nprotect = true\n")
        monkeypatch.setenv("UVREV_PROTECT", "false")

        assert load_settings() == Settings(protect=False)

    def test_extra_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        extra = write(tmp_path / "extra.toml", "[uvrev]\ncompile_bytecode = false\n")
        monkeypatch.setenv("UVREV_SETTINGS", str(extra))

        assert load_settings().compile_bytecode is False

    @pytest.mark.parametrize(
        "content",
        [
            'protect = "maybe"',
            "unknown = 1",
            'python_versions_supported = ["3.x"]',
        ],
    )
    def test_invalid(self, content: str, isolate_settings: Path) -> None:
        write(isolate_settings, f"[uvrev]\n{content}\n")

        with pytest.raises(TsError):
            load_settings()

    def test_location(self) -> None:
        """The settings file is in the platform specific config dir"""
        path = constants.config_file()

        assert path.name == "config.toml"
        assert path.parent.name == "uvrev"


class TestPythonVersionsSupported:
    def test_default_installed(self) -> None:
        """By default the Python versions installed for uv are used"""
        installed = settings.installed_python_versions()

        assert Settings().python_versions_supported == installed
        # the Python running the tests is installed for uv as well
        assert f"{sys.version_info.major}.{sys.version_info.minor}" in installed

    def test_file(self, isolate_settings: Path) -> None:
        write(isolate_settings, '[uvrev]\npython_versions_supported = ["3.12"]\n')

        assert load_settings().python_versions_supported == ("3.12",)

    @pytest.mark.parametrize("value", ["3.12:3.14", "3.12,3.14", "3.12, 3.14"])
    def test_env(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """typed-settings splits at ":", "," is split by uvrev"""
        monkeypatch.setenv(constants.PYTHON_VERSIONS_SUPPORTED_ENV, value)

        assert load_settings().python_versions_supported == ("3.12", "3.14")

    @pytest.mark.parametrize("value", ["3", "3.12.1", "abc", "3.x"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"must be given as major\.minor"):
            Settings(python_versions_supported=(value,))


class TestCli:
    def test_show(self, invoke: Invoker, isolate_settings: Path) -> None:
        write(isolate_settings, '[uvrev]\nprotect_mode = "go-w"\n')

        result = invoke("settings")

        assert result.exit_code == 0
        assert str(isolate_settings) in result.output
        assert "protect = true" in result.output
        assert 'protect_mode = "go-w"' in result.output
        assert "python_versions_supported = [" in result.output

    def test_invalid(self, invoke: Invoker, isolate_settings: Path) -> None:
        """Invalid settings stop every command with a readable message"""
        write(isolate_settings, '[uvrev]\nprotect = "maybe"\n')

        result = invoke("env", "list")

        assert result.exit_code == 1
        assert "Invalid settings" in result.output
        assert "protect" in result.output
