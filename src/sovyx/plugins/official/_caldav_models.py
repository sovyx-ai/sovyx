"""Lightweight typed views over CalDAV / iCalendar payloads.

CalDAV servers return XML for collection metadata (PROPFIND) and a
mix of XML+iCalendar (RFC 5545) for events (REPORT). The
:mod:`icalendar` library handles VEVENT / RRULE parsing; this
module provides the dataclasses + helpers that turn that raw output
into the strongly-typed objects the plugin actually surfaces to the
LLM.

Why a separate module?

* Mirrors :mod:`_ha_models` next to ``home_assistant.py`` — keeps
  parsing logic out of the orchestration layer so the plugin file
  reads as "fetch → render", not "fetch → parse → render".
* Recurrence expansion is non-trivial and benefits from focused
  testing (RRULE edge cases, EXDATE, DST transitions). Exposing
  :func:`expand_event` as a module-level function lets the test
  suite exercise it without a full plugin instance.

Time semantics
--------------
Every datetime that crosses the boundary is timezone-aware. ``DATE``
(all-day) values from iCalendar are represented as ``datetime`` at
midnight of the user's mind timezone — that lets the renderers
treat all events uniformly when sorting, while a separate
``all_day`` flag tells the formatter to drop the time component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# RRULE expansion is capped to keep pathological "RRULE without UNTIL"
# events (which technically expand to infinity) from blowing out
# memory or LLM context. 200 instances is generous: a daily event for
# half a year, or a weekly event for ~4 years.
_MAX_RECURRENCE_INSTANCES = 200


@dataclass(frozen=True)
class CalendarSource:
    """One calendar collection discovered on the CalDAV server."""

    url: str  # absolute href as returned by PROPFIND
    name: str  # display name (CALDAV:displayname or fallback)
    color: str | None = None  # CSS-style color if the server provided one


@dataclass(frozen=True)
class CalendarEvent:
    """A single occurrence of a calendar event.

    Recurring VEVENTs are expanded into one ``CalendarEvent`` per
    instance during the parsing pass — so callers iterate over a
    flat list rather than reasoning about RRULEs.
    """

    uid: str
    summary: str
    start: datetime  # always tz-aware
    end: datetime  # always tz-aware; for DATE values, end-exclusive midnight
    all_day: bool = False
    location: str = ""
    description: str = ""
    organizer: str = ""
    attendees: tuple[str, ...] = field(default_factory=tuple)
    calendar_name: str = ""  # which CalendarSource this came from


# ── Timezone helpers ────────────────────────────────────────────────


def resolve_tz(name: str) -> tzinfo:
    """Best-effort tzinfo resolution.

    Falls back to UTC for unknown zones — same posture as the DREAM
    scheduler's tz parser. Plugins should never crash on a malformed
    config value when the right answer is "use UTC and warn".
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC


def to_aware(value: object, tz: tzinfo) -> datetime | None:
    """Coerce iCalendar's date/datetime mix into a tz-aware datetime.

    ``icalendar`` returns:

    * :class:`datetime.date` for DATE (all-day) values
    * naive :class:`datetime.datetime` for floating times
    * tz-aware :class:`datetime.datetime` for UTC and zoned times

    Anything else (None, strings) returns ``None`` so the caller can
    skip the event with a debug log instead of raising.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=tz)
    return None


# ── Event expansion ────────────────────────────────────────────────


def expand_event(
    component: Any,  # noqa: ANN401 — icalendar Component has no public stubs.
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    calendar_name: str = "",
) -> list[CalendarEvent]:
    """Return every occurrence of ``component`` inside ``window_*``.

    Handles three cases:

    1. Single (non-recurring) event — emitted once if it overlaps.
    2. Recurring event with RRULE — expanded via ``component.walk``
       and the iCalendar tools-of-fortune approach: we read DTSTART /
       DTEND once, then iterate the RRULE manually with
       ``dateutil.rrule`` so we stay within the cap.
    3. Component without a usable DTSTART — skipped.

    Capped at ``_MAX_RECURRENCE_INSTANCES`` to bound memory + token
    spend on prompts. Daily-for-eternity events with no UNTIL still
    behave gracefully: we return the first 200 instances inside the
    window and stop.
    """
    if str(getattr(component, "name", "")).upper() != "VEVENT":
        return []

    raw_dtstart = _get_value(component, "DTSTART")
    raw_dtend = _get_value(component, "DTEND")
    if raw_dtstart is None:
        return []

    all_day = isinstance(raw_dtstart, date) and not isinstance(raw_dtstart, datetime)
    start = to_aware(raw_dtstart, tz)
    if start is None:
        return []

    if raw_dtend is not None:
        end = to_aware(raw_dtend, tz)
    else:
        # iCalendar default: DURATION property or +1 day for DATE / 0 min for DATE-TIME.
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    if end is None:
        end = start

    base = _build_event(
        component,
        start=start,
        end=end,
        all_day=all_day,
        calendar_name=calendar_name,
    )

    rrule_prop = component.get("RRULE")
    if rrule_prop is None:
        if _overlaps(base.start, base.end, window_start, window_end):
            return [base]
        return []

    return _expand_rrule(
        component=component,
        base=base,
        rrule_prop=rrule_prop,
        window_start=window_start,
        window_end=window_end,
    )


def _expand_rrule(
    *,
    component: Any,  # noqa: ANN401 — icalendar Component, no stubs.
    base: CalendarEvent,
    rrule_prop: Any,  # noqa: ANN401 — icalendar vRecur, no stubs.
    window_start: datetime,
    window_end: datetime,
) -> list[CalendarEvent]:
    """Iterate an RRULE producing CalendarEvent occurrences within the window."""
    try:
        from dateutil.rrule import rrulestr  # noqa: PLC0415
    except ImportError:
        # python-dateutil is a transitive dep of many things, but if it
        # isn't installed we degrade to "first occurrence only" rather
        # than crashing the entire plugin.
        return [base] if _overlaps(base.start, base.end, window_start, window_end) else []

    duration = base.end - base.start
    rule_str = _rrule_to_string(rrule_prop)
    try:
        rule = rrulestr(rule_str, dtstart=base.start)
    except (ValueError, TypeError):
        return [base] if _overlaps(base.start, base.end, window_start, window_end) else []

    exdates = _exdates(component)

    occurrences: list[CalendarEvent] = []
    for occurrence_start in rule:
        if occurrence_start > window_end:
            break
        if occurrence_start in exdates:
            continue
        occurrence_end = occurrence_start + duration
        if not _overlaps(occurrence_start, occurrence_end, window_start, window_end):
            continue
        occurrences.append(
            CalendarEvent(
                uid=base.uid,
                summary=base.summary,
                start=occurrence_start,
                end=occurrence_end,
                all_day=base.all_day,
                location=base.location,
                description=base.description,
                organizer=base.organizer,
                attendees=base.attendees,
                calendar_name=base.calendar_name,
            )
        )
        if len(occurrences) >= _MAX_RECURRENCE_INSTANCES:
            break
    return occurrences


# ── Component → CalendarEvent ──────────────────────────────────────


def _build_event(
    component: Any,  # noqa: ANN401 — icalendar Component, no stubs.
    *,
    start: datetime,
    end: datetime,
    all_day: bool,
    calendar_name: str,
) -> CalendarEvent:
    return CalendarEvent(
        uid=str(component.get("UID") or ""),
        summary=str(component.get("SUMMARY") or "(no title)"),
        start=start,
        end=end,
        all_day=all_day,
        location=str(component.get("LOCATION") or ""),
        description=str(component.get("DESCRIPTION") or ""),
        organizer=_extract_organizer(component),
        attendees=_extract_attendees(component),
        calendar_name=calendar_name,
    )


def _extract_organizer(component: Any) -> str:  # noqa: ANN401
    raw = component.get("ORGANIZER")
    if raw is None:
        return ""
    return str(raw).removeprefix("mailto:").strip()


def _extract_attendees(component: Any) -> tuple[str, ...]:  # noqa: ANN401
    raw = component.get("ATTENDEE")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raw = [raw]
    return tuple(str(a).removeprefix("mailto:").strip() for a in raw)


def _exdates(component: Any) -> set[datetime]:  # noqa: ANN401
    """Collect EXDATE entries as a set of tz-aware datetimes for fast lookup."""
    raw = component.get("EXDATE")
    if raw is None:
        return set()
    items = raw if isinstance(raw, list) else [raw]
    out: set[datetime] = set()
    for entry in items:
        for dt in getattr(entry, "dts", []):
            value = getattr(dt, "dt", None)
            if isinstance(value, datetime):
                out.add(value if value.tzinfo else value.replace(tzinfo=UTC))
            elif isinstance(value, date):
                out.add(datetime(value.year, value.month, value.day, tzinfo=UTC))
    return out


def _rrule_to_string(rrule_prop: Any) -> str:  # noqa: ANN401
    """``icalendar`` RRULE objects expose ``.to_ical()`` returning bytes."""
    raw = rrule_prop.to_ical() if hasattr(rrule_prop, "to_ical") else str(rrule_prop)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not raw.upper().startswith("RRULE:"):
        raw = f"RRULE:{raw}"
    return raw


def _get_value(component: Any, key: str) -> Any:  # noqa: ANN401
    """Walk through icalendar's vDDDTypes wrapper to the raw ``.dt`` value."""
    prop = component.get(key)
    if prop is None:
        return None
    return getattr(prop, "dt", prop)


def _overlaps(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> bool:
    """Half-open interval overlap test [a_start, a_end) ∩ [b_start, b_end)."""
    return a_start < b_end and b_start < a_end
