"""The settings of uvrev: their defaults, validation and where they come from.

Loaded with typed-settings from the ``[uvrev]`` table of the config file, the
files in ``UVREV_SETTINGS`` and ``UVREV_*`` environment variables.
Its constants and the location of the config file are in ``constants.py``.
"""

import json
import subprocess
from dataclasses import dataclass, field
from functools import cache

import typed_settings as ts

from . import constants


@cache
def installed_python_versions(uv: str = "uv") -> tuple[str, ...]:
    """The Python versions (major.minor) installed for uv, sorted

    Cached, as the settings are created often and uv is asked only once.
    Empty if uv is not installed: the settings are also needed by commands
    that work without uv, and the others report the missing uv themselves.

    Args:
        uv: Name of the uv command, started from PATH like everywhere in uvrev
            (commands.py can not be used here, it imports the settings itself).
    """
    try:
        output = subprocess.run(
            (uv, "python", "list", "--only-installed", "--output-format", "json"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        return ()
    versions = {
        tuple(int(part) for part in entry["version"].split(".")[:2])
        for entry in json.loads(output)
    }
    return tuple(f"{major}.{minor}" for major, minor in sorted(versions))


def split_versions(versions: tuple[str, ...]) -> tuple[str, ...]:
    """Split entries like "3.12,3.14" and check the format major.minor

    typed-settings splits environment variables at ":" only, so the comma
    separated form of ``UVREV_PYTHON_VERSIONS_SUPPORTED`` works as well.

    >>> split_versions(("3.12, 3.14", "3.13"))
    ('3.12', '3.14', '3.13')
    >>> split_versions(("3.x",))
    Traceback (most recent call last):
    ...
    ValueError: python_versions_supported (UVREV_PYTHON_VERSIONS_SUPPORTED) must be given as major.minor (e.g. 3.12), not '3.x'
    """
    split = tuple(
        version.strip()
        for entry in versions
        for version in entry.split(",")
        if version.strip()
    )
    for version in split:
        if not constants.PYTHON_MINOR_VERSION.fullmatch(version):
            raise ValueError(
                f"python_versions_supported ({constants.PYTHON_VERSIONS_SUPPORTED_ENV}) must be "
                f"given as major.minor (e.g. 3.12), not {version!r}"
            )
    return split


@dataclass(frozen=True)
class Settings:
    """Settings of uvrev

    >>> Settings(python_versions_supported=("3.12",))
    Settings(protect=True, chmod_command='chmod', protect_mode='a-w', unprotect_mode='u+w', compile_bytecode=True, python_versions_supported=('3.12',), verify_after_change=False)
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
    # Python versions (major.minor) the environments are resolved for
    # (tool.uv.environments), by default the Python versions installed for uv
    python_versions_supported: tuple[str, ...] = field(
        default_factory=installed_python_versions
    )
    # Run `uvrev verify --fix` after every change of an environment
    verify_after_change: bool = False

    def __post_init__(self) -> None:
        # frozen dataclass: bypass __setattr__ to store the split versions
        object.__setattr__(
            self,
            "python_versions_supported",
            split_versions(self.python_versions_supported),
        )


def load_settings() -> Settings:
    """Load the settings.

    Sources, later ones win:

    1. the defaults
    2. the ``[uvrev]`` table of the settings file (see :func:`config_file`)
    3. files listed in the environment variable ``UVREV_SETTINGS``
       (separated by ``:``, ``config_files_var`` of typed-settings)
    4. environment variables like ``UVREV_PROTECT=false``

    Raises:
        typed_settings.exceptions.TsError: If the settings are invalid.
    """
    return ts.load(Settings, constants.APPNAME, config_files=(constants.config_file(),))
