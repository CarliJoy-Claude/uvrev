"""All constants of uvrev: file names, defaults, patterns and fixed texts.

Every constant lives here, also if only one module uses it, so they are found
in one place. This module must not import anything of uvrev.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import httpx2
from packaging.utils import NormalizedName, canonicalize_name
from platformdirs import user_config_path

DEFAULT_ENV: Final[str] = "base"
# environment variable with the name of the environment, if --env is not given
ENV_VARIABLE: Final[str] = "UVREV_ENV"
INITIAL_COMMIT_MSG: Final[str] = "(rev 0) Initial project creation"
PYPROJECT_FILE: Final[str] = "pyproject.toml"
LOCK_FILE: Final[str] = "uv.lock"
# Python request (e.g. a version or range), written by uv python pin and read by uv
PYTHON_VERSION_FILE: Final[str] = ".python-version"
# exact version of the Python interpreter used by the environment, written by uvrev
PYTHON_VERSION_LOCK_FILE: Final[str] = ".python-version.lock"
# written as the last step of ``uvrev env create``, contains the uvrev version
CREATED_MARKER_FILE: Final[str] = ".uvrev_created_successfully"
# in the venv: uv locks the venv with it, also to only read it (uv sync --check)
VENV_LOCK_FILE: Final[str] = ".lock"
# exists while the venv is unprotected by ``uvrev unprotect``, see marker.py
UNPROTECTED_MARKER_FILE: Final[str] = ".unprotected"
# name of the entry for Python where it is compared like a package
# (its exact version and its pinned request)
PYTHON_NAME: Final[NormalizedName] = canonicalize_name("python")

# --- cli.py
INCOMPLETE_HINT = " (not created successfully, run 'uvrev env fix {name}' if it works)"

# --- commands.py
# stdout of internal commands is redirected here, so uvrev's stdout stays parseable
STDERR_FD = 2

# --- interpreter.py
# Prints the version of the running interpreter, PEP 440 compatible
# (platform.python_version() can be e.g. "3.15.0a1+" which is no valid version)
PRINT_PYTHON_VERSION = """
import sys
v = sys.version_info
pre = {"alpha": "a", "beta": "b", "candidate": "rc"}.get(v.releaselevel)
print(f"{v.major}.{v.minor}.{v.micro}" + (f"{pre}{v.serial}" if pre else ""))
"""

# --- marker.py
# Seconds between two looks at the clock of the filesystem. Old filesystems
# (ext3 with 128 byte inodes) only store full seconds.
CLOCK_INTERVAL: Final[float] = 0.1
# Signals that would stop uvrev while it makes the venv writable
INTERRUPTING_SIGNALS: Final[tuple[str, ...]] = ("SIGINT", "SIGTERM", "SIGHUP")

# --- parser.py
# "any version": how a requirement without version is given to uv, as uv keeps
# an existing constraint for a bare name. It is the same as no constraint.
ANY_VERSION: Final[str] = ">=0"
# a file name like pkg-1.0-py3-none-any.whl parses as requirement, but is a path
ARCHIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".whl",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
)

# --- report.py
SCHEMA_VERSION: Final[int] = 2

# --- reporting.py
# key in click.Context.meta: error found while parsing the options, raised when
# the command is invoked, so it is reported like the failures of the command
DEFERRED_ERROR = "uvrev.deferred_error"

# --- settings.py
APPNAME: Final[str] = "uvrev"
CONFIG_FILE_NAME: Final[str] = "config.toml"
# environment variable of the setting ``python_versions_supported``
PYTHON_VERSIONS_SUPPORTED_ENV: Final[str] = "UVREV_PYTHON_VERSIONS_SUPPORTED"
PYTHON_MINOR_VERSION: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+")

# --- tool.py
RECEIPT_NAME: Final[str] = "uvrev-receipt.json"
EXTRA_METADATA: Final[str] = "extra_metadata"
EMBEDDED_FILES: Final[tuple[str, ...]] = ("uv.lock", "pyproject.toml")
# Variables of the caller that would change how the environment is built
IGNORED_VARIABLES: Final[frozenset[str]] = frozenset(
    {"UV_FROZEN", "UV_NO_SYNC", "UV_LOCKED", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}
)
# Dynamic fields that make uv build the metadata, which needs the source tree
DYNAMIC_NEEDS_SOURCE: Final[tuple[str, ...]] = (
    "dependencies",
    "optional-dependencies",
    "requires-python",
)
# connect, read, write and pool timeout of each operation, not of the whole download
DOWNLOAD_TIMEOUT: Final[httpx2.Timeout] = httpx2.Timeout(30.0)
SHA256_HEX_LENGTH: Final[int] = 64
SUPPORTED_SPEC: Final[str] = (
    "Give 'name', 'name==version' or the path or URL of a wheel"
)

# --- verify.py
# Problems fixed by reinstalling the package
REINSTALL_PROBLEMS: Final[frozenset[str]] = frozenset({"missing", "modified"})
# The files a venv consists of itself, they are not installed by a package and
# can not be verified or restored (only by creating the venv again).
# In the root of the venv:
VENV_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {"pyvenv.cfg", ".gitignore", "CACHEDIR.TAG", VENV_LOCK_FILE}
)
# next to the interpreter (bin or Scripts):
VENV_SCRIPTS: Final[frozenset[str]] = frozenset(
    {
        "activate",
        "activate.bat",
        "activate.csh",
        "activate.fish",
        "activate.nu",
        "activate.ps1",
        "activate.xsh",
        "activate_this.py",
        "deactivate.bat",
        "pydoc.bat",
    }
)
VENV_INTERPRETER: Final[re.Pattern[str]] = re.compile(
    r"(python|pypy)w?[0-9.]*t?(\.exe)?"
)
SCRIPT_DIRECTORIES: Final[frozenset[str]] = frozenset({"bin", "Scripts"})
# in site-packages:
VENV_SITE_FILES: Final[frozenset[str]] = frozenset(
    {"_virtualenv.py", "_virtualenv.pth"}
)
PROBLEM_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "missing": "missing",
        "modified": "modified",
        "not_synced": "not in sync with uv.lock",
        "untracked": "not installed by any package",
    }
)

# --- repair.py
# Files of the venv itself that uv creates or rewrites while using the venv
# (e.g. its lock). A change of them is harmless.
VOLATILE_VENV_FILES: Final[frozenset[str]] = frozenset(VENV_ROOT_FILES - {"pyvenv.cfg"})


def config_file() -> Path:
    """Location of the settings file, e.g. ~/.config/uvrev/config.toml on Linux.

    A function and no constant: it depends on environment variables like
    ``XDG_CONFIG_HOME``, which can change after this module was imported
    (the tests use this to never read the settings of the user).
    """
    return user_config_path(APPNAME) / CONFIG_FILE_NAME
