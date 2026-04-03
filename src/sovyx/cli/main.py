"""Sovyx CLI entry point."""

import typer

from sovyx import __version__

app = typer.Typer(
    name="sovyx",
    help="Sovyx — Sovereign Minds Engine",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print Sovyx version."""
    typer.echo(f"Sovyx v{__version__}")
