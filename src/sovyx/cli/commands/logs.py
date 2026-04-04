"""sovyx logs — query and filter structured JSON logs.

Usage::

    sovyx logs                              # last 50 lines
    sovyx logs --level error                # only errors
    sovyx logs --filter module=brain        # filter by field
    sovyx logs --since 1h                   # last hour
    sovyx logs --follow                     # tail -f mode
    sovyx logs --json                       # raw JSON output
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import TextIO

import typer
from rich.console import Console
from rich.text import Text

console = Console()

logs_app = typer.Typer(name="logs", help="Query and filter structured logs")

# Default log file location
_DEFAULT_LOG_DIR = Path.home() / ".sovyx" / "logs"
_DEFAULT_LOG_FILE = _DEFAULT_LOG_DIR / "sovyx.log"

# ── Duration parser ─────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> timedelta:
    """Parse a duration string like '1h', '30m', '2d' into a timedelta.

    Raises:
        typer.BadParameter: If the format is invalid.
    """
    match = _DURATION_RE.match(value.strip())
    if not match:
        msg = f"Invalid duration '{value}'. Use format: 30s, 5m, 1h, 2d"
        raise typer.BadParameter(msg)
    amount = int(match.group(1))
    unit = match.group(2)
    return timedelta(seconds=amount * _DURATION_UNITS[unit])


# ── Filter parser ───────────────────────────────────────────────────────────


def _parse_filters(filters: list[str]) -> dict[str, str]:
    """Parse 'key=value' filter strings into a dict.

    Raises:
        typer.BadParameter: If format is invalid.
    """
    result: dict[str, str] = {}
    for f in filters:
        if "=" not in f:
            msg = f"Invalid filter '{f}'. Use format: key=value"
            raise typer.BadParameter(msg)
        key, value = f.split("=", 1)
        result[key.strip()] = value.strip()
    return result


# ── Log line matching ───────────────────────────────────────────────────────

_LEVEL_PRIORITY = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


def _matches(
    entry: dict[str, object],
    *,
    level_min: str | None,
    filters: dict[str, str],
    since: datetime | None,
) -> bool:
    """Check if a log entry matches all criteria."""
    # Level filter
    if level_min is not None:
        entry_level = str(entry.get("level", "info")).lower()
        if _LEVEL_PRIORITY.get(entry_level, 0) < _LEVEL_PRIORITY.get(level_min, 0):
            return False

    # Time filter
    if since is not None:
        ts = entry.get("timestamp", "")
        if isinstance(ts, str) and ts:
            try:
                entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if entry_time < since:
                    return False
            except ValueError:
                pass  # Can't parse → include it

    # Key=value filters
    for key, value in filters.items():
        entry_value = str(entry.get(key, ""))
        if value not in entry_value:
            return False

    return True


# ── Formatting ──────────────────────────────────────────────────────────────

_LEVEL_COLORS = {
    "debug": "dim",
    "info": "green",
    "warning": "yellow",
    "error": "red",
    "critical": "bold red",
}


def _format_entry(entry: dict[str, object]) -> Text:
    """Format a log entry for rich console output."""
    level = str(entry.get("level", "info")).lower()
    ts = str(entry.get("timestamp", ""))
    event = str(entry.get("event", ""))
    logger_name = str(entry.get("logger", ""))

    # Short timestamp (HH:MM:SS)
    short_ts = ts[11:19] if len(ts) >= 19 else ts

    color = _LEVEL_COLORS.get(level, "white")
    text = Text()
    text.append(f"{short_ts} ", style="dim")
    text.append(f"{level.upper():8s}", style=color)
    text.append(f" {logger_name}: " if logger_name else " ", style="dim cyan")
    text.append(event, style="bold")

    # Context fields (mind_id, conversation_id, request_id, etc.)
    skip = {"level", "timestamp", "event", "logger", "exc_info", "stack_info"}
    ctx_parts = []
    for key, value in entry.items():
        if key not in skip and value is not None:
            ctx_parts.append(f"{key}={value}")
    if ctx_parts:
        text.append("  ")
        text.append(" ".join(ctx_parts), style="dim")

    return text


# ── File reading ────────────────────────────────────────────────────────────


def _read_log_lines(
    log_file: Path,
    *,
    level: str | None,
    filters: dict[str, str],
    since: datetime | None,
    limit: int,
    raw_json: bool,
) -> int:
    """Read and display matching log lines. Returns count of lines shown."""
    if not log_file.exists():
        console.print(f"[yellow]Log file not found: {log_file}[/yellow]")
        console.print("[dim]Start the daemon with logging enabled to generate logs.[/dim]")
        return 0

    # Read all lines, filter, then take last `limit`
    matching: list[dict[str, object]] = []
    with open(log_file, encoding="utf-8") as f:
        for entry in _iter_new_lines(f):
            if _matches(entry, level_min=level, filters=filters, since=since):
                matching.append(entry)

    # Take last N
    to_show = matching[-limit:] if len(matching) > limit else matching

    for entry in to_show:
        if raw_json:
            console.print(json.dumps(entry))
        else:
            console.print(_format_entry(entry))

    return len(to_show)


def _iter_new_lines(
    file_handle: TextIO,
) -> Generator[dict[str, object], None, None]:
    """Yield parsed JSON log entries from a file handle.

    Reads available lines from the current position. Returns when
    no more lines are available (caller decides whether to retry).
    Skips blank lines and invalid JSON silently.
    """
    while True:
        line = file_handle.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _follow_log(  # pragma: no cover
    log_file: Path,
    *,
    level: str | None,
    filters: dict[str, str],
    raw_json: bool,
) -> None:
    """Tail -f mode: follow new log entries."""
    if not log_file.exists():
        console.print(f"[yellow]Waiting for log file: {log_file}[/yellow]")
        while not log_file.exists():
            time.sleep(0.5)

    with open(log_file, encoding="utf-8") as f:
        f.seek(0, 2)
        console.print("[dim]Following logs (Ctrl+C to stop)...[/dim]")

        try:
            while True:
                for entry in _iter_new_lines(f):
                    if _matches(entry, level_min=level, filters=filters, since=None):
                        if raw_json:
                            console.print(json.dumps(entry))
                        else:
                            console.print(_format_entry(entry))
                time.sleep(0.1)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped following.[/dim]")


# ── CLI command ─────────────────────────────────────────────────────────────


@logs_app.callback(invoke_without_command=True)
def logs(
    level: str | None = typer.Option(
        None,
        "--level",
        "-l",
        help="Minimum log level: debug, info, warning, error",
    ),
    filter_args: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--filter",
        "-f",
        help="Filter by field: key=value (repeatable)",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        "-s",
        help="Show logs since duration: 30s, 5m, 1h, 2d",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-F",
        help="Follow mode (tail -f)",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max lines to show",
    ),
    raw_json: bool = typer.Option(
        False,
        "--json",
        help="Output raw JSON lines",
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help="Path to log file (default: ~/.sovyx/logs/sovyx.log)",
    ),
) -> None:
    """Query and filter structured JSON logs.

    Examples::

        sovyx logs                           # last 50 lines
        sovyx logs --level error             # only errors
        sovyx logs -f module=brain           # filter by module
        sovyx logs --since 1h               # last hour
        sovyx logs --follow                  # tail -f
        sovyx logs --json                    # raw JSON
        sovyx logs -l warning -f mind_id=nyx --since 30m
    """
    target = log_file or _DEFAULT_LOG_FILE

    # Parse filters
    filters = _parse_filters(filter_args) if filter_args else {}

    # Parse level
    level_lower = level.lower() if level else None
    if level_lower and level_lower not in _LEVEL_PRIORITY:
        console.print(f"[red]Invalid level: {level}. Use: debug, info, warning, error[/red]")
        raise typer.Exit(1)

    # Parse since
    since_dt: datetime | None = None
    if since:
        delta = _parse_duration(since)
        since_dt = datetime.now(tz=UTC) - delta

    if follow:  # pragma: no cover
        _follow_log(
            target,
            level=level_lower,
            filters=filters,
            raw_json=raw_json,
        )
    else:
        count = _read_log_lines(
            target,
            level=level_lower,
            filters=filters,
            since=since_dt,
            limit=limit,
            raw_json=raw_json,
        )
        if count == 0 and target.exists():
            console.print("[dim]No matching log entries found.[/dim]")
