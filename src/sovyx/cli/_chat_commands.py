"""Slash-command handlers for the ``sovyx chat`` REPL (SPE-015 §3.1).

Kept separate from ``cli/chat.py`` so the REPL loop stays focused on
input/output orchestration. Each handler is a coroutine that takes
the active :class:`DaemonClient` plus the parsed argv and returns a
``SlashResult`` carrying:

* ``rendered`` — Rich renderable to print (Table, Text, Panel)
* ``should_exit`` — True when the user typed ``/exit`` or ``/quit``
* ``new_conversation`` — True when ``/new`` was used (REPL must
  rotate ``conversation_id``)
* ``clear_screen`` — True when ``/clear`` was used (terminal wipe)

The dispatcher (``dispatch``) is the only public entry point; tests
invoke it directly with a mocked client and assert on the result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from sovyx.cli.rpc_client import DaemonClient


@dataclass
class SlashResult:
    """Outcome of one slash-command invocation."""

    rendered: Any = field(default_factory=lambda: Text(""))
    should_exit: bool = False
    new_conversation: bool = False
    clear_screen: bool = False


# ── /help ────────────────────────────────────────────────────────────


_HELP_ROWS: tuple[tuple[str, str], ...] = (
    ("/help", "Show this list of commands"),
    ("/status", "Daemon health, uptime, today's LLM cost"),
    ("/minds", "List active minds (which one is the default)"),
    ("/config", "Show the active mind's config (read-only)"),
    ("/new", "Start a fresh conversation (rotates conversation_id)"),
    ("/clear", "Clear the screen (and reset conversation_id)"),
    ("/exit, /quit", "Leave the REPL (Ctrl+D also works)"),
)


async def _cmd_help(_client: DaemonClient, _argv: list[str]) -> SlashResult:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description", style="dim")
    for cmd, desc in _HELP_ROWS:
        table.add_row(cmd, desc)
    panel = Panel(table, title="REPL commands", border_style="cyan")
    return SlashResult(rendered=panel)


# ── /exit, /quit ─────────────────────────────────────────────────────


async def _cmd_exit(_client: DaemonClient, _argv: list[str]) -> SlashResult:
    return SlashResult(
        rendered=Text("Goodbye.", style="dim"),
        should_exit=True,
    )


# ── /new ─────────────────────────────────────────────────────────────


async def _cmd_new(_client: DaemonClient, _argv: list[str]) -> SlashResult:
    return SlashResult(
        rendered=Text("New conversation started.", style="dim italic"),
        new_conversation=True,
    )


# ── /clear ───────────────────────────────────────────────────────────


async def _cmd_clear(_client: DaemonClient, _argv: list[str]) -> SlashResult:
    return SlashResult(
        rendered=Text("Cleared.", style="dim italic"),
        clear_screen=True,
        new_conversation=True,
    )


# ── /status ──────────────────────────────────────────────────────────


async def _cmd_status(client: DaemonClient, _argv: list[str]) -> SlashResult:
    """Daemon-reported status — version, uptime, today's cost."""
    raw = await client.call("status")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="green")
    for key in sorted(data):
        table.add_row(key, str(data[key]))
    if not data:
        table.add_row("status", "no fields returned")
    return SlashResult(rendered=Panel(table, title="Status", border_style="green"))


# ── /minds ───────────────────────────────────────────────────────────


async def _cmd_minds(client: DaemonClient, _argv: list[str]) -> SlashResult:
    raw = await client.call("mind.list")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    minds = data.get("minds") or []
    active = data.get("active")

    if not minds:
        return SlashResult(
            rendered=Text(
                "No active minds — has the engine finished bootstrapping?",
                style="yellow",
            )
        )

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Mind", style="bold cyan")
    table.add_column("Active", style="green", justify="center")
    for mind in minds:
        marker = "●" if mind == active else " "
        table.add_row(mind, marker)
    return SlashResult(rendered=Panel(table, title="Minds", border_style="cyan"))


# ── /config ──────────────────────────────────────────────────────────


async def _cmd_config(client: DaemonClient, _argv: list[str]) -> SlashResult:
    raw = await client.call("config.get")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}

    if not data.get("available"):
        return SlashResult(
            rendered=Text(
                "Config not available — PersonalityEngine missing from the registry.",
                style="yellow",
            )
        )

    top_table = Table(show_header=False, box=None, padding=(0, 2))
    top_table.add_column("Key", style="bold cyan")
    top_table.add_column("Value", style="white")
    for key in ("name", "language", "timezone", "template", "mind_id"):
        if key in data:
            top_table.add_row(key, str(data[key]))

    llm: dict[str, Any] = data.get("llm") or {}
    llm_table = Table(show_header=False, box=None, padding=(0, 2))
    llm_table.add_column("Key", style="bold cyan")
    llm_table.add_column("Value", style="white")
    for key in (
        "default_provider",
        "default_model",
        "fast_model",
        "temperature",
        "budget_daily_usd",
    ):
        if key in llm:
            llm_table.add_row(key, str(llm[key]))

    brain: dict[str, Any] = data.get("brain") or {}
    brain_table = Table(show_header=False, box=None, padding=(0, 2))
    brain_table.add_column("Key", style="bold cyan")
    brain_table.add_column("Value", style="white")
    for key in (
        "consolidation_interval_hours",
        "dream_time",
        "dream_lookback_hours",
        "dream_max_patterns",
        "max_concepts",
        "forgetting_enabled",
        "decay_rate",
    ):
        if key in brain:
            brain_table.add_row(key, str(brain[key]))

    grouped = Group(
        Panel(top_table, title="Mind", border_style="cyan"),
        Panel(llm_table, title="LLM", border_style="cyan"),
        Panel(brain_table, title="Brain", border_style="cyan"),
    )
    return SlashResult(rendered=grouped)


# ── Dispatch table ───────────────────────────────────────────────────


SlashHandler = Callable[["DaemonClient", list[str]], Awaitable[SlashResult]]

_HANDLERS: dict[str, SlashHandler] = {
    "/help": _cmd_help,
    "/?": _cmd_help,
    "/exit": _cmd_exit,
    "/quit": _cmd_exit,
    "/new": _cmd_new,
    "/clear": _cmd_clear,
    "/status": _cmd_status,
    "/minds": _cmd_minds,
    "/config": _cmd_config,
}


def known_commands() -> tuple[str, ...]:
    """Return every recognised slash command (canonical + aliases)."""
    return tuple(_HANDLERS.keys())


def parse(line: str) -> tuple[str, list[str]] | None:
    """Split a raw input line into ``(command, argv)``.

    Returns ``None`` for non-slash input (the caller treats that as a
    chat message). Empty argv is the common case — the MVP slash
    commands take no arguments.
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    return parts[0].lower(), parts[1:]


async def dispatch(
    client: DaemonClient,
    command: str,
    argv: list[str],
) -> SlashResult:
    """Route ``command`` to the registered handler.

    Unknown commands return a help-pointer rather than raising — slash
    typos are common and a friendly message keeps the REPL flowing.
    """
    handler = _HANDLERS.get(command)
    if handler is None:
        return SlashResult(
            rendered=Text(
                f"Unknown command: {command}. Type /help for the list.",
                style="yellow",
            ),
        )
    return await handler(client, argv)
