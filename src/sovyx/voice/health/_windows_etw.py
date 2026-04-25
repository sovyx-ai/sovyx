"""Windows audio ETW event-log probe (WI1).

The Windows audio stack writes structured events to the
``Microsoft-Windows-Audio/Operational`` event channel (and a handful
of sibling channels) the moment anything notable happens — a default
device change, a format-negotiation failure, a glitch report from
the audio engine, an endpoint volume change that disrupted a stream,
a driver crash. These events are the **gold-standard** signal Sovyx
has access to for "what went wrong with capture in the last hour" —
WASAPI / PortAudio surface only the symptom (timeout, all-zero frames),
while the operational channel carries the cause attribution.

Pre-WI1 the only Windows-side observability was the WI2 service
watchdog, which only sees current ``Audiosrv`` / ``AudioEndpointBuilder``
state. A user reporting "Sovyx went deaf for 30 s, then recovered" had
no signal at all. With WI1 we can pull the recent audio events and
correlate them with the deaf-window timestamp.

This module ships:

* :class:`EtwEventLevel` — closed-set vocabulary mapping the Windows
  Event Log level integers to readable tokens.
* :class:`EtwEvent` — one parsed event (timestamp + level + provider
  + event-id + message + raw text for forensic).
* :class:`EtwQueryResult` — per-channel query outcome carrying the
  events tuple + structured notes (probe-failure isolation).
* :func:`query_audio_etw_events` — top-level probe that fans out
  across the audio channels and returns one :class:`EtwQueryResult`
  per channel. Bounded subprocess calls; never raises.

Discovery method: ``wevtutil qe`` — built into every Windows since
Vista, no external dependency, runs without elevation against
operational channels. Bounded 5 s timeout per channel.

Design contract (mirrors WI2):

* Detection NEVER raises — subprocess / parse failures collapse
  into UNKNOWN with structured ``notes``.
* Always opt-in at the call site; this module exposes only pure
  functions, no global state, no thread.
* Per-event ``raw_text`` truncated to 4 KB to bound memory.

Reference: F1 inventory mission task WI1; Microsoft's
``Microsoft-Windows-Audio/Operational`` channel reference.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted Win event log binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds + tunables ─────────────────────────────────────────────


_WEVTUTIL_TIMEOUT_S = 5.0
"""Wall-clock budget per ``wevtutil qe`` call. Healthy event log
returns in <100 ms; 5 s absorbs a momentarily-slow disk read while
short enough that a wedged event log doesn't stall preflight or
the dashboard endpoint."""


_DEFAULT_LOOKBACK_S = 3600
"""Default lookback window for event queries — last hour. Long
enough to cover a typical "I just had a problem" report, short
enough to avoid wevtutil scanning hours of unrelated events."""


_DEFAULT_MAX_EVENTS_PER_CHANNEL = 50
"""Cap on events returned per channel. wevtutil in newest-first
mode (``/rd:true``) gives us the most-recent events first; 50
covers a chatty audio glitch storm without unbounded growth."""


_RAW_TEXT_TRUNCATE_BYTES = 4096
"""Per-event raw_text truncation. Operational audio events are
short (<2 KB typical); 4 KB is enough room for the long descriptions
without letting one pathological event balloon the report."""


# ── Public types ──────────────────────────────────────────────────


class EtwEventLevel(StrEnum):
    """Closed-set vocabulary for Windows Event Log levels.

    Maps the Windows ``Level`` integer codes to readable tokens.
    The wevtutil text format already emits the textual form, so we
    parse the token directly — these constants are the canonical
    set. UNKNOWN is the inconclusive bucket (parse failure, missing
    level field)."""

    CRITICAL = "critical"
    """Microsoft level 1 — driver crash, audio service hang."""

    ERROR = "error"
    """Microsoft level 2 — format negotiation failure, endpoint
    enumeration failure, capture stream broken."""

    WARNING = "warning"
    """Microsoft level 3 — glitch report, format conversion fallback,
    transient device unavailable."""

    INFO = "information"
    """Microsoft level 4 — default device change, stream open / close,
    endpoint volume change. Voluminous but useful for correlation."""

    VERBOSE = "verbose"
    """Microsoft level 5 — extremely chatty trace events. Excluded
    from the default audio query."""

    UNKNOWN = "unknown"
    """Probe parse failure or missing Level field."""


_LEVEL_TOKENS: dict[str, EtwEventLevel] = {
    "CRITICAL": EtwEventLevel.CRITICAL,
    "ERROR": EtwEventLevel.ERROR,
    "WARNING": EtwEventLevel.WARNING,
    "INFORMATION": EtwEventLevel.INFO,
    "INFORMATIONAL": EtwEventLevel.INFO,
    "INFO": EtwEventLevel.INFO,
    "VERBOSE": EtwEventLevel.VERBOSE,
}


@dataclass(frozen=True, slots=True)
class EtwEvent:
    """One parsed event from a Windows audio operational channel.

    The fields are the subset of the wevtutil text-format event
    record that's stable across Windows versions and useful for
    operator-facing diagnostics. ``raw_text`` carries the full
    event block (truncated) so the dashboard can render it for
    forensic deep-dives without a second wevtutil call."""

    channel: str
    """The event log channel the event was read from
    (e.g. ``"Microsoft-Windows-Audio/Operational"``)."""

    level: EtwEventLevel
    """Severity bucket — see :class:`EtwEventLevel`."""

    event_id: int
    """The Microsoft-published event ID. ``0`` if parsing failed.
    Stable across Windows versions for a given provider; operators
    can look these up in Microsoft docs."""

    timestamp_iso: str = ""
    """ISO 8601 timestamp from the ``Date:`` field, verbatim. Empty
    when parsing failed."""

    provider: str = ""
    """The provider name (e.g. ``"Microsoft-Windows-Audio"``)."""

    description: str = ""
    """Human-readable description, first 512 chars."""

    raw_text: str = ""
    """Verbatim event block from wevtutil, truncated to 4 KB."""


@dataclass(frozen=True, slots=True)
class EtwQueryResult:
    """Outcome of querying one event channel."""

    channel: str
    """The channel name (e.g. ``"Microsoft-Windows-Audio/Operational"``)."""

    events: tuple[EtwEvent, ...] = field(default_factory=tuple)
    """Newest-first list of parsed events. Empty when the probe
    failed OR the channel had no matching events in the lookback
    window — distinguish via ``notes``."""

    lookback_seconds: int = _DEFAULT_LOOKBACK_S
    """The lookback window applied to this query, for trace
    observability."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (subprocess errors, channel-not-
    found, parse fallbacks). Empty on a successful query that
    simply found no events."""


# ── Channel set ───────────────────────────────────────────────────


_AUDIO_CHANNELS: tuple[str, ...] = (
    "Microsoft-Windows-Audio/Operational",
    "Microsoft-Windows-Audio/PlaybackManager",
    "Microsoft-Windows-Audio/CaptureMonitor",
)
"""The audio operational channels Sovyx queries by default. The
``Operational`` channel is always present on modern Windows and
carries the bulk of capture-relevant events; the latter two are
optional (some Windows SKUs / versions don't ship them) — they
collapse to a NOT_FOUND note rather than failing the probe."""


# ── Probe ─────────────────────────────────────────────────────────


def query_audio_etw_events(
    *,
    lookback_seconds: int = _DEFAULT_LOOKBACK_S,
    max_events_per_channel: int = _DEFAULT_MAX_EVENTS_PER_CHANNEL,
    min_level: EtwEventLevel = EtwEventLevel.WARNING,
    channels: tuple[str, ...] | None = None,
) -> tuple[EtwQueryResult, ...]:
    """Query recent audio ETW events across the operational channels.

    Args:
        lookback_seconds: How far back to look. Defaults to 1 hour.
            Bounds: 60 to 86_400 (1 day). Out-of-range values are
            clamped at construction.
        max_events_per_channel: Cap on returned events per channel.
            Defaults to 50. Bounds: 1 to 500.
        min_level: Minimum severity to include. Defaults to WARNING
            (excludes the chatty INFO + VERBOSE buckets). Pass
            :attr:`EtwEventLevel.INFO` to include default-device-
            change events for correlation analyses.
        channels: Override the channel set. ``None`` defaults to
            :data:`_AUDIO_CHANNELS`.

    Returns:
        One :class:`EtwQueryResult` per requested channel. Never
        raises — subprocess / parse failures collapse into per-
        channel notes with empty events.
    """
    lookback_seconds = max(60, min(86_400, lookback_seconds))
    max_events_per_channel = max(1, min(500, max_events_per_channel))
    targets = channels if channels is not None else _AUDIO_CHANNELS

    if sys.platform != "win32":
        return tuple(
            EtwQueryResult(
                channel=ch,
                lookback_seconds=lookback_seconds,
                notes=(f"non-windows platform: {sys.platform}",),
            )
            for ch in targets
        )

    wevtutil_path = shutil.which("wevtutil.exe") or shutil.which("wevtutil")
    if wevtutil_path is None:
        return tuple(
            EtwQueryResult(
                channel=ch,
                lookback_seconds=lookback_seconds,
                notes=("wevtutil binary not found on PATH",),
            )
            for ch in targets
        )

    return tuple(
        _query_one_channel(
            wevtutil_path,
            ch,
            lookback_seconds=lookback_seconds,
            max_events=max_events_per_channel,
            min_level=min_level,
        )
        for ch in targets
    )


def _query_one_channel(
    wevtutil_path: str,
    channel: str,
    *,
    lookback_seconds: int,
    max_events: int,
    min_level: EtwEventLevel,
) -> EtwQueryResult:
    notes: list[str] = []
    # Build the XPath query: events newer than lookback, level <= threshold.
    # Microsoft levels: 1=Critical, 2=Error, 3=Warning, 4=Info, 5=Verbose.
    # Lower numeric level = higher severity; "<=" includes more severe.
    level_threshold = _level_to_microsoft_int(min_level)
    lookback_ms = lookback_seconds * 1000
    xpath = (
        f"*[System[(Level<={level_threshold}) and "
        f"TimeCreated[timediff(@SystemTime) <= {lookback_ms}]]]"
    )
    argv = (
        wevtutil_path,
        "qe",
        channel,
        f"/q:{xpath}",
        f"/c:{max_events}",
        "/rd:true",
        "/f:Text",
    )
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_WEVTUTIL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"wevtutil qe {channel} timed out")
        return EtwQueryResult(
            channel=channel,
            lookback_seconds=lookback_seconds,
            notes=tuple(notes),
        )
    except OSError as exc:
        notes.append(f"wevtutil spawn failed: {exc!r}")
        return EtwQueryResult(
            channel=channel,
            lookback_seconds=lookback_seconds,
            notes=tuple(notes),
        )

    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "channel" in stderr_lower and (
            "not found" in stderr_lower or "could not be found" in stderr_lower
        ):
            return EtwQueryResult(
                channel=channel,
                lookback_seconds=lookback_seconds,
                notes=("channel not present on this Windows SKU",),
            )
        notes.append(
            f"wevtutil exited {result.returncode}: {result.stderr.strip()[:200]}",
        )
        return EtwQueryResult(
            channel=channel,
            lookback_seconds=lookback_seconds,
            notes=tuple(notes),
        )

    events = _parse_wevtutil_text_output(result.stdout, channel=channel)
    return EtwQueryResult(
        channel=channel,
        events=events,
        lookback_seconds=lookback_seconds,
        notes=tuple(notes),
    )


def _level_to_microsoft_int(level: EtwEventLevel) -> int:
    """Map :class:`EtwEventLevel` back to the Microsoft Level integer.

    Used to build the XPath ``Level<=N`` filter. Lower number =
    more severe — the XPath ``<=`` includes the threshold AND
    everything more severe."""
    if level is EtwEventLevel.CRITICAL:
        return 1
    if level is EtwEventLevel.ERROR:
        return 2
    if level is EtwEventLevel.WARNING:
        return 3
    if level is EtwEventLevel.INFO:
        return 4
    if level is EtwEventLevel.VERBOSE:
        return 5
    # UNKNOWN — fall back to "everything except verbose".
    return 4


# ── Parser ────────────────────────────────────────────────────────


def _parse_wevtutil_text_output(
    stdout: str,
    *,
    channel: str,
) -> tuple[EtwEvent, ...]:
    """Parse the ``wevtutil qe /f:Text`` output into events.

    Output format (one event)::

        Event[0]:
          Log Name: Microsoft-Windows-Audio/Operational
          Source: Microsoft-Windows-Audio
          Date: 2026-04-25T12:34:56.789Z
          Event ID: 65
          Task: N/A
          Level: Warning
          Opcode: N/A
          Keyword: N/A
          User: S-1-5-18
          User Name: NT AUTHORITY\\SYSTEM
          Computer: HOSTNAME
          Description:
          <multi-line description text>

    Events are separated by ``Event[N]:`` headers. Description is
    everything after the ``Description:`` line until the next
    ``Event[N]:`` or end-of-input.
    """
    events: list[EtwEvent] = []
    blocks = _split_event_blocks(stdout)
    for block in blocks:
        ev = _parse_single_event_block(block, channel=channel)
        if ev is not None:
            events.append(ev)
    return tuple(events)


def _split_event_blocks(stdout: str) -> list[str]:
    """Split wevtutil text output into per-event blocks.

    Blocks start with ``Event[N]:`` (where N is a non-negative
    integer). The header line itself is dropped from the block —
    the body is everything until the next ``Event[`` or EOF."""
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for raw_line in stdout.splitlines():
        if raw_line.startswith("Event[") and "]:" in raw_line:
            if in_block and current:
                blocks.append("\n".join(current))
            current = []
            in_block = True
            continue
        if in_block:
            current.append(raw_line)
    if in_block and current:
        blocks.append("\n".join(current))
    return blocks


def _parse_single_event_block(block: str, *, channel: str) -> EtwEvent | None:
    """Parse one event block into :class:`EtwEvent`.

    Returns ``None`` if the block has no recognisable fields (e.g.
    truncated output). On partial parse, returns an event with
    UNKNOWN level + ``event_id=0`` rather than dropping it — the
    raw_text is still useful for forensic."""
    if not block.strip():
        return None
    fields: dict[str, str] = {}
    description_lines: list[str] = []
    in_description = False
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if in_description:
            description_lines.append(line)
            continue
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            in_description = True
            after = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if after:
                description_lines.append(after)
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip().lower()] = value.strip()

    event_id = _safe_int(fields.get("event id", "0"))
    level_token = fields.get("level", "").upper()
    level = _LEVEL_TOKENS.get(level_token, EtwEventLevel.UNKNOWN)
    timestamp = fields.get("date", "")
    provider = fields.get("source", "")
    description = " ".join(s for s in description_lines if s.strip())
    raw_text = block[:_RAW_TEXT_TRUNCATE_BYTES]

    return EtwEvent(
        channel=channel,
        level=level,
        event_id=event_id,
        timestamp_iso=timestamp,
        provider=provider,
        description=description[:512],
        raw_text=raw_text,
    )


def _safe_int(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


__all__ = [
    "EtwEvent",
    "EtwEventLevel",
    "EtwQueryResult",
    "query_audio_etw_events",
]
