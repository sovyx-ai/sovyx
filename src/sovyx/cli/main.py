"""Sovyx CLI entry point."""

from __future__ import annotations

import typer

from sovyx import __version__

app = typer.Typer(
    name="sovyx",
    help="Sovyx — Sovereign Minds Engine",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit"),
) -> None:
    """Sovyx — Sovereign Minds Engine."""
    if version:
        typer.echo(f"Sovyx v{__version__}")
        raise typer.Exit
