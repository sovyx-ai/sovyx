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

Discovery method: ``wevtutil qe … /f:XML`` — built into every Windows
since Vista, no external dependency, runs without elevation against
operational channels. Bounded 5 s timeout per channel.

The XML render mode is deliberate (WINDOWS-2 audit fix): the legacy
``/f:Text`` mode is locale/format-fragile — the ``Level:`` VALUE is
localized (pt-BR emits ``Informações`` / ``Aviso``), and the
``Event[N]`` block header is emitted WITHOUT the trailing colon on
real Windows 11 hosts, so a colon-requiring splitter parsed ZERO
events (silently blind probe). ``/f:XML`` carries the numeric
``<Level>`` element plus ``<TimeCreated>`` / ``<Provider>`` /
``<EventID>``, all fully locale-neutral. wevtutil emits the events as
a CONCATENATED sequence of ``<Event>`` roots (no enclosing document
element); the parser wraps them in a synthetic root before handing
the string to :mod:`xml.etree.ElementTree`. Channel absence is
detected via the ``ERROR_EVT_CHANNEL_NOT_FOUND`` return code (15007 /
``0x3A9F``), never via localized stderr text.

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
from typing import TYPE_CHECKING

from defusedxml import ElementTree as ET  # noqa: N817 — stdlib-style alias.

if TYPE_CHECKING:
    from xml.etree.ElementTree import (
        Element,  # nosec B405 — typing-only; parsing goes through defusedxml
    )

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
    The wevtutil XML render carries the NUMERIC ``<Level>`` element
    (locale-neutral — the textual form is localized, e.g. pt-BR
    ``Informações``), so parsing maps the integer via
    :data:`_MICROSOFT_LEVEL_TO_LEVEL`. UNKNOWN is the inconclusive
    bucket (parse failure, missing / out-of-range level element)."""

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


_MICROSOFT_LEVEL_TO_LEVEL: dict[int, EtwEventLevel] = {
    # Microsoft numeric levels per winmeta.xml. Level 0 is LogAlways
    # ("always logged", no severity attached) — bucketed as INFO so a
    # provider that emits it doesn't surface as UNKNOWN severity.
    0: EtwEventLevel.INFO,
    1: EtwEventLevel.CRITICAL,
    2: EtwEventLevel.ERROR,
    3: EtwEventLevel.WARNING,
    4: EtwEventLevel.INFO,
    5: EtwEventLevel.VERBOSE,
}
"""Numeric ``<Level>`` → :class:`EtwEventLevel`. Locale-neutral by
construction — replaces the pre-WINDOWS-2 English-token table that
mapped every localized level value to UNKNOWN."""


@dataclass(frozen=True, slots=True)
class EtwEvent:
    """One parsed event from a Windows audio operational channel.

    The fields are the subset of the wevtutil XML event record
    (``<System>`` children + ``<EventData>``) that's stable across
    Windows versions and useful for operator-facing diagnostics.
    ``raw_text`` carries the full event XML (truncated) so the
    dashboard can render it for forensic deep-dives without a
    second wevtutil call."""

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
    """ISO 8601 timestamp from ``<TimeCreated SystemTime='…'/>``,
    verbatim. Empty when parsing failed."""

    provider: str = ""
    """The provider name from ``<Provider Name='…'/>``
    (e.g. ``"Microsoft-Windows-Audio"``)."""

    description: str = ""
    """``Name=value`` pairs synthesized from the event's
    ``<EventData>`` children, first 512 chars. The XML render carries
    the raw event payload rather than a (localized) rendered message —
    the payload values are what triage actually greps for (device
    names, endpoint IDs, state codes)."""

    raw_text: str = ""
    """Verbatim per-event XML from wevtutil, truncated to 4 KB."""


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
        # XML render — locale-neutral numeric <Level> + attribute-form
        # provider/timestamp. NEVER /f:Text: its Level VALUE is
        # localized and its Event[N] header format drifts across
        # Windows builds (see module docstring, WINDOWS-2).
        "/f:XML",
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
        # Locale-neutral channel-absence check: wevtutil exits with
        # ERROR_EVT_CHANNEL_NOT_FOUND (15007 / 0x3A9F; empirically
        # rc=15007 on Windows 11) when the channel doesn't exist on
        # this SKU. The pre-WINDOWS-2 code grepped stderr for the
        # ENGLISH "not found" phrase — pt-BR emits "Não foi possível
        # encontrar o canal especificado.", misclassifying a merely-
        # absent channel as a probe failure.
        if _is_channel_not_found_returncode(result.returncode):
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

    events = _parse_wevtutil_xml_output(result.stdout, channel=channel, notes=notes)
    return EtwQueryResult(
        channel=channel,
        events=events,
        lookback_seconds=lookback_seconds,
        notes=tuple(notes),
    )


_CHANNEL_NOT_FOUND_WIN32 = 15007
"""``ERROR_EVT_CHANNEL_NOT_FOUND`` (winerror.h, ``0x3A9F``) — the exit
code wevtutil returns for a channel that doesn't exist on this
Windows SKU. Empirically verified rc=15007 on this Windows 11 host."""

_CHANNEL_NOT_FOUND_HRESULT = 0x80073A9F
"""HRESULT-wrapped form of :data:`_CHANNEL_NOT_FOUND_WIN32`
(``HRESULT_FROM_WIN32(15007)``) — accepted defensively in case a
Windows build surfaces the HRESULT instead of the bare Win32 code."""


def _is_channel_not_found_returncode(returncode: int) -> bool:
    """Return ``True`` when ``returncode`` signals channel-not-found.

    Masks to unsigned 32-bit first so an HRESULT surfaced as a
    negative CPython exit code still matches."""
    rc = returncode & 0xFFFFFFFF
    return rc in (_CHANNEL_NOT_FOUND_WIN32, _CHANNEL_NOT_FOUND_HRESULT)


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


def _local_tag(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _parse_wevtutil_xml_output(
    stdout: str,
    *,
    channel: str,
    notes: list[str],
) -> tuple[EtwEvent, ...]:
    """Parse ``wevtutil qe … /f:XML`` output into events.

    Real wevtutil output (empirically captured on Windows 11) is a
    CONCATENATED sequence of ``<Event xmlns='…'>…</Event>`` roots with
    no enclosing document element and no separators::

        <Event xmlns='…'><System><Provider Name='Microsoft-Windows-Audio' …/>
        <EventID>65</EventID>…<Level>4</Level>…
        <TimeCreated SystemTime='2026-02-22T15:46:34.3677596Z'/>…</System>
        <EventData><Data Name='DeviceName'>…</Data>…</EventData></Event><Event …

    The parser wraps the sequence in a synthetic root so ElementTree
    accepts it, then extracts the locale-neutral fields per event.
    A malformed payload appends a structured note (probe-failure
    isolation — NEVER silently blind) and returns no events. Empty
    stdout is a healthy quiet channel: no events, no notes.
    """
    if not stdout.strip():
        return ()
    try:
        root = ET.fromstring(f"<SovyxEvents>{stdout}</SovyxEvents>")
    except ET.ParseError as exc:
        notes.append(f"wevtutil XML parse failed: {exc}")
        return ()
    events: list[EtwEvent] = []
    for element in root:
        if _local_tag(element.tag) != "Event":
            continue
        events.append(_parse_single_event_element(element, channel=channel))
    return tuple(events)


def _parse_single_event_element(
    element: Element,
    *,
    channel: str,
) -> EtwEvent:
    """Parse one ``<Event>`` element into :class:`EtwEvent`.

    Total: a partial / malformed element still yields an event with
    UNKNOWN level + ``event_id=0`` rather than being dropped — the
    ``raw_text`` XML is still useful for forensic deep-dives."""
    level = EtwEventLevel.UNKNOWN
    event_id = 0
    timestamp = ""
    provider = ""
    data_parts: list[str] = []

    for section in element:
        section_tag = _local_tag(section.tag)
        if section_tag == "System":
            for child in section:
                child_tag = _local_tag(child.tag)
                if child_tag == "Level":
                    # Distinguish "element absent / garbage" (UNKNOWN)
                    # from a genuine numeric 0 (LogAlways → INFO) —
                    # _safe_int's 0-fallback would conflate the two.
                    level_int = _safe_int_or_none(child.text or "")
                    if level_int is not None:
                        level = _MICROSOFT_LEVEL_TO_LEVEL.get(
                            level_int,
                            EtwEventLevel.UNKNOWN,
                        )
                elif child_tag == "EventID":
                    event_id = _safe_int(child.text or "")
                elif child_tag == "TimeCreated":
                    timestamp = child.get("SystemTime", "")
                elif child_tag == "Provider":
                    provider = child.get("Name", "")
        elif section_tag == "EventData":
            for data in section:
                if _local_tag(data.tag) != "Data":
                    continue
                value = (data.text or "").strip()
                name = data.get("Name", "")
                data_parts.append(f"{name}={value}" if name else value)

    raw_text = ET.tostring(element, encoding="unicode")[:_RAW_TEXT_TRUNCATE_BYTES]
    description = " ".join(part for part in data_parts if part)

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


def _safe_int_or_none(value: str) -> int | None:
    """Like :func:`_safe_int` but with a ``None`` (not ``0``) fallback —
    for fields where ``0`` is a legitimate distinct value."""
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


__all__ = [
    "EtwEvent",
    "EtwEventLevel",
    "EtwQueryResult",
    "query_audio_etw_events",
]
