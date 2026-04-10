"""CLI command: sovyx dashboard — show dashboard access info."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from sovyx.dashboard.server import TOKEN_FILE

console = Console()
dashboard_app = typer.Typer(help="Dashboard management")


@dashboard_app.callback(invoke_without_command=True)
def dashboard_info(
    ctx: typer.Context,
    show_token: bool = typer.Option(False, "--token", "-t", help="Show the auth token"),
) -> None:
    """Show dashboard access information."""
    if ctx.invoked_subcommand is not None:
        return

    # Read config for host/port (defaults)
    host = "127.0.0.1"
    port = 7777

    config_path = Path.home() / ".sovyx" / "system.yaml"
    if config_path.exists():
        try:
            import yaml

            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            api_cfg = cfg.get("api", {})
            host = api_cfg.get("host", host)
            port = api_cfg.get("port", port)
        except Exception:  # noqa: BLE001  # nosec B110 — best-effort config read; defaults are safe
            pass

    url = f"http://{host}:{port}"

    console.print()
    console.print("[bold]Sovyx Dashboard[/bold]")
    console.print()
    console.print(f"  URL:   [link={url}]{url}[/link]")

    if show_token:
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text().strip()
            console.print(f"  Token: [dim]{token}[/dim]")
        else:
            console.print("  Token: [yellow]Not generated yet (start sovyx first)[/yellow]")
    else:
        console.print("  Token: [dim]use --token to reveal[/dim]")

    console.print()
    console.print("[dim]Start the daemon with 'sovyx start' to access the dashboard.[/dim]")
    console.print()
