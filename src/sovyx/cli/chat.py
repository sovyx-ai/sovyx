"""Interactive chat REPL — ``sovyx chat`` (SPE-015 §3.1).

A line-oriented conversation with the active mind, with persistent
history at ``~/.sovyx/history`` and slash commands handled inline.

Transport
---------
The REPL talks to the daemon via JSON-RPC over the existing Unix
socket (``~/.sovyx/sovyx.sock``), not via HTTP. That keeps the
chat available even when the dashboard FastAPI is disabled and
avoids serializing through a second JSON layer. File-permission
auth (socket is ``0o600``) is enough — we never put the REPL behind
a network listener.

Slash commands
--------------
Anything starting with ``/`` is parsed by ``cli/_chat_commands.py``.
Everything else is treated as user input and forwarded to the
``chat`` RPC method, which calls ``handle_chat_message`` (the same
entry point the dashboard ``POST /api/chat`` uses).

Conversation state
------------------
The REPL keeps a single ``conversation_id`` for the session. The
``/new`` and ``/clear`` slash commands rotate it so the user can
start a fresh conversation without restarting the process. The
backend ``ConversationTracker`` does the persistence; the REPL is
purely client state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from sovyx.cli import _chat_commands
from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine.errors import ChannelConnectionError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from rich.console import RenderableType


class _PromptSession(Protocol):
    """Subset of ``prompt_toolkit.PromptSession`` we depend on.

    Avoids importing prompt_toolkit at module load (it's heavy) and
    lets tests pass a tiny fake without subclassing the real type.
    """

    async def prompt_async(self, prompt: str = ..., /) -> str: ...


logger = get_logger(__name__)

DEFAULT_HISTORY_PATH = Path.home() / ".sovyx" / "history"

# Slash-command response timeout. Status / config calls hit local
# in-memory state and return in <100 ms; 5 s is a generous ceiling
# that flags a deadlocked daemon without making the user wait.
_SLASH_TIMEOUT_S = 5.0

# Chat turn timeout. Mirrors the dashboard's 30 s ceiling on
# ``handle_chat_message`` — long enough for an LLM call and a
# multi-tool ReAct loop, short enough that a stuck cycle surfaces
# as a real error.
_CHAT_TIMEOUT_S = 30.0


def run_repl(
    *,
    socket_path: Path | None = None,
    history_path: Path | None = None,
    console: Console | None = None,
) -> int:
    """Run the REPL until the user exits.

    Returns the process exit code: ``0`` on clean exit, ``1`` when
    the daemon is unreachable or an unrecoverable error occurs.
    Every keyword arg has a sensible default; tests inject a mock
    console + tmp history path.
    """
    cli = console or Console()
    client = DaemonClient(socket_path=socket_path)

    if not client.is_daemon_running():
        cli.print(
            "[red]Sovyx daemon is not running.[/red]\n"
            "[dim]Start it first:  [bold]sovyx start[/bold][/dim]"
        )
        return 1

    history_file = history_path or DEFAULT_HISTORY_PATH
    history_file.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 — owner-only, matches the daemon socket.
    if not history_file.exists():
        history_file.touch(mode=0o600)

    session = _build_session(history_file)

    cli.print(
        Panel(
            Text.from_markup(
                "Type a message to chat. Slash commands like "
                "[bold cyan]/help[/bold cyan], [bold cyan]/status[/bold cyan], "
                "[bold cyan]/minds[/bold cyan], [bold cyan]/config[/bold cyan] "
                "are available.\nPress [bold]Ctrl+D[/bold] or type "
                "[bold cyan]/exit[/bold cyan] to leave."
            ),
            title="Sovyx REPL",
            border_style="cyan",
        )
    )

    state = _ReplState()

    try:
        asyncio.run(_loop(cli, client, session, state))
    except KeyboardInterrupt:
        cli.print("\n[dim]Interrupted.[/dim]")
        return 0
    return 0


# ── Internals ────────────────────────────────────────────────────────


class _ReplState:
    """Mutable per-session state — just the conversation_id for MVP."""

    __slots__ = ("conversation_id",)

    def __init__(self) -> None:
        self.conversation_id: str | None = None


async def _loop(
    cli: Console,
    client: DaemonClient,
    session: _PromptSession,
    state: _ReplState,
) -> None:
    """Read → dispatch → render until the user exits."""
    while True:
        try:
            line = await _prompt(session)
        except (EOFError, KeyboardInterrupt):
            cli.print("\n[dim]Goodbye.[/dim]")
            return

        if not line.strip():
            continue

        parsed = _chat_commands.parse(line)
        if parsed is not None:
            command, argv = parsed
            if not await _handle_slash(cli, client, command, argv, state):
                return
            continue

        await _handle_chat(cli, client, line, state)


async def _handle_slash(
    cli: Console,
    client: DaemonClient,
    command: str,
    argv: list[str],
    state: _ReplState,
) -> bool:
    """Run one slash command. Returns ``False`` to break the REPL loop."""
    try:
        result = await asyncio.wait_for(
            _chat_commands.dispatch(client, command, argv),
            timeout=_SLASH_TIMEOUT_S,
        )
    except TimeoutError:
        cli.print(f"[red]{command} timed out after {_SLASH_TIMEOUT_S:.0f}s.[/red]")
        return True
    except ChannelConnectionError as exc:
        cli.print(f"[red]Daemon error: {exc}[/red]")
        return True
    except Exception as exc:  # noqa: BLE001 — REPL boundary; log + render + keep going.
        logger.warning("repl_slash_failed", command=command, exc_info=True)
        cli.print(f"[red]{command} failed: {exc}[/red]")
        return True

    _print_renderable(cli, result.rendered)

    if result.clear_screen:
        cli.clear()
    if result.new_conversation:
        state.conversation_id = None
    return not result.should_exit


async def _handle_chat(
    cli: Console,
    client: DaemonClient,
    message: str,
    state: _ReplState,
) -> None:
    """Send one chat turn and render the assistant's reply."""
    params: dict[str, Any] = {"message": message}
    if state.conversation_id is not None:
        params["conversation_id"] = state.conversation_id

    try:
        raw = await asyncio.wait_for(
            client.call("chat", params, timeout=_CHAT_TIMEOUT_S),
            timeout=_CHAT_TIMEOUT_S,
        )
    except TimeoutError:
        cli.print(
            f"[red]No response after {_CHAT_TIMEOUT_S:.0f}s — the cognitive loop "
            "is stuck or the LLM is unreachable.[/red]"
        )
        return
    except ChannelConnectionError as exc:
        cli.print(f"[red]Daemon error: {exc}[/red]")
        return
    except Exception as exc:  # noqa: BLE001 — REPL boundary; one bad turn must
        # not crash the whole session. Log so we can investigate, render a
        # short error to the user, and wait for the next prompt.
        logger.warning("repl_chat_failed", exc_info=True)
        cli.print(f"[red]Chat failed: {exc}[/red]")
        return

    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if "conversation_id" in data:
        state.conversation_id = str(data["conversation_id"])

    response_text = str(data.get("response") or "")
    if not response_text.strip():
        cli.print("[dim](empty response)[/dim]")
        return

    tags = data.get("tags") or []
    tag_suffix = ""
    if isinstance(tags, list) and tags:
        tag_suffix = "  " + " ".join(f"[dim]·{t}[/dim]" for t in tags)

    cli.print(f"[bold magenta]assistant[/bold magenta]{tag_suffix}")
    cli.print(response_text)
    cli.print()


def _print_renderable(cli: Console, renderable: RenderableType) -> None:
    """Print a Rich renderable; falls back to ``str`` for primitives."""
    cli.print(renderable)
    cli.print()


def _build_session(history_file: Path) -> _PromptSession:
    """Build a ``prompt_toolkit`` session with history + auto-complete.

    Imported lazily so the rest of the CLI doesn't pay the
    ``prompt_toolkit`` startup cost on every invocation. Tests pass
    a mock ``session`` and skip this entirely.
    """
    from prompt_toolkit import PromptSession  # noqa: PLC0415
    from prompt_toolkit.completion import WordCompleter  # noqa: PLC0415
    from prompt_toolkit.history import FileHistory  # noqa: PLC0415

    completer = WordCompleter(
        list(_chat_commands.known_commands()),
        ignore_case=True,
    )
    return PromptSession(
        history=FileHistory(str(history_file)),
        completer=completer,
        enable_history_search=True,
    )


async def _prompt(session: _PromptSession) -> str:
    """Single-line prompt — async so the REPL loop can await it."""
    return await session.prompt_async("> ")
