"""Sovyx CLI — command-line interface for the Sovyx daemon."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sovyx import __version__
from sovyx.cli.commands.audit import audit_app
from sovyx.cli.commands.brain_analyze import analyze_app
from sovyx.cli.commands.dashboard import dashboard_app
from sovyx.cli.commands.doctor import doctor_app
from sovyx.cli.commands.kb import kb_app
from sovyx.cli.commands.logs import logs_app
from sovyx.cli.commands.plugin import plugin_app
from sovyx.cli.commands.voice import voice_app
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
retention_app = typer.Typer(
    name="retention",
    help="Per-mind retention policy (GDPR Art. 5(1)(e) / LGPD Art. 16)",
)
mind_app.add_typer(retention_app)
brain_app.add_typer(analyze_app)
app.add_typer(brain_app)
app.add_typer(mind_app)
app.add_typer(logs_app)
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(doctor_app)
app.add_typer(plugin_app, name="plugin")
app.add_typer(audit_app)
app.add_typer(kb_app, name="kb")
app.add_typer(voice_app)


def _get_client() -> DaemonClient:
    """Get daemon client."""
    return DaemonClient()


def _surface_preflight_warnings() -> None:
    """Print any boot-preflight warnings persisted by the voice factory.

    v1.3 §4.8 L7 — closes the user-perceived gap for voice-first-with-CLI
    sessions: users who never open the dashboard still see the warning
    on ``sovyx start`` / ``sovyx status`` because
    :func:`sovyx.voice.factory.create_voice_pipeline` wrote the same
    list the dashboard store holds to
    ``~/.sovyx/preflight_warnings.json``. This helper reads the
    marker (empty on missing/malformed) and prints one yellow line
    per warning plus the canonical remediation hint.

    Non-blocking: any IO hiccup is swallowed because a crashing voice
    surface must not crash ``sovyx start``.
    """
    try:
        from sovyx.voice.health import read_preflight_warnings_file
    except Exception:  # noqa: BLE001
        return
    try:
        warnings = read_preflight_warnings_file()
    except Exception:  # noqa: BLE001
        return
    if not warnings:
        return
    for w in warnings:
        code = w.get("code", "unknown")
        hint = w.get("hint", "")
        console.print(f"[yellow]⚠ Voice preflight warning:[/yellow] {code}")
        if hint:
            console.print(f"[dim]  {hint}[/dim]")
        console.print(
            "[dim]  Run: [bold]sovyx doctor voice --fix --yes[/bold] to remediate.[/dim]",
        )


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
) -> None:
    """Initialize Sovyx: create config files and data directory."""
    # ``--quick`` was removed 2026-05-02 (mission pre-wake-word T02).
    # The flag was declared but never honoured because ``init`` is
    # already non-interactive — there are no prompts to skip.

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
    # rc.6 (Agent 2 E.2): point operators at the calibration wizard for
    # silent-mic / hardware-pinned issues. Pre-rc.6 a fresh Sony VAIO +
    # PipeWire operator following `sovyx init` had no breadcrumb to
    # `sovyx doctor voice --calibrate`; they would hit silent-mic on
    # first run and have to dig through docs to find the wizard.
    console.print(
        "\n[dim]Voice not working as expected? Run "
        "[bold]sovyx doctor voice --calibrate[/bold] (Linux, 8-12 min) "
        "for an automatic mic + mixer + APO calibration.[/dim]"
    )


@app.command()
def start() -> None:  # pragma: no cover
    """Start the Sovyx daemon."""
    # ``--foreground`` was removed 2026-05-02 (mission pre-wake-word T02).
    # The flag was declared but never honoured — ``sovyx start`` already
    # blocks in ``run_forever`` and there is no daemonize/fork path that
    # the flag could disable. Operators wanting backgrounded execution
    # should use the OS service manager (systemd / launchd / Windows
    # Service) rather than a Sovyx-internal flag.
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
        # v1.3 §4.8 L7 — surface any prior-session preflight warnings
        # before handing off to ``run_forever``. A user booting into a
        # session where the mixer is still saturated should not be
        # silently blocked waiting for audio that will never reach the
        # VAD.
        _surface_preflight_warnings()
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

    # v1.3 §4.8 L7 — also surface preflight warnings here so a user
    # running ``sovyx status`` on an existing daemon sees the same
    # signal the dashboard displays without having to open it.
    _surface_preflight_warnings()


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


@mind_app.command("forget")
def mind_forget(
    mind_id: str = typer.Argument(..., help="Mind identifier to wipe"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be wiped without writing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt (scripted use).",
    ),
) -> None:
    """Right-to-erasure for a mind (GDPR Art. 17 / LGPD Art. 18 VI).

    Wipes every per-mind row across the brain DB (concepts, episodes,
    relations, embeddings, consolidation log, conversation_imports),
    the conversations DB (conversations + turns), the system DB
    (daily_stats), and the voice consent ledger. The mind's
    configuration is preserved — only its data is destroyed, so the
    operator can re-onboard the mind without re-creating it.

    The command requires the daemon to be running (the daemon owns
    the database pools). For scripted use, pass ``--yes`` to skip
    the confirmation prompt; pair with ``--dry-run`` to preview the
    counts before committing.

    Phase 8 / T8.21 step 4.
    """
    client = _get_client()
    if not client.is_daemon_running():
        console.print(
            "[red]Daemon not running — start with `sovyx start` first[/red]",
        )
        raise typer.Exit(1)

    if not mind_id.strip():
        console.print("[red]error:[/red] mind_id must be a non-empty string")
        raise typer.Exit(2)

    if not dry_run and not yes:
        confirm = typer.confirm(
            f"This will permanently wipe ALL data for mind={mind_id!r} "
            f"(brain + conversations + system + voice consent ledger). "
            f"The mind's configuration is preserved. Continue?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(1)

    try:
        result = _run(
            client.call(
                "mind.forget",
                {"mind_id": mind_id, "dry_run": dry_run},
            ),
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary — renders error and exits; pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None

    if not isinstance(result, dict):
        console.print(result)
        return

    verb = "would purge" if result.get("dry_run") else "purged"
    total = int(result.get("total_rows_purged", 0))
    consent = int(result.get("consent_ledger_purged", 0))
    console.print(
        f"[green]{verb}[/green] [bold]{total}[/bold] relational rows + "
        f"[bold]{consent}[/bold] consent-ledger records for mind={mind_id!r}",
    )
    # Per-table breakdown — only print non-zero rows so the output
    # stays useful when most tables are empty.
    breakdown_keys = (
        "concepts_purged",
        "relations_purged",
        "episodes_purged",
        "concept_embeddings_purged",
        "episode_embeddings_purged",
        "conversation_imports_purged",
        "consolidation_log_purged",
        "conversations_purged",
        "conversation_turns_purged",
        "daily_stats_purged",
        "consent_ledger_purged",
    )
    for key in breakdown_keys:
        count = int(result.get(key, 0))
        if count:
            console.print(f"  {key}: [cyan]{count}[/cyan]")


@retention_app.command("prune")
def mind_retention_prune(
    mind_id: str = typer.Argument(..., help="Mind identifier"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be pruned without writing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt (scripted use).",
    ),
) -> None:
    """Apply time-based retention policy to a mind.

    Prunes records older than per-surface horizons configured via
    ``EngineConfig.tuning.retention.*`` + ``MindConfig.retention.*``
    overrides. Distinguished from ``sovyx mind forget``: forget
    wipes EVERY record (right-to-erasure GDPR Art. 17 / LGPD Art.
    18 VI), retention prunes only OLD records (storage limitation
    GDPR Art. 5(1)(e) / LGPD Art. 16). Tombstone is RETENTION_PURGE
    not DELETE so external auditors can distinguish the two.

    Phase 8 / T8.21 step 6.
    """
    client = _get_client()
    if not client.is_daemon_running():
        console.print(
            "[red]Daemon not running — start with `sovyx start` first[/red]",
        )
        raise typer.Exit(1)

    if not mind_id.strip():
        console.print("[red]error:[/red] mind_id must be a non-empty string")
        raise typer.Exit(2)

    if not dry_run and not yes:
        confirm = typer.confirm(
            f"Apply retention policy to mind={mind_id!r}? "
            f"Old records (per configured horizons) will be permanently "
            f"deleted. Continue?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(1)

    try:
        result = _run(
            client.call(
                "mind.retention.prune",
                {"mind_id": mind_id, "dry_run": dry_run},
            ),
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary; pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None

    if not isinstance(result, dict):
        console.print(result)
        return

    verb = "would prune" if result.get("dry_run") else "pruned"
    total = int(result.get("total_rows_purged", 0))
    consent = int(result.get("consent_ledger_purged", 0))
    cutoff = result.get("cutoff_utc", "")
    console.print(
        f"[green]{verb}[/green] [bold]{total}[/bold] relational rows + "
        f"[bold]{consent}[/bold] consent-ledger records for mind={mind_id!r}",
    )
    if cutoff:
        console.print(f"  cutoff: [dim]{cutoff}[/dim]")
    breakdown_keys = (
        "episodes_purged",
        "conversations_purged",
        "conversation_turns_purged",
        "consolidation_log_purged",
        "daily_stats_purged",
        "consent_ledger_purged",
    )
    for key in breakdown_keys:
        count = int(result.get(key, 0))
        if count:
            console.print(f"  {key}: [cyan]{count}[/cyan]")
    horizons = result.get("effective_horizons", {})
    if isinstance(horizons, dict) and horizons:
        console.print("  [dim]Effective horizons (days):[/dim]")
        for surface, days in horizons.items():
            label = "[dim]disabled[/dim]" if days == 0 else f"{days}d"
            console.print(f"    {surface}: {label}")


@retention_app.command("status")
def mind_retention_status(
    mind_id: str = typer.Argument(..., help="Mind identifier"),
) -> None:
    """Preview retention horizons + counts WITHOUT writing.

    Equivalent to ``sovyx mind retention prune <mind_id> --dry-run --yes``
    but with a clearer name + no confirmation prompt (read-only).
    """
    client = _get_client()
    if not client.is_daemon_running():
        console.print("[red]Daemon not running[/red]")
        raise typer.Exit(1)

    try:
        result = _run(
            client.call(
                "mind.retention.prune",
                {"mind_id": mind_id, "dry_run": True},
            ),
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary; pragma: no cover
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None

    if not isinstance(result, dict):
        console.print(result)
        return

    total = int(result.get("total_rows_purged", 0))
    consent = int(result.get("consent_ledger_purged", 0))
    console.print(
        f"[bold]Retention status — mind={mind_id!r}[/bold]",
    )
    console.print(
        f"  Eligible to prune: [yellow]{total}[/yellow] relational + "
        f"[yellow]{consent}[/yellow] consent-ledger records",
    )
    horizons = result.get("effective_horizons", {})
    if isinstance(horizons, dict) and horizons:
        console.print("  Effective horizons (days):")
        for surface, days in horizons.items():
            label = "[dim]disabled[/dim]" if days == 0 else f"{days}d"
            console.print(f"    {surface}: {label}")
