"""Sovyx CLI — command-line interface for the Sovyx daemon."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sovyx import __version__
from sovyx.cli.commands.dashboard import dashboard_app
from sovyx.cli.commands.logs import logs_app
from sovyx.cli.rpc_client import DaemonClient

console = Console()  # pragma: no cover
app = typer.Typer(
    name="sovyx",
    help="Sovyx — Sovereign Minds Engine",
    no_args_is_help=True,
)
brain_app = typer.Typer(name="brain", help="Brain memory commands")
mind_app = typer.Typer(name="mind", help="Mind management commands")
app.add_typer(brain_app)
app.add_typer(mind_app)
app.add_typer(logs_app)
app.add_typer(dashboard_app, name="dashboard")


def _get_client() -> DaemonClient:
    """Get daemon client."""
    return DaemonClient()


def _run(coro: object) -> object:  # pragma: no cover
    """Run async coroutine in sync context."""
    return asyncio.run(coro)  # type: ignore[arg-type]


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    """Sovyx CLI entry point."""
    if version:
        console.print(f"sovyx {__version__}")
        raise typer.Exit()


@app.command()
def init(
    name: str = typer.Argument("Aria", help="Mind name"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode: defaults, zero prompts"),
) -> None:
    """Initialize Sovyx: create config files and data directory."""

    data_dir = Path.home() / ".sovyx"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create system.yaml if not exists
    system_yaml = data_dir / "system.yaml"
    if not system_yaml.exists():
        system_yaml.write_text(
            "# Sovyx system configuration\n# See https://docs.sovyx.ai/config for options\n"
        )
        console.print(f"[green]✓[/green] Created {system_yaml}")
    else:
        console.print(f"[dim]• {system_yaml} already exists[/dim]")

    # Create mind.yaml
    mind_dir = data_dir / name.lower()
    mind_dir.mkdir(parents=True, exist_ok=True)
    mind_yaml = mind_dir / "mind.yaml"
    if not mind_yaml.exists():
        mind_yaml.write_text(
            f"name: {name}\n"
            f"language: en\n"
            f"personality:\n"
            f"  openness: 0.7\n"
            f"  conscientiousness: 0.8\n"
            f"  extraversion: 0.5\n"
            f"  agreeableness: 0.7\n"
            f"  neuroticism: 0.3\n"
        )
        console.print(f"[green]✓[/green] Created mind '{name}' at {mind_yaml}")
    else:
        console.print(f"[dim]• Mind '{name}' already exists[/dim]")

    console.print("\n[bold green]Sovyx initialized![/bold green]")
    console.print(f"Data directory: {data_dir}")
    console.print("\nNext: [bold]sovyx start[/bold] to launch the daemon")


@app.command()
def start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
) -> None:  # pragma: no cover
    """Start the Sovyx daemon."""
    client = _get_client()
    if client.is_daemon_running():
        console.print("[red]Sovyx daemon is already running[/red]")
        raise typer.Exit(1)

    console.print("[bold]Starting Sovyx daemon...[/bold]")

    from sovyx.engine.bootstrap import bootstrap
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.events import EventBus
    from sovyx.engine.lifecycle import LifecycleManager
    from sovyx.engine.rpc_server import DaemonRPCServer
    from sovyx.mind.config import MindConfig

    async def _start() -> None:
        config = EngineConfig()
        mind_config = MindConfig(name="Aria")  # v0.1: single mind

        # Load mind.yaml if exists
        mind_yaml = config.database.data_dir / "aria" / "mind.yaml"
        if mind_yaml.exists():
            import yaml

            with open(mind_yaml) as f:
                mind_data = yaml.safe_load(f)
            if mind_data:
                mind_config = MindConfig(**mind_data)

        registry = await bootstrap(config, [mind_config])
        event_bus = await registry.resolve(EventBus)

        # Setup RPC server
        rpc = DaemonRPCServer()
        rpc.register_method("status", lambda: {"version": __version__, "status": "running"})
        rpc.register_method("shutdown", lambda: "ok")
        await rpc.start()
        registry.register_instance(DaemonRPCServer, rpc)

        lifecycle = LifecycleManager(registry, event_bus)
        await lifecycle.start()

        console.print("[bold green]Sovyx daemon started[/bold green]")
        await lifecycle.run_forever()

    _run(_start())


@app.command()
def stop() -> None:
    """Stop the Sovyx daemon."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[yellow]Sovyx daemon is not running[/yellow]")
        raise typer.Exit(1)

    try:
        _run(client.call("shutdown"))
        console.print("[green]Sovyx daemon stopped[/green]")
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Failed to stop daemon: {e}[/red]")
        raise typer.Exit(1) from None


@app.command()
def status() -> None:
    """Show daemon status."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[yellow]Sovyx daemon is not running[/yellow]")
        raise typer.Exit(1)

    try:
        result = _run(client.call("status"))
        if isinstance(result, dict):
            table = Table(title="Sovyx Status")
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="green")
            for k, v in result.items():
                table.add_row(str(k), str(v))
            console.print(table)
        else:
            console.print(result)
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command()
def doctor(
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Run health checks on the Sovyx installation.

    Offline checks (always available): disk, RAM, CPU, model files, config.
    Online checks (daemon required): database, brain, LLM, channels,
    consolidation, cost budget.
    """
    import asyncio
    import json

    from sovyx.observability.health import (
        CheckStatus,
        HealthRegistry,
        create_offline_registry,
    )

    results = []

    # ── Tier 1: Offline checks (always run) ─────────────────────────
    offline = create_offline_registry()
    offline_results = asyncio.run(offline.run_all(timeout=10.0))
    results.extend(offline_results)

    # Config validation (extra offline check)
    data_dir = Path.home() / ".sovyx"
    config_file = data_dir / "system.yaml"
    from sovyx.observability.health import CheckResult

    if config_file.exists():
        results.append(
            CheckResult(
                name="Config",
                status=CheckStatus.GREEN,
                message=f"system.yaml found ({config_file.stat().st_size} bytes)",
            )
        )
    else:
        results.append(
            CheckResult(
                name="Config",
                status=CheckStatus.YELLOW,
                message="system.yaml not found (using defaults)",
            )
        )

    # ── Tier 2: Online checks (daemon required) ─────────────────────
    client = _get_client()
    daemon_running = client.is_daemon_running()

    if daemon_running:
        try:
            rpc_result = _run(client.call("doctor"))
            if isinstance(rpc_result, dict):
                for name, check_data in rpc_result.get("checks", {}).items():
                    if isinstance(check_data, dict):
                        status_str = check_data.get("status", "green")
                        status = CheckStatus(status_str)
                        results.append(
                            CheckResult(
                                name=name,
                                status=status,
                                message=check_data.get("message", ""),
                                metadata=check_data.get("metadata") or {},
                            )
                        )
                    else:
                        ok = bool(check_data)
                        results.append(
                            CheckResult(
                                name=name,
                                status=CheckStatus.GREEN if ok else CheckStatus.RED,
                                message="ok" if ok else "failed",
                            )
                        )
        except Exception as exc:  # pragma: no cover
            results.append(
                CheckResult(
                    name="Daemon RPC",
                    status=CheckStatus.RED,
                    message=f"RPC call failed: {exc}",
                )
            )

    # ── Output ──────────────────────────────────────────────────────
    if output_json:
        import dataclasses

        json_out = [dataclasses.asdict(r) for r in results]
        for item in json_out:
            item["status"] = item["status"].value
        typer.echo(json.dumps(json_out, indent=2))
        return

    from rich.table import Table

    status_icon = {
        CheckStatus.GREEN: "[green]✓[/green]",
        CheckStatus.YELLOW: "[yellow]⚠[/yellow]",
        CheckStatus.RED: "[red]✗[/red]",
    }

    table = Table(title="Sovyx Health Check", show_lines=False)
    table.add_column("", width=3)
    table.add_column("Check", min_width=20)
    table.add_column("Status", min_width=10)
    table.add_column("Message")

    for r in results:
        icon = status_icon.get(r.status, "?")
        status_style = {
            CheckStatus.GREEN: "green",
            CheckStatus.YELLOW: "yellow",
            CheckStatus.RED: "red",
        }.get(r.status, "white")
        table.add_row(
            icon,
            r.name,
            f"[{status_style}]{r.status.value}[/{status_style}]",
            r.message,
        )

    console.print(table)

    if not daemon_running:
        console.print(
            "\n[yellow]Daemon not running — showing offline checks only.[/yellow]"
            "\n[dim]Start the daemon for full health checks "
            "(database, brain, LLM, channels).[/dim]"
        )

    # Summary using HealthRegistry.summary()
    overall = HealthRegistry().summary(results)
    greens = sum(1 for r in results if r.status == CheckStatus.GREEN)
    yellows = sum(1 for r in results if r.status == CheckStatus.YELLOW)
    reds = sum(1 for r in results if r.status == CheckStatus.RED)
    total = len(results)

    overall_style = {
        CheckStatus.GREEN: "green",
        CheckStatus.YELLOW: "yellow",
        CheckStatus.RED: "red",
    }.get(overall, "white")

    console.print(
        f"\n[{overall_style} bold]{overall.value.upper()}[/{overall_style} bold] — "
        f"[bold]{greens}[/bold]/{total} passed"
        + (f", [yellow]{yellows} warnings[/yellow]" if yellows else "")
        + (f", [red]{reds} critical[/red]" if reds else "")
    )


# Brain commands
@brain_app.command("search")
def brain_search(
    query: str = typer.Argument(..., help="Search query"),
    mind: str = typer.Option("default", help="Mind ID"),
    limit: int = typer.Option(5, help="Max results"),
) -> None:
    """Search concepts in the brain."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[red]Daemon not running[/red]")
        raise typer.Exit(1)

    try:
        params = {"query": query, "mind_id": mind, "limit": limit}
        result = _run(client.call("brain.search", params))
        if isinstance(result, list):
            for item in result:
                console.print(f"  • {item}")
        else:
            console.print(result)
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Error: {e}[/red]")


@brain_app.command("stats")
def brain_stats(
    mind: str = typer.Option("default", help="Mind ID"),
) -> None:
    """Show brain statistics."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[red]Daemon not running[/red]")
        raise typer.Exit(1)

    try:
        result = _run(client.call("brain.stats", {"mind_id": mind}))
        if isinstance(result, dict):
            for k, v in result.items():
                console.print(f"  {k}: [cyan]{v}[/cyan]")
        else:
            console.print(result)
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Error: {e}[/red]")


# Mind commands
@mind_app.command("list")
def mind_list() -> None:
    """List active minds."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[red]Daemon not running[/red]")
        raise typer.Exit(1)

    try:
        result = _run(client.call("mind.list"))
        if isinstance(result, list):
            for m in result:
                console.print(f"  • [cyan]{m}[/cyan]")
        else:
            console.print(result)
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Error: {e}[/red]")


@mind_app.command("status")
def mind_status(
    name: str = typer.Argument("default", help="Mind name"),
) -> None:
    """Show mind status."""
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[red]Daemon not running[/red]")
        raise typer.Exit(1)

    try:
        result = _run(client.call("mind.status", {"mind_id": name}))
        if isinstance(result, dict):
            for k, v in result.items():
                console.print(f"  {k}: [cyan]{v}[/cyan]")
        else:
            console.print(result)
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
