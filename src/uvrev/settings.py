import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import typed_settings as ts
from platformdirs import user_config_path

APPNAME: Final[str] = "uvrev"
# replaces the default location of the settings file
CONFIG_FILE_ENV: Final[str] = "UVREV_CONFIG_FILE"


@dataclass(frozen=True)
class Settings:
    """Settings of uvrev

    >>> Settings()
    Settings(protect=True, chmod_command='chmod', protect_mode='a-w', unprotect_mode='u+w', compile_bytecode=True, support_python_version='', verify_after_change=False)
    """

    # Make the .venv read-only, so it can only be changed by uvrev
    protect: bool = True
    # Command used to change the permissions (name or path)
    chmod_command: str = "chmod"
    # Mode for `chmod -R <mode> .venv` to protect the .venv
    protect_mode: str = "a-w"
    # Mode for `chmod -R <mode> .venv` to make the .venv writable again
    unprotect_mode: str = "u+w"
    # Compile bytecode on install, a protected .venv can not cache it later
    compile_bytecode: bool = True
    # Python versions (major.minor, comma separated, e.g. "3.12,3.14") the
    # environments are resolved for (tool.uv.environments),
    # if empty the installed Python versions are used
    support_python_version: str = ""
    # Run `uvrev verify --fix` after every change of an environment
    verify_after_change: bool = False


def default_config_file() -> Path:
    """Location of the settings file, e.g. ~/.config/uvrev/config.toml on Linux

    It can be replaced with the environment variable ``UVREV_CONFIG_FILE``.
    """
    if custom := os.getenv(CONFIG_FILE_ENV):
        return Path(custom)
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
