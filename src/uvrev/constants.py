from typing import Final

DEFAULT_ENV: Final[str] = "base"
INITIAL_COMMIT_MSG: Final[str] = "(rev 0) Initial project creation"
# requested Python version (major.minor), read by uv
PYTHON_VERSION_FILE: Final[str] = ".python-version"
# exact version of the Python interpreter used by the environment, written by uvrev
PYTHON_VERSION_LOCK_FILE: Final[str] = ".python-version.lock"
# Python versions (major.minor, comma separated) for ``tool.uv.environments``,
# if not set, the installed Python versions are used
SUPPORT_PYTHON_VERSION_ENV: Final[str] = "UVREV_SUPPORT_PYTHON_VERSION"
# written as the last step of ``uvrev env create``, contains the uvrev version
CREATED_MARKER_FILE: Final[str] = ".uvrev_created_successfully"
