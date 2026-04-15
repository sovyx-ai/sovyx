"""Sovyx CalDAV Plugin — read calendar events over the CalDAV protocol.

Connects to the user's CalDAV server (Nextcloud, iCloud, Fastmail,
Radicale, SOGo, Baikal, …) and exposes 6 read-only LLM-callable
tools across the calendar surface:

* ``list_calendars``  — discover available calendars on the server
* ``get_today``        — events for today (recurring rules expanded)
* ``get_upcoming``     — next N days (default 7, max 30)
* ``get_event``        — full detail for one event by UID
* ``find_free_slot``   — first free window of a given duration
* ``search_events``    — fuzzy search by title / description

Why CalDAV directly (and not the ``caldav`` library)?
-----------------------------------------------------
The third-party ``caldav`` package routes its own HTTP traffic via
``requests`` — that bypasses :class:`SandboxedHttpClient` and turns
the plugin sandbox into theatre (CLAUDE.md anti-pattern #13). We
talk PROPFIND / REPORT XML directly through the sandbox, then hand
the iCalendar bodies to the lightweight :mod:`icalendar` parser.
``python-dateutil`` (a transitive dep of the project already) drives
the RRULE expansion in :mod:`_caldav_models`.

Permissions
-----------
``Permission.NETWORK_INTERNET`` — most CalDAV servers are SaaS
(``caldav.fastmail.com``, ``caldav.icloud.com``, hosted Nextcloud).
Self-hosted on the LAN is supported too via ``allow_local: true`` in
config; the sandbox still enforces the allowlist of one host (the
configured server) and the rate-limit + size cap.

Configuration
-------------
``mind.yaml`` under ``plugins_config.caldav``::

    plugins_config:
      caldav:
        base_url: "https://caldav.fastmail.com/dav/calendars/user/me@example.com/"
        username: "me@example.com"
        password: "<app-specific password>"
        verify_ssl: true              # optional, default true
        default_calendar: "Personal"  # optional
        allow_local: false            # optional, true for self-hosted on LAN

What this v0 does *not* do
--------------------------
- No write surface — events are read-only. No create / edit / delete.
- No incremental sync (no ctag / etag) — every refresh issues a full
  REPORT for the time window. CalDAV payloads are small so the cost
  is acceptable; ctag/etag is on the next-PR list.
- No subscribe / push notifications.
- Multi-account: only one calendar source per plugin instance in v0.
- **Google Calendar** discontinued CalDAV in 2023 — not supported.
  Use a self-hosted Nextcloud, iCloud, or Fastmail account instead.

Ref: IMPL-009-CALDAV (v0, scope-tightened from spec — read-only).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, ClassVar

from defusedxml import ElementTree as ET  # noqa: N817 — stdlib-style alias.

from sovyx.plugins.official._caldav_models import (
    CalendarEvent,
    CalendarSource,
    expand_event,
    resolve_tz,
)
from sovyx.plugins.permissions import Permission
from sovyx.plugins.sandbox_http import SandboxedHttpClient
from sovyx.plugins.sdk import ISovyxPlugin, tool

if TYPE_CHECKING:
    from collections.abc import Iterable

# ── Constants ───────────────────────────────────────────────────────

# Calendar payloads change less often than smart-home state; 5 min is
# a comfortable freshness window that still amortises a multi-tool
# ReAct cycle ("get_today, then find_free_slot, then search_events").
_EVENT_CACHE_TTL_S = 300.0

# Default upcoming window. Capped at 30 days because:
#  • RRULE expansion explodes super-linearly past that
#  • LLM context budgets break around 100+ events anyway
_DEFAULT_UPCOMING_DAYS = 7
_MAX_UPCOMING_DAYS = 30

# Search window for ``search_events`` — searching across more than a
# month is rarely useful and costs another full REPORT.
_SEARCH_WINDOW_DAYS = 30

# Minimum gap considered "free" by ``find_free_slot``. Avoids
# returning 1-second crannies between back-to-back meetings as real
# free time.
_MIN_FREE_SLOT_MINUTES = 5

# Per-request HTTP timeout. CalDAV servers can be slow on cold REPORT
# requests over WAN — 20 s leaves headroom without making the LLM
# wait forever when the server is genuinely down.
_HTTP_TIMEOUT_S = 20.0

# CalDAV / WebDAV / iCalendar XML namespaces. Used by both PROPFIND
# request bodies and PROPFIND response parsing.
_NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "ic": "http://apple.com/ns/ical/",
}

# PROPFIND body for ``list_calendars``. Asks each child collection
# for its display name, resourcetype (so we can filter for
# ``calendar`` collections), and Apple's calendar-color extension.
_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:ic="http://apple.com/ns/ical/">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <ic:calendar-color/>
  </d:prop>
</d:propfind>"""


def _calendar_query_body(start: datetime, end: datetime) -> str:
    """Build a CalDAV REPORT body for events in [start, end).

    The CalDAV ``calendar-query`` REPORT is the standard way to ask a
    server "give me every VEVENT whose recurrence intersects this
    window" without pulling the full collection. The ``time-range``
    filter shifts the recurrence-expansion cost to the server.
    """
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">\n'
        "  <d:prop>\n"
        "    <d:getetag/>\n"
        "    <c:calendar-data/>\n"
        "  </d:prop>\n"
        "  <c:filter>\n"
        '    <c:comp-filter name="VCALENDAR">\n'
        '      <c:comp-filter name="VEVENT">\n'
        f'        <c:time-range start="{start.astimezone(UTC).strftime(fmt)}" '
        f'end="{end.astimezone(UTC).strftime(fmt)}"/>\n'
        "      </c:comp-filter>\n"
        "    </c:comp-filter>\n"
        "  </c:filter>\n"
        "</c:calendar-query>\n"
    )


_DEFAULT_BASE_URL = "https://caldav.fastmail.com/dav/calendars/user/"


class CalDAVPlugin(ISovyxPlugin):
    """Read events from a CalDAV server.

    Stateless across cycles aside from a per-window event cache and
    a one-shot calendar discovery cache. Both invalidate on TTL.
    """

    config_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "description": (
                    "CalDAV base URL of the principal or calendar collection "
                    "(e.g. https://caldav.fastmail.com/dav/calendars/user/me@x/)."
                ),
            },
            "username": {"type": "string", "description": "Account username or email."},
            "password": {
                "type": "string",
                "description": "Password — use an app-specific password for iCloud/Fastmail.",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": True,
                "description": "Whether to verify TLS certificates. Default true.",
            },
            "default_calendar": {
                "type": "string",
                "description": "Display name of the calendar to use when no name is supplied.",
            },
            "allow_local": {
                "type": "boolean",
                "default": False,
                "description": "Allow LAN URLs (self-hosted Nextcloud / Radicale).",
            },
            "timezone": {
                "type": "string",
                "description": "Override timezone for floating events. Defaults to UTC.",
            },
        },
        "required": ["base_url", "username", "password"],
    }

    def __init__(self) -> None:
        # Config is filled by setup(ctx). Tools that fire before setup
        # — or with an empty config — return a friendly "not
        # configured" message instead of raising.
        self._base_url: str = ""
        self._username: str = ""
        self._password: str = ""
        self._verify_ssl: bool = True
        self._allow_local: bool = False
        self._default_calendar: str = ""
        self._tz_name: str = "UTC"
        self._calendars_cache: tuple[float, list[CalendarSource]] | None = None
        self._events_cache: dict[str, tuple[float, list[CalendarEvent]]] = {}

    # ── Lifecycle ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "caldav"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Read calendar events from any CalDAV server (Nextcloud, iCloud, Fastmail, …)."

    @property
    def permissions(self) -> list[Permission]:
        return [Permission.NETWORK_INTERNET]

    async def setup(self, ctx: object) -> None:
        cfg = getattr(ctx, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        base_url = cfg.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            # Trailing slash matters for PROPFIND href construction.
            self._base_url = base_url.rstrip("/") + "/"
        username = cfg.get("username")
        if isinstance(username, str):
            self._username = username.strip()
        password = cfg.get("password")
        if isinstance(password, str):
            self._password = password
        verify_ssl = cfg.get("verify_ssl", True)
        if isinstance(verify_ssl, bool):
            self._verify_ssl = verify_ssl
        allow_local = cfg.get("allow_local", False)
        if isinstance(allow_local, bool):
            self._allow_local = allow_local
        default_cal = cfg.get("default_calendar")
        if isinstance(default_cal, str):
            self._default_calendar = default_cal.strip()
        tz_name = cfg.get("timezone")
        if isinstance(tz_name, str) and tz_name.strip():
            self._tz_name = tz_name.strip()

    # ── Tools ─────────────────────────────────────────────────

    @tool(description="List the calendars available on the configured CalDAV server.")
    async def list_calendars(self) -> str:
        """Discover every calendar collection under the configured base URL."""
        if not self._configured():
            return _not_configured()
        sources = await self._fetch_calendars()
        if not sources:
            return "No calendars found at the configured CalDAV URL."
        lines = [f"Found {len(sources)} calendar(s):"]
        for src in sources:
            color = f" [{src.color}]" if src.color else ""
            lines.append(f"  • {src.name}{color}")
        return "\n".join(lines)

    @tool(description="List events scheduled for today (recurring rules expanded).")
    async def get_today(self, calendar: str | None = None) -> str:
        """All events overlapping today in the configured timezone."""
        if not self._configured():
            return _not_configured()
        tz = resolve_tz(self._tz_name)
        now_local = datetime.now(tz)
        start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)
        events = await self._fetch_events(start_of_day, end_of_day, calendar=calendar)
        if not events:
            return "No events scheduled for today."
        return _format_events_grouped_by_day(events, tz=tz, header="Today")

    @tool(
        description=(
            "List events for the next N days. Default 7, max 30. "
            "Pass calendar to scope to a single calendar by name."
        )
    )
    async def get_upcoming(
        self,
        days: int = _DEFAULT_UPCOMING_DAYS,
        calendar: str | None = None,
    ) -> str:
        """Events from now through ``days`` days from now."""
        if not self._configured():
            return _not_configured()
        days = max(1, min(_MAX_UPCOMING_DAYS, int(days)))
        tz = resolve_tz(self._tz_name)
        now_local = datetime.now(tz)
        end = now_local + timedelta(days=days)
        events = await self._fetch_events(now_local, end, calendar=calendar)
        if not events:
            return f"No events in the next {days} day(s)."
        return _format_events_grouped_by_day(events, tz=tz, header=f"Upcoming ({days} day(s))")

    @tool(description="Get full detail for a single event by its UID.")
    async def get_event(self, uid: str, calendar: str | None = None) -> str:
        """Return summary, time, location, attendees, description for one UID.

        Searches the next 30 days by default — events outside that
        window aren't visible to v0. Recurring instances all share
        the master VEVENT's UID, so we return the next occurrence on
        or after now.
        """
        if not self._configured():
            return _not_configured()
        if not uid or not isinstance(uid, str):
            return "Invalid UID."
        tz = resolve_tz(self._tz_name)
        now_local = datetime.now(tz)
        end = now_local + timedelta(days=_SEARCH_WINDOW_DAYS)
        events = await self._fetch_events(now_local, end, calendar=calendar)
        for event in events:
            if event.uid == uid:
                return _format_event_detail(event, tz=tz)
        return f"Event not found: {uid}"

    @tool(
        description=(
            "Find the first free slot of duration_minutes within the next ``days`` days. "
            "Default 7 days."
        )
    )
    async def find_free_slot(
        self,
        duration_minutes: int,
        days: int = _DEFAULT_UPCOMING_DAYS,
        calendar: str | None = None,
    ) -> str:
        """Walk the upcoming window and return the first gap big enough."""
        if not self._configured():
            return _not_configured()
        try:
            duration_minutes = max(_MIN_FREE_SLOT_MINUTES, int(duration_minutes))
        except (TypeError, ValueError):
            return f"Invalid duration_minutes: {duration_minutes!r}"
        days = max(1, min(_MAX_UPCOMING_DAYS, int(days)))
        tz = resolve_tz(self._tz_name)
        now_local = datetime.now(tz)
        end = now_local + timedelta(days=days)
        events = await self._fetch_events(now_local, end, calendar=calendar)

        slot = _first_free_slot(
            events,
            window_start=now_local,
            window_end=end,
            duration=timedelta(minutes=duration_minutes),
        )
        if slot is None:
            return f"No free {duration_minutes}-minute slot in the next {days} day(s)."
        slot_start, slot_end = slot
        return (
            f"Free slot found: {_format_dt(slot_start, tz=tz)} → "
            f"{_format_dt(slot_end, tz=tz)} ({duration_minutes} min)."
        )

    @tool(
        description=(
            "Search the next 30 days of events for a substring "
            "(matched against title, description, location)."
        )
    )
    async def search_events(self, query: str, calendar: str | None = None) -> str:
        """Case-insensitive substring search across the upcoming window."""
        if not self._configured():
            return _not_configured()
        if not query or not isinstance(query, str):
            return "Invalid search query."
        needle = query.strip().lower()
        if not needle:
            return "Invalid search query."

        tz = resolve_tz(self._tz_name)
        now_local = datetime.now(tz)
        end = now_local + timedelta(days=_SEARCH_WINDOW_DAYS)
        events = await self._fetch_events(now_local, end, calendar=calendar)
        matches = [
            e
            for e in events
            if needle in e.summary.lower()
            or needle in e.description.lower()
            or needle in e.location.lower()
        ]
        if not matches:
            return f"No events matching {query!r} in the next {_SEARCH_WINDOW_DAYS} day(s)."
        lines = [f"Found {len(matches)} event(s) matching {query!r}:"]
        for event in matches[:25]:  # cap output for LLM context
            lines.append(f"  • {_format_event_summary(event, tz=tz)}")
        if len(matches) > 25:  # noqa: PLR2004
            lines.append(f"  …and {len(matches) - 25} more (refine the query).")
        return "\n".join(lines)

    # ── HTTP helpers ──────────────────────────────────────────

    def _configured(self) -> bool:
        return bool(self._base_url and self._username and self._password)

    def _make_client(self) -> SandboxedHttpClient:
        host = _hostname_from_url(self._base_url)
        allowed = sorted({host}) if host else []
        return SandboxedHttpClient(
            plugin_name=self.name,
            allowed_domains=allowed,
            allow_local=self._allow_local,
            timeout_s=_HTTP_TIMEOUT_S,
        )

    def _auth(self) -> tuple[str, str]:
        return (self._username, self._password)

    async def _propfind(self, url: str, body: str, depth: str = "1") -> str | None:
        """Issue a PROPFIND request and return the response body or None on error."""
        return await self._dav_request("PROPFIND", url, body, depth=depth)

    async def _report(self, url: str, body: str) -> str | None:
        """Issue a REPORT request (calendar-query) and return the response body."""
        return await self._dav_request("REPORT", url, body, depth="1")

    async def _dav_request(
        self,
        method: str,
        url: str,
        body: str,
        *,
        depth: str = "1",
    ) -> str | None:
        """Shared transport for PROPFIND / REPORT — returns body or None.

        207 Multi-Status is the canonical CalDAV success code; some
        servers fall back to plain 200. Anything else (auth failure,
        not-found, server error) collapses to ``None`` and the caller
        renders a friendly fallback to the LLM.
        """
        client = self._make_client()
        try:
            resp = await client.request(
                method,
                url,
                content=body,
                headers={
                    "Content-Type": "application/xml; charset=utf-8",
                    "Depth": depth,
                },
                auth=self._auth(),
            )
        except Exception:  # noqa: BLE001 — plugin boundary; render & continue.
            return None
        finally:
            await client.close()

        if resp.status_code not in (207, 200):  # noqa: PLR2004
            return None
        return resp.text

    # ── Calendar discovery ────────────────────────────────────

    async def _fetch_calendars(self) -> list[CalendarSource]:
        if self._calendars_cache is not None:
            ts, cached = self._calendars_cache
            if (time.monotonic() - ts) < _EVENT_CACHE_TTL_S:
                return cached
        body = await self._propfind(self._base_url, _PROPFIND_CALENDARS)
        if body is None:
            return self._calendars_cache[1] if self._calendars_cache else []
        sources = _parse_calendar_list(body, base_url=self._base_url)
        self._calendars_cache = (time.monotonic(), sources)
        return sources

    # ── Event fetching + parsing ──────────────────────────────

    async def _fetch_events(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        calendar: str | None = None,
    ) -> list[CalendarEvent]:
        """Pull events from one or all calendars within the window."""
        sources = await self._fetch_calendars()
        if not sources:
            return []

        target = self._select_calendars(sources, name=calendar)
        if not target:
            return []

        cache_key = self._cache_key(window_start, window_end, target)
        cached = self._events_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < _EVENT_CACHE_TTL_S:
            return cached[1]

        body = _calendar_query_body(window_start, window_end)
        tz = resolve_tz(self._tz_name)
        all_events: list[CalendarEvent] = []
        for source in target:
            xml = await self._report(source.url, body)
            if xml is None:
                continue
            ical_bodies = _extract_calendar_data(xml)
            for ical in ical_bodies:
                events = _parse_ical_events(
                    ical,
                    window_start=window_start,
                    window_end=window_end,
                    tz=tz,
                    calendar_name=source.name,
                )
                all_events.extend(events)

        all_events.sort(key=lambda e: e.start)
        self._events_cache[cache_key] = (time.monotonic(), all_events)
        return all_events

    def _select_calendars(
        self,
        sources: list[CalendarSource],
        *,
        name: str | None,
    ) -> list[CalendarSource]:
        target_name = name or self._default_calendar
        if not target_name:
            return sources  # all calendars
        wanted = target_name.lower()
        matches = [s for s in sources if s.name.lower() == wanted]
        return matches

    @staticmethod
    def _cache_key(start: datetime, end: datetime, sources: list[CalendarSource]) -> str:
        names = "|".join(s.name for s in sources)
        return f"{start.isoformat()}::{end.isoformat()}::{names}"


# ── Module-level helpers ────────────────────────────────────────────


def _hostname_from_url(url: str) -> str:
    """Extract the hostname for the SandboxedHttpClient allowlist."""
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        host = None
    return host or ""


def _parse_calendar_list(xml_body: str, *, base_url: str) -> list[CalendarSource]:
    """Parse a PROPFIND multi-status response into CalendarSource objects.

    Filters to ``<resourcetype>`` elements containing ``<C:calendar/>``
    so we don't surface address-books or principal collections.
    """
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return []
    sources: list[CalendarSource] = []
    for response in root.findall("d:response", _NS):
        resourcetype = response.find(".//d:resourcetype", _NS)
        if resourcetype is None or resourcetype.find("c:calendar", _NS) is None:
            continue
        href_el = response.find("d:href", _NS)
        if href_el is None or not href_el.text:
            continue
        href = href_el.text.strip()
        full_url = _absolute_url(base_url, href)
        if full_url == base_url:
            continue
        name_el = response.find(".//d:displayname", _NS)
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not name:
            name = href.rstrip("/").rsplit("/", 1)[-1] or href
        color_el = response.find(".//ic:calendar-color", _NS)
        color = (color_el.text or "").strip() if color_el is not None else None
        sources.append(CalendarSource(url=full_url, name=name, color=color or None))
    return sources


def _extract_calendar_data(xml_body: str) -> list[str]:
    """Pull every ``<C:calendar-data>`` body out of a REPORT response."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return []
    return [
        el.text for el in root.findall(".//c:calendar-data", _NS) if el is not None and el.text
    ]


def _parse_ical_events(
    ical_body: str,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    calendar_name: str,
) -> list[CalendarEvent]:
    """Hand off iCalendar parsing to :mod:`icalendar`, then expand."""
    try:
        from icalendar import Calendar  # noqa: PLC0415
    except ImportError:
        return []
    try:
        cal = Calendar.from_ical(ical_body)
    except (ValueError, IndexError, KeyError):
        return []
    out: list[CalendarEvent] = []
    for component in cal.walk("VEVENT"):
        out.extend(
            expand_event(
                component,
                window_start=window_start,
                window_end=window_end,
                tz=tz,
                calendar_name=calendar_name,
            )
        )
    return out


def _absolute_url(base_url: str, href: str) -> str:
    """Resolve a server-returned href against the configured base URL."""
    from urllib.parse import urljoin  # noqa: PLC0415

    return urljoin(base_url, href)


# ── Free-slot search ───────────────────────────────────────────────


def _first_free_slot(
    events: Iterable[CalendarEvent],
    *,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
) -> tuple[datetime, datetime] | None:
    """Linear scan for the first ``duration``-long gap inside the window."""
    blocking = sorted((e for e in events if not e.all_day), key=lambda e: e.start)
    cursor = window_start
    for event in blocking:
        if event.end <= cursor:
            continue
        if event.start - cursor >= duration:
            return (cursor, cursor + duration)
        cursor = max(cursor, event.end)
        if cursor >= window_end:
            break
    if window_end - cursor >= duration:
        return (cursor, cursor + duration)
    return None


# ── Renderers ──────────────────────────────────────────────────────


def _format_dt(dt: datetime, *, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _format_date(dt: datetime, *, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _format_event_summary(event: CalendarEvent, *, tz: tzinfo) -> str:
    if event.all_day:
        when = f"all day on {_format_date(event.start, tz=tz)}"
    else:
        when = f"{_format_dt(event.start, tz=tz)} – {_format_dt(event.end, tz=tz)}"
    location = f" @ {event.location}" if event.location else ""
    cal = f" [{event.calendar_name}]" if event.calendar_name else ""
    return f"{event.summary}{cal} — {when}{location}"


def _format_events_grouped_by_day(
    events: list[CalendarEvent],
    *,
    tz: tzinfo,
    header: str,
) -> str:
    by_day: dict[str, list[CalendarEvent]] = {}
    for event in events:
        day = _format_date(event.start, tz=tz)
        by_day.setdefault(day, []).append(event)

    lines = [f"{header} — {len(events)} event(s):"]
    for day in sorted(by_day.keys()):
        lines.append(f"\n[{day}]")
        for event in by_day[day]:
            time_part = (
                "(all day)"
                if event.all_day
                else f"{event.start.astimezone(tz).strftime('%H:%M')}–"
                f"{event.end.astimezone(tz).strftime('%H:%M')}"
            )
            cal = f" [{event.calendar_name}]" if event.calendar_name else ""
            location = f" @ {event.location}" if event.location else ""
            lines.append(f"  {time_part}  {event.summary}{cal}{location}")
    return "\n".join(lines)


def _format_event_detail(event: CalendarEvent, *, tz: tzinfo) -> str:
    when = (
        f"all day on {_format_date(event.start, tz=tz)}"
        if event.all_day
        else f"{_format_dt(event.start, tz=tz)} – {_format_dt(event.end, tz=tz)}"
    )
    lines = [f"Event: {event.summary}", f"  When: {when}"]
    if event.location:
        lines.append(f"  Location: {event.location}")
    if event.calendar_name:
        lines.append(f"  Calendar: {event.calendar_name}")
    if event.organizer:
        lines.append(f"  Organizer: {event.organizer}")
    if event.attendees:
        lines.append(f"  Attendees: {', '.join(event.attendees)}")
    if event.description:
        lines.append(f"  Description: {event.description.strip()[:500]}")
    if event.uid:
        lines.append(f"  UID: {event.uid}")
    return "\n".join(lines)


def _not_configured() -> str:
    return (
        "CalDAV plugin is not configured. Set ``base_url``, ``username`` and "
        "``password`` under ``plugins_config.caldav`` in mind.yaml."
    )
