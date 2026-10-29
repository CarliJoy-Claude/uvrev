from dataclasses import dataclass
from pathlib import Path
from typing import Final

import typed_settings as ts
from platformdirs import user_config_path

APPNAME: Final[str] = "uvrev"


@dataclass(frozen=True)
class Settings:
    """Settings of uvrev

    >>> Settings()
    Settings(protect=True, protect_mode='a-w', unprotect_mode='u+w', compile_bytecode=True, verify_after_change=False)
    """

    # Make the .venv read-only, so it can only be changed by uvrev
    protect: bool = True
    # Mode for `chmod -R <mode> .venv` to protect the .venv
    protect_mode: str = "a-w"
    # Mode for `chmod -R <mode> .venv` to make the .venv writable again
    unprotect_mode: str = "u+w"
    # Compile bytecode on install, a protected .venv can not cache it later
    compile_bytecode: bool = True
    # Run `uvrev verify --fix` after every change of an environment
    verify_after_change: bool = False


def default_config_file() -> Path:
    """Location of the settings file, e.g. ~/.config/uvrev/config.toml on Linux"""
    return user_config_path(APPNAME) / "config.toml"


def load_settings() -> Settings:
    """Load the settings.

    Sources, later ones win:

    1. the defaults
    2. the ``[uvrev]`` table of the settings file (see :func:`default_config_file`)
    3. files listed in the environment variable ``UVREV_SETTINGS``
    4. environment variables like ``UVREV_PROTECT=false``

    Raises:
        typed_settings.exceptions.TsError: If the settings are invalid.
    """
    return ts.load(Settings, APPNAME, config_files=(default_config_file(),))
