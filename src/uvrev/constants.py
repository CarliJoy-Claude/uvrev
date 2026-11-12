"""Names shared by several modules: file names of an environment and defaults.

Only plain values, this module imports nothing of uvrev.
"""

from typing import Final

DEFAULT_ENV: Final[str] = "base"
# environment variable with the name of the environment, if --env is not given
ENV_VARIABLE: Final[str] = "UVREV_ENV"
INITIAL_COMMIT_MSG: Final[str] = "(rev 0) Initial project creation"
# requested Python version (major.minor), read by uv
PYTHON_VERSION_FILE: Final[str] = ".python-version"
# exact version of the Python interpreter used by the environment, written by uvrev
PYTHON_VERSION_LOCK_FILE: Final[str] = ".python-version.lock"
# written as the last step of ``uvrev env create``, contains the uvrev version
CREATED_MARKER_FILE: Final[str] = ".uvrev_created_successfully"
# exists while the venv is unprotected by ``uvrev unprotect``, see marker.py
UNPROTECTED_MARKER_FILE: Final[str] = ".unprotected"
