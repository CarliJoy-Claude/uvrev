import click


def success(msg: str) -> None:
    click.secho(f"✅ {msg}", fg="green", err=True)


def warning(msg: str) -> None:
    click.secho(f"⚠️ {msg}", fg="yellow", err=True)


def error(msg: str) -> None:
    click.secho(f"❌ {msg}", fg="red", err=True)
