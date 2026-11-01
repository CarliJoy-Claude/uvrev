from typing import Final

DEFAULT_ENV: Final[str] = "base"
INITIAL_COMMIT_MSG: Final[str] = "(rev 0) Initial project creation"
# requested Python version (major.minor), read by uv
PYTHON_VERSION_FILE: Final[str] = ".python-version"
# exact version of the Python interpreter used by the environment, written by uvrev
PYTHON_VERSION_LOCK_FILE: Final[str] = ".python-version.lock"
