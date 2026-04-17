"""Sovyx CLI — command-line interface for the Sovyx daemon."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sovyx import __version__
from sovyx.cli.commands.brain_analyze import analyze_app
from sovyx.cli.commands.dashboard import dashboard_app
from sovyx.cli.commands.logs import logs_app
from sovyx.cli.commands.plugin import plugin_app
from sovyx.cli.rpc_client import DaemonClient
from sovyx.dashboard.server import TOKEN_FILE

console = Console()  # pragma: no cover
app = typer.Typer(
    name="sovyx",
    help="Sovyx — Sovereign Minds Engine",
    no_args_is_help=True,
)
brain_app = typer.Typer(name="brain", help="Brain memory commands")
mind_app = typer.Typer(name="mind", help="Mind management commands")
brain_app.add_typer(analyze_app)
app.add_typer(brain_app)
app.add_typer(mind_app)
app.add_typer(logs_app)
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(plugin_app, name="plugin")


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
def token(
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy token to clipboard"),
) -> None:
    """Show the dashboard authentication token.

    Quick access to the API token needed for dashboard login.
    Equivalent to `sovyx dashboard --token`.
    """
    if not TOKEN_FILE.exists():
        console.print(
            "[yellow]Token not generated yet.[/yellow]\n"
            "[dim]Start Sovyx first: [bold]sovyx start[/bold][/dim]",
        )
        raise typer.Exit(1)

    token_value = TOKEN_FILE.read_text().strip()
    if not token_value:
        console.print("[red]Token file is empty.[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]🔑 Dashboard Token[/bold]\n\n  {token_value}\n")

    if copy:
        try:
            import subprocess

            subprocess.run(  # noqa: S603, S607
                ["xclip", "-selection", "clipboard"],
                input=token_value.encode(),
                check=True,
                capture_output=True,
            )
            console.print("[green]✓ Copied to clipboard[/green]")
        except (FileNotFoundError, subprocess.CalledProcessError):
            try:
                subprocess.run(  # noqa: S603, S607
                    ["pbcopy"],
                    input=token_value.encode(),
                    check=True,
                    capture_output=True,
                )
                console.print("[green]✓ Copied to clipboard[/green]")
            except (FileNotFoundError, subprocess.CalledProcessError):
                console.print("[dim]Clipboard not available — copy manually.[/dim]")

    console.print("[dim]Use this token to authenticate with the dashboard.[/dim]\n")


_MIND_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _validate_mind_name(name: str) -> str:
    """Validate mind-name before using it in filesystem paths.

    Prevents path traversal (``../``), absolute paths, separators, and other
    filesystem-dangerous characters. The resulting name is lowercased for the
    filesystem directory but the original is kept for display.

    Raises:
        typer.BadParameter: if the name contains disallowed characters or is
            longer than 64 characters.
    """
    if not _MIND_NAME_PATTERN.match(name):
        raise typer.BadParameter(
            f"Invalid mind name {name!r}: must match "
            f"^[A-Za-z][A-Za-z0-9_-]{{0,63}}$ (ASCII letters/digits/_/-, "
            f"starting with a letter, 1-64 chars).",
            param_hint="name",
        )
    return name


@app.command()
def init(
    name: str = typer.Argument(
        "Sovyx",
        help="Mind name (letters/digits/_/-, 1-64 chars, starts with letter)",
    ),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode: defaults, zero prompts"),
) -> None:
    """Initialize Sovyx: create config files and data directory."""

    name = _validate_mind_name(name)

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

    # Create logs directory
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if not any(logs_dir.iterdir()):
        console.print(f"[green]✓[/green] Created {logs_dir}")
    else:
        console.print(f"[dim]• {logs_dir} already exists[/dim]")

    # Create mind.yaml using full config (auto-detects LLM provider from env)
    mind_dir = data_dir / name.lower()
    mind_dir.mkdir(parents=True, exist_ok=True)
    mind_yaml = mind_dir / "mind.yaml"
    if not mind_yaml.exists():
        from sovyx.mind.config import create_default_mind_config

        create_default_mind_config(name, mind_dir)
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
    from sovyx.engine.config import load_engine_config
    from sovyx.engine.events import EventBus
    from sovyx.engine.lifecycle import LifecycleManager
    from sovyx.engine.rpc_server import DaemonRPCServer
    from sovyx.mind.config import MindConfig

    async def _start() -> None:
        system_yaml = Path.home() / ".sovyx" / "system.yaml"
        config = load_engine_config(config_path=system_yaml if system_yaml.exists() else None)
        mind_config = MindConfig(name="Sovyx")  # v0.1: single mind

        # Load mind.yaml — discover first mind directory
        mind_yaml: Path | None = None
        data_dir = config.database.data_dir
        if data_dir.exists():
            for child in sorted(data_dir.iterdir()):
                candidate = child / "mind.yaml"
                if child.is_dir() and candidate.exists():
                    mind_yaml = candidate
                    break

        if mind_yaml is not None and mind_yaml.exists():
            import yaml

            with open(mind_yaml) as f:  # noqa: PTH123
                mind_data = yaml.safe_load(f)
            if mind_data:
                mind_config = MindConfig(**mind_data)

        registry = await bootstrap(config, [mind_config])
        event_bus = await registry.resolve(EventBus)

        # Setup RPC server
        rpc = DaemonRPCServer()
        rpc.register_method("status", lambda: {"version": __version__, "status": "running"})
        rpc.register_method("shutdown", lambda: "ok")

        # CLI surface — chat, mind.list, config.get (SPE-015 §2).
        from sovyx.engine._rpc_handlers import register_cli_handlers

        register_cli_handlers(rpc, registry)

        await rpc.start()
        registry.register_instance(DaemonRPCServer, rpc)

        lifecycle = LifecycleManager(registry, event_bus)
        await lifecycle.start()

        console.print("[bold green]Sovyx daemon started[/bold green]")
        await lifecycle.run_forever()

    _run(_start())


@app.command()
def chat() -> None:  # pragma: no cover — interactive REPL
    """Open an interactive chat REPL with the active mind.

    Slash commands (``/help``, ``/status``, ``/minds``, ``/config``,
    ``/new``, ``/clear``, ``/exit``) work inline. History persists
    across sessions in ``~/.sovyx/history``. Press Ctrl+D to leave.

    Requires the daemon to be running (``sovyx start``).
    """
    from sovyx.cli.chat import run_repl

    raise typer.Exit(run_repl())


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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
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
        except Exception as exc:  # noqa: BLE001 — CLI boundary — renders RPC failure to doctor table; pragma: no cover
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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
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
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
