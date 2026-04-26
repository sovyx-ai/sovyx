"""MA14 — macOS recent audio events probe (sysdiagnose subset).

Mission §3.14 / MA14: Apple's ``log show`` CLI exposes the unified
logging system, which captures every CoreAudio + AVFoundation
operational event. This module ships a bounded query — ``log show
--predicate 'subsystem == "com.apple.audio"' --last 5m`` — to
surface recent audio activity for forensic correlation when a user
reports silent capture.

This is the macOS analogue of WI1 (Windows ETW probe). The naming
echoes Apple's ``sysdiagnose`` collection tool, but we deliberately
do NOT invoke ``sysdiagnose`` itself (which produces a multi-GB
archive and requires admin privileges) — only the lightweight
``log show`` query.

Public API:

* :func:`query_audio_log_events` — synchronous bounded query.
* :class:`MacosLogEvent` — one parsed event line.
* :class:`MacosLogQueryResult` — events + diagnostic notes.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.d.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_LOOKBACK = "5m"
"""``log show`` accepts ISO-8601 duration-like strings. 5 minutes is
the sweet spot: enough to capture an entire recent capture session
incl. setup events, short enough to keep the subprocess fast (~1-2s
on a typical Mac)."""

_LOG_SHOW_TIMEOUT_S = 8.0
"""``log show`` cold-start can hit 5 s when reading from cold storage;
8 s gives ~60% headroom."""

_DEFAULT_MAX_EVENTS = 100
"""Cap on returned events. Matches the WI1 ETW probe's per-channel
cap. Higher caps risk dashboard render issues + push the parse cost
into noticeable territory."""

_AUDIO_SUBSYSTEM_PREDICATE = 'subsystem == "com.apple.audio"'
"""The unified-logging predicate that filters to CoreAudio +
AVFoundation events. Other audio-relevant subsystems
(``com.apple.coreaudio``, ``com.apple.audiotoolbox``) are emitted
under ``com.apple.audio`` in modern macOS so the single predicate
covers the boot path."""


class MacosLogEventLevel(StrEnum):
    """Coarse severity from ``log show`` output.

    Apple's unified logging defines DEFAULT / INFO / DEBUG / ERROR /
    FAULT. We collapse to the StrEnum vocabulary used elsewhere in
    the voice stack.
    """

    INFO = "info"
    """Default + Info events — operational telemetry."""

    DEBUG = "debug"
    """Verbose / debug events. Usually suppressed unless the
    ``log config`` overrides have been changed."""

    WARNING = "warning"
    """Error events — surfaced as WARNING in dashboards."""

    FAULT = "fault"
    """Fault events — daemon-level errors. The most severe class
    in the unified logging vocabulary."""


@dataclass(frozen=True, slots=True)
class MacosLogEvent:
    """One parsed ``log show`` event line."""

    timestamp_iso: str
    """Original timestamp from ``log show`` output. ``log show``
    emits ``YYYY-MM-DD HH:MM:SS.NNNNNN-HHMM`` format."""

    level: MacosLogEventLevel
    """Coarse severity."""

    subsystem: str
    """The unified-logging subsystem (e.g.
    ``com.apple.audio.AudioHardwareService``)."""

    process: str
    """Emitting process name (e.g. ``coreaudiod``,
    ``AudioComponentRegistrar``)."""

    description: str
    """The event description — first 256 characters."""

    raw_text: str
    """Full raw line — first 1 KB for forensic inspection."""


@dataclass(frozen=True, slots=True)
class MacosLogQueryResult:
    """Outcome of :func:`query_audio_log_events`."""

    events: tuple[MacosLogEvent, ...] = field(default_factory=tuple)
    """Newest-first list of parsed events. Empty when the probe
    failed OR no events fell into the lookback window."""

    lookback: str = _DEFAULT_LOOKBACK
    """The lookback duration applied (for trace observability)."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes."""


def _level_for_token(token: str) -> MacosLogEventLevel:
    """Map a ``log show`` level token to our enum.

    Apple's ``log show`` emits ``Default``, ``Info``, ``Debug``,
    ``Error``, ``Fault`` as the level word in its default human
    format. Unknown tokens collapse to INFO (safe default — the
    dashboard renders them as benign rather than fault).
    """
    upper = token.strip().upper()
    if upper in {"FAULT"}:
        return MacosLogEventLevel.FAULT
    if upper in {"ERROR"}:
        return MacosLogEventLevel.WARNING
    if upper in {"DEBUG"}:
        return MacosLogEventLevel.DEBUG
    return MacosLogEventLevel.INFO


def _parse_log_line(line: str) -> MacosLogEvent | None:
    """Best-effort parse of one ``log show`` line.

    Apple's default ``log show`` output format is::

        2026-04-25 12:34:56.123456-0300  Default  0x1234   coreaudiod: (HALPlugIn) ...

    We split on whitespace conservatively. Lines that don't match the
    expected shape return ``None`` (the caller skips them).

    Preamble lines (``Filtering...``, ``Skipping...``, ``Timestamp``,
    column dividers ``=====``) do NOT start with an ISO-8601-ish date
    so the date-prefix gate filters them out cleanly.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return None
    # Date-prefix gate: a real event line starts with YYYY-MM-DD.
    if len(line) < 10 or line[4] != "-" or line[7] != "-":
        return None
    if not (line[:4].isdigit() and line[5:7].isdigit() and line[8:10].isdigit()):
        return None
    parts = line.split(None, 5)
    # parts: [date, time, level, thread_id, process, rest]
    if len(parts) < 6:
        return None
    timestamp = f"{parts[0]} {parts[1]}"
    level = _level_for_token(parts[2])
    process_with_colon = parts[4]
    process = process_with_colon.rstrip(":")
    rest = parts[5]
    # rest may start with "(subsystem) message" or just "message".
    subsystem = ""
    description = rest
    if rest.startswith("("):
        end = rest.find(")")
        if end > 0:
            subsystem = rest[1:end]
            description = rest[end + 1 :].strip()
    return MacosLogEvent(
        timestamp_iso=timestamp,
        level=level,
        subsystem=subsystem,
        process=process,
        description=description[:256],
        raw_text=line[:1024],
    )


def query_audio_log_events(
    *,
    lookback: str = _DEFAULT_LOOKBACK,
    max_events: int = _DEFAULT_MAX_EVENTS,
) -> MacosLogQueryResult:
    """Synchronous bounded query of recent audio-subsystem events.

    Args:
        lookback: ISO-8601-ish duration string (e.g. ``"5m"``,
            ``"1h"``). Forwarded as the ``--last`` argument to
            ``log show``. Defaults to 5 minutes.
        max_events: Cap on returned events. Defaults to 100.
            Bounds: 1 to 500.

    Returns:
        :class:`MacosLogQueryResult`. Never raises — non-darwin /
        binary missing / subprocess error / parse fallback all
        collapse into empty events with notes.
    """
    max_events = max(1, min(500, max_events))

    if sys.platform != "darwin":
        return MacosLogQueryResult(
            lookback=lookback,
            notes=(f"non-darwin platform: {sys.platform}",),
        )

    log_path = shutil.which("log")
    if log_path is None:
        return MacosLogQueryResult(
            lookback=lookback,
            notes=("log binary not found on PATH",),
        )

    try:
        result = subprocess.run(  # noqa: S603 — log_path from shutil.which, args fixed
            (
                log_path,
                "show",
                "--predicate",
                _AUDIO_SUBSYSTEM_PREDICATE,
                "--last",
                lookback,
                "--style",
                "compact",
            ),
            capture_output=True,
            text=True,
            timeout=_LOG_SHOW_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return MacosLogQueryResult(
            lookback=lookback,
            notes=(f"log show timed out after {_LOG_SHOW_TIMEOUT_S}s",),
        )
    except OSError as exc:
        return MacosLogQueryResult(
            lookback=lookback,
            notes=(f"log show spawn failed: {exc!r}",),
        )

    if result.returncode != 0:
        return MacosLogQueryResult(
            lookback=lookback,
            notes=(f"log show exited {result.returncode}: {result.stderr.strip()[:120]}",),
        )

    parsed: list[MacosLogEvent] = []
    notes: list[str] = []
    skipped_lines = 0
    for line in result.stdout.splitlines():
        if not line.strip() or line.startswith("="):
            continue
        event = _parse_log_line(line)
        if event is None:
            skipped_lines += 1
            continue
        parsed.append(event)
        if len(parsed) >= max_events:
            break
    if skipped_lines > 0:
        notes.append(f"skipped {skipped_lines} unparseable lines")

    # Reverse so newest-first matches the WI1 contract.
    parsed.reverse()
    return MacosLogQueryResult(
        events=tuple(parsed),
        lookback=lookback,
        notes=tuple(notes),
    )


__all__ = [
    "MacosLogEvent",
    "MacosLogEventLevel",
    "MacosLogQueryResult",
    "query_audio_log_events",
]
