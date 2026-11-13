"""Names shared by several modules: file names of an environment and defaults.

Only values, this module imports nothing of uvrev.
"""

from typing import Final

from packaging.utils import NormalizedName, canonicalize_name

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
