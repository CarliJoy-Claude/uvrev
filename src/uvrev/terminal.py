import sys
from typing import NoReturn

import click


def success(msg: str) -> None:
    click.secho(f"✅ {msg}", fg="green", err=True)


def warning(msg: str) -> None:
    click.secho(f"⚠️ {msg}", fg="yellow", err=True)


def error(msg: str) -> None:
    click.secho(f"❌ {msg}", fg="red", err=True)


def fatal_error(msg: str, exit_code: int = 1) -> NoReturn:
    error(msg)
    sys.exit(exit_code)
