from pathlib import Path

import pytest
from conftest import Invoker
from typed_settings.exceptions import TsError

from uvrev import settings
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
        ],
    )
    def test_invalid(self, content: str, isolate_settings: Path) -> None:
        write(isolate_settings, f"[uvrev]\n{content}\n")

        with pytest.raises(TsError):
            load_settings()

    def test_default_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real location is in the platform specific config dir"""
        # undo the isolation, only to read the path
        monkeypatch.undo()
        path = settings.default_config_file()

        assert path.name == "config.toml"
        assert path.parent.name == "uvrev"


class TestCli:
    def test_show(self, invoke: Invoker, isolate_settings: Path) -> None:
        write(isolate_settings, '[uvrev]\nprotect_mode = "go-w"\n')

        result = invoke("settings")

        assert result.exit_code == 0
        assert str(isolate_settings) in result.output
        assert "protect = true" in result.output
        assert "protect_mode = 'go-w'" in result.output

    def test_invalid(self, invoke: Invoker, isolate_settings: Path) -> None:
        """Invalid settings stop every command with a readable message"""
        write(isolate_settings, '[uvrev]\nprotect = "maybe"\n')

        result = invoke("env", "list")

        assert result.exit_code == 1
        assert "Invalid settings" in result.output
        assert "protect" in result.output
