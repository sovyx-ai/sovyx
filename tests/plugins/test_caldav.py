"""Tests for sovyx.plugins.official.caldav.

Same mock pattern as ``test_home_assistant.py`` (CLAUDE.md
anti-pattern #14): patch ``ca_mod.SandboxedHttpClient`` and stub
``request`` / ``close`` on the returned instance. The plugin uses
``client.request("PROPFIND", …)`` and ``client.request("REPORT", …)``
exclusively, so we don't bother mocking ``get`` / ``post``.

The XML / iCalendar fixtures below are the smallest valid bodies
the plugin actually parses. Adding more granular cases (timezones,
EXDATE, EXRULE) belongs in ``test_caldav_models.py`` against the
pure-Python ``expand_event`` function — keeping those out of the
HTTP path keeps the test surface focused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sovyx.plugins.official import caldav as ca_mod
from sovyx.plugins.official._caldav_models import CalendarEvent, CalendarSource
from sovyx.plugins.official.caldav import (
    CalDAVPlugin,
    _absolute_url,
    _calendar_query_body,
    _extract_calendar_data,
    _first_free_slot,
    _hostname_from_url,
    _parse_calendar_list,
)
from sovyx.plugins.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────


_PROPFIND_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
               xmlns:ic="http://apple.com/ns/ical/">
  <d:response>
    <d:href>/dav/calendars/user/me/Personal/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Personal</d:displayname>
        <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
        <ic:calendar-color>#3a87ad</ic:calendar-color>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/calendars/user/me/Work/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Work</d:displayname>
        <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/addressbooks/user/me/Contacts/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Contacts</d:displayname>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


def _ical_event(
    *,
    uid: str = "evt-1",
    summary: str = "Standup",
    dtstart: str = "20260420T140000Z",
    dtend: str = "20260420T143000Z",
    description: str = "",
    location: str = "",
    rrule: str | None = None,
) -> str:
    """Build a minimal valid VCALENDAR/VEVENT body."""
    extras = []
    if rrule:
        extras.append(f"RRULE:{rrule}")
    if description:
        extras.append(f"DESCRIPTION:{description}")
    if location:
        extras.append(f"LOCATION:{location}")
    extra_block = "\r\n".join(extras)
    block = "\r\n" + extra_block if extra_block else ""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Sovyx//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"DTSTAMP:{dtstart}{block}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _report_response(*ical_bodies: str) -> str:
    """Wrap one or more iCalendar bodies in a CalDAV REPORT multistatus."""
    responses = []
    for idx, body in enumerate(ical_bodies):
        responses.append(
            "  <d:response>\n"
            f"    <d:href>/dav/calendars/user/me/Personal/event-{idx}.ics</d:href>\n"
            "    <d:propstat>\n"
            "      <d:prop>\n"
            f'        <d:getetag>"etag-{idx}"</d:getetag>\n'
            f"        <c:calendar-data><![CDATA[{body}]]></c:calendar-data>\n"
            "      </d:prop>\n"
            "      <d:status>HTTP/1.1 200 OK</d:status>\n"
            "    </d:propstat>\n"
            "  </d:response>\n"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">\n'
        + "".join(responses)
        + "</d:multistatus>"
    )


def _http(status: int, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


def _patch_client(*, propfind_response: MagicMock, report_response: MagicMock | None = None):
    """Build a SandboxedHttpClient mock that returns canned PROPFIND / REPORT replies."""
    mock_client = MagicMock()

    async def fake_request(method: str, url: str, **_kw: Any) -> MagicMock:
        if method == "PROPFIND":
            return propfind_response
        if method == "REPORT":
            return report_response if report_response is not None else _http(207, "")
        return _http(404)

    mock_client.request = AsyncMock(side_effect=fake_request)
    mock_client.close = AsyncMock()
    return patch.object(ca_mod, "SandboxedHttpClient", return_value=mock_client), mock_client


async def _configured() -> CalDAVPlugin:
    plugin = CalDAVPlugin()
    ctx = MagicMock()
    ctx.config = {
        "base_url": "https://caldav.fastmail.com/dav/calendars/user/me/",
        "username": "me@example.com",
        "password": "test-password",
        "timezone": "UTC",
    }
    await plugin.setup(ctx)
    return plugin


# ── Plugin metadata + lifecycle ──────────────────────────────────────


class TestCalDAVPluginMeta:
    def test_name(self) -> None:
        assert CalDAVPlugin().name == "caldav"

    def test_version(self) -> None:
        assert CalDAVPlugin().version == "0.1.0"

    def test_description_mentions_caldav(self) -> None:
        assert "CalDAV" in CalDAVPlugin().description

    def test_permissions_include_network_internet(self) -> None:
        assert Permission.NETWORK_INTERNET in CalDAVPlugin().permissions

    async def test_setup_reads_full_config(self) -> None:
        plugin = CalDAVPlugin()
        ctx = MagicMock()
        ctx.config = {
            "base_url": "https://caldav.example.com/dav/me/",
            "username": "me@example.com",
            "password": "secret",
            "verify_ssl": False,
            "default_calendar": "Work",
            "allow_local": True,
            "timezone": "America/Sao_Paulo",
        }
        await plugin.setup(ctx)
        assert plugin._base_url == "https://caldav.example.com/dav/me/"  # noqa: SLF001
        assert plugin._username == "me@example.com"  # noqa: SLF001
        assert plugin._password == "secret"  # noqa: SLF001
        assert plugin._verify_ssl is False  # noqa: SLF001
        assert plugin._allow_local is True  # noqa: SLF001
        assert plugin._default_calendar == "Work"  # noqa: SLF001
        assert plugin._tz_name == "America/Sao_Paulo"  # noqa: SLF001

    async def test_setup_tolerates_non_dict_config(self) -> None:
        plugin = CalDAVPlugin()
        ctx = MagicMock()
        ctx.config = "not a dict"
        await plugin.setup(ctx)
        # Defaults preserved.
        assert plugin._base_url == ""  # noqa: SLF001


# ── Not-configured guard ─────────────────────────────────────────────


class TestNotConfiguredGuard:
    async def test_list_calendars_without_config(self) -> None:
        plugin = CalDAVPlugin()
        result = await plugin.list_calendars()
        assert "not configured" in result.lower()

    async def test_get_today_without_config(self) -> None:
        plugin = CalDAVPlugin()
        result = await plugin.get_today()
        assert "not configured" in result.lower()

    async def test_search_events_without_config(self) -> None:
        plugin = CalDAVPlugin()
        result = await plugin.search_events("standup")
        assert "not configured" in result.lower()


# ── list_calendars ──────────────────────────────────────────────────


class TestListCalendars:
    async def test_returns_calendars_excluding_addressbooks(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(propfind_response=_http(207, _PROPFIND_RESPONSE))
        with patcher:
            result = await plugin.list_calendars()
        assert "Personal" in result
        assert "Work" in result
        assert "Contacts" not in result  # addressbook filtered out
        assert "#3a87ad" in result  # color rendered

    async def test_propfind_failure_returns_empty_message(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(propfind_response=_http(401))
        with patcher:
            result = await plugin.list_calendars()
        assert "No calendars found" in result

    async def test_calendars_cached_within_ttl(self) -> None:
        plugin = await _configured()
        patcher, client = _patch_client(propfind_response=_http(207, _PROPFIND_RESPONSE))
        with patcher:
            await plugin.list_calendars()
            await plugin.list_calendars()
        # PROPFIND issued only once — cache hit on the second call.
        assert client.request.await_count == 1


# ── get_today + get_upcoming ────────────────────────────────────────


class TestGetToday:
    async def test_renders_events_grouped_by_day(self) -> None:
        plugin = await _configured()
        # Build an event that's definitely "today" wherever the test runs.
        now = datetime.now(UTC)
        ical = _ical_event(
            uid="standup-1",
            summary="Daily Standup",
            dtstart=now.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(now + timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ"),
        )
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            result = await plugin.get_today()
        assert "Daily Standup" in result
        assert "Today" in result

    async def test_no_events_today(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response()),
        )
        with patcher:
            result = await plugin.get_today()
        assert "No events scheduled for today" in result


class TestGetUpcoming:
    async def test_clamps_days_above_max(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response()),
        )
        with patcher:
            result = await plugin.get_upcoming(days=999)
        # Render uses the clamped value (30, not 999).
        assert "30 day(s)" in result

    async def test_returns_events_in_window(self) -> None:
        plugin = await _configured()
        future = datetime.now(UTC) + timedelta(days=2)
        ical = _ical_event(
            uid="meeting-2",
            summary="Project Review",
            dtstart=future.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(future + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
        )
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            result = await plugin.get_upcoming(days=7)
        assert "Project Review" in result

    async def test_filters_by_calendar(self) -> None:
        plugin = await _configured()
        future = datetime.now(UTC) + timedelta(days=1)
        ical = _ical_event(
            uid="work-evt",
            summary="Sprint Planning",
            dtstart=future.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(future + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
        )
        patcher, client = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            await plugin.get_upcoming(days=7, calendar="Work")
        # Two REPORT calls issued — wait, only ONE: only "Work" matches.
        report_calls = [c for c in client.request.await_args_list if c.args[0] == "REPORT"]
        assert len(report_calls) == 1
        # And the URL was the Work calendar.
        assert "Work" in report_calls[0].args[1]


# ── get_event ───────────────────────────────────────────────────────


class TestGetEvent:
    async def test_returns_detail_for_known_uid(self) -> None:
        plugin = await _configured()
        future = datetime.now(UTC) + timedelta(hours=2)
        ical = _ical_event(
            uid="meeting-x",
            summary="Architecture Review",
            dtstart=future.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(future + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
            location="Conference Room A",
            description="Discuss the new caching layer design.",
        )
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            result = await plugin.get_event("meeting-x")
        assert "Architecture Review" in result
        assert "Conference Room A" in result
        assert "caching layer" in result
        assert "UID: meeting-x" in result

    async def test_unknown_uid_returns_friendly_error(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response()),
        )
        with patcher:
            result = await plugin.get_event("ghost-uid")
        assert "Event not found" in result

    async def test_empty_uid_rejected(self) -> None:
        plugin = await _configured()
        result = await plugin.get_event("")
        assert "Invalid UID" in result


# ── find_free_slot ──────────────────────────────────────────────────


class TestFindFreeSlot:
    async def test_returns_first_gap(self) -> None:
        plugin = await _configured()
        # No events at all → entire window is free.
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response()),
        )
        with patcher:
            result = await plugin.find_free_slot(duration_minutes=30, days=1)
        assert "Free slot found" in result
        assert "30 min" in result

    async def test_no_slot_when_window_too_full(self) -> None:
        plugin = await _configured()
        # Build a wall-to-wall event covering the next 24h.
        start = datetime.now(UTC)
        end = start + timedelta(days=1, hours=1)
        ical = _ical_event(
            uid="all-day",
            summary="Hackathon",
            dtstart=start.strftime("%Y%m%dT%H%M%SZ"),
            dtend=end.strftime("%Y%m%dT%H%M%SZ"),
        )
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            result = await plugin.find_free_slot(duration_minutes=120, days=1)
        assert "No free" in result

    async def test_invalid_duration_rejected(self) -> None:
        plugin = await _configured()
        result = await plugin.find_free_slot(duration_minutes="forever", days=7)  # type: ignore[arg-type]
        assert "Invalid duration_minutes" in result


# ── search_events ───────────────────────────────────────────────────


class TestSearchEvents:
    async def test_matches_summary(self) -> None:
        plugin = await _configured()
        future = datetime.now(UTC) + timedelta(days=1)
        ical = _ical_event(
            uid="brainstorm-1",
            summary="Brainstorm Session",
            dtstart=future.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(future + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
        )
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response(ical)),
        )
        with patcher:
            result = await plugin.search_events("brainstorm")
        assert "Brainstorm Session" in result

    async def test_no_match_returns_empty_message(self) -> None:
        plugin = await _configured()
        patcher, _ = _patch_client(
            propfind_response=_http(207, _PROPFIND_RESPONSE),
            report_response=_http(207, _report_response()),
        )
        with patcher:
            result = await plugin.search_events("nonexistent-event")
        assert "No events matching" in result

    async def test_empty_query_rejected(self) -> None:
        plugin = await _configured()
        result = await plugin.search_events("   ")
        assert "Invalid search query" in result


# ── Tool surface ────────────────────────────────────────────────────


class TestToolSurface:
    """6 tools, all read-only — no requires_confirmation."""

    def test_tool_count(self) -> None:
        plugin = CalDAVPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 6  # noqa: PLR2004

    def test_tool_names_namespaced(self) -> None:
        plugin = CalDAVPlugin()
        names = {t.name for t in plugin.get_tools()}
        expected = {
            "caldav.list_calendars",
            "caldav.get_today",
            "caldav.get_upcoming",
            "caldav.get_event",
            "caldav.find_free_slot",
            "caldav.search_events",
        }
        assert names == expected

    def test_no_tool_requires_confirmation(self) -> None:
        plugin = CalDAVPlugin()
        confirm = [t for t in plugin.get_tools() if t.requires_confirmation]
        assert confirm == []


# ── Module-level helpers ────────────────────────────────────────────


class TestHelpers:
    def test_hostname_from_url(self) -> None:
        assert _hostname_from_url("https://caldav.fastmail.com/dav/me/") == "caldav.fastmail.com"
        assert _hostname_from_url("not a url at all") == ""

    def test_calendar_query_body_includes_window(self) -> None:
        start = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
        body = _calendar_query_body(start, end)
        assert 'start="20260420T000000Z"' in body
        assert 'end="20260427T000000Z"' in body

    def test_absolute_url_resolves_relative_href(self) -> None:
        base = "https://caldav.example.com/dav/me/"
        assert (
            _absolute_url(base, "/dav/me/Personal/")
            == "https://caldav.example.com/dav/me/Personal/"
        )

    def test_parse_calendar_list_filters_addressbooks(self) -> None:
        sources = _parse_calendar_list(
            _PROPFIND_RESPONSE,
            base_url="https://caldav.fastmail.com/dav/calendars/user/me/",
        )
        names = {s.name for s in sources}
        assert names == {"Personal", "Work"}

    def test_parse_calendar_list_handles_malformed(self) -> None:
        assert _parse_calendar_list("not xml at all", base_url="https://x/") == []

    def test_extract_calendar_data_handles_malformed(self) -> None:
        assert _extract_calendar_data("not xml") == []


# ── _first_free_slot pure logic ─────────────────────────────────────


class TestFirstFreeSlotLogic:
    def _ev(self, start: datetime, end: datetime) -> CalendarEvent:
        return CalendarEvent(uid="x", summary="x", start=start, end=end)

    def test_empty_calendar_returns_window_start(self) -> None:
        start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
        end = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)
        slot = _first_free_slot(
            [], window_start=start, window_end=end, duration=timedelta(minutes=30)
        )
        assert slot is not None
        assert slot[0] == start

    def test_finds_gap_between_meetings(self) -> None:
        start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
        end = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)
        events = [
            self._ev(start, start + timedelta(hours=1)),
            self._ev(start + timedelta(hours=2), start + timedelta(hours=3)),
        ]
        slot = _first_free_slot(
            events, window_start=start, window_end=end, duration=timedelta(minutes=30)
        )
        assert slot is not None
        # Gap is 10:00–11:00; we want the start of the gap.
        assert slot[0] == start + timedelta(hours=1)

    def test_no_gap_returns_none(self) -> None:
        start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
        end = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)
        events = [self._ev(start, end)]
        slot = _first_free_slot(
            events, window_start=start, window_end=end, duration=timedelta(minutes=30)
        )
        assert slot is None

    def test_all_day_events_dont_block(self) -> None:
        """All-day flagged events are excluded from blocking."""
        start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
        end = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)
        events = [
            CalendarEvent(uid="x", summary="x", start=start, end=end, all_day=True),
        ]
        slot = _first_free_slot(
            events, window_start=start, window_end=end, duration=timedelta(minutes=30)
        )
        assert slot is not None
        assert slot[0] == start


# ── Calendar source filtering ───────────────────────────────────────


class TestSelectCalendars:
    def test_no_filter_returns_all(self) -> None:
        plugin = CalDAVPlugin()
        sources = [
            CalendarSource(url="u1", name="Personal"),
            CalendarSource(url="u2", name="Work"),
        ]
        assert plugin._select_calendars(sources, name=None) == sources  # noqa: SLF001

    def test_default_calendar_used_when_no_name(self) -> None:
        plugin = CalDAVPlugin()
        plugin._default_calendar = "Work"  # noqa: SLF001
        sources = [
            CalendarSource(url="u1", name="Personal"),
            CalendarSource(url="u2", name="Work"),
        ]
        result = plugin._select_calendars(sources, name=None)  # noqa: SLF001
        assert len(result) == 1
        assert result[0].name == "Work"

    def test_explicit_name_overrides_default(self) -> None:
        plugin = CalDAVPlugin()
        plugin._default_calendar = "Personal"  # noqa: SLF001
        sources = [
            CalendarSource(url="u1", name="Personal"),
            CalendarSource(url="u2", name="Work"),
        ]
        result = plugin._select_calendars(sources, name="Work")  # noqa: SLF001
        assert len(result) == 1
        assert result[0].name == "Work"

    def test_unknown_name_returns_empty(self) -> None:
        plugin = CalDAVPlugin()
        sources = [CalendarSource(url="u", name="Personal")]
        assert plugin._select_calendars(sources, name="Ghost") == []  # noqa: SLF001
