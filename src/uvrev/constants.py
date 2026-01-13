import os
from typing import Final

DEFAULT_PYTHON: Final[str] = os.environ.get("UV_PYTHON", "3.12")
DEFAULT_ENV: Final[str] = "base"
INITIAL_COMMIT_MSG: Final[str] = "(rev 0) Initial project creation"
