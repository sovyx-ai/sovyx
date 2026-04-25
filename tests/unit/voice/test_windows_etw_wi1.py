"""Tests for the WI1 Windows audio ETW event-log probe.

Coverage:
* Cross-platform safety (non-win32 returns structured UNKNOWN per
  channel without raising).
* wevtutil binary missing → NOT-found note per channel.
* wevtutil timeout / OSError isolated per channel.
* Channel-not-found stderr → "channel not present" note (not error).
* Successful parse of representative wevtutil text output.
* Level filter integer mapping (XPath construction).
* Bounds clamping on lookback_seconds + max_events_per_channel.
* Per-event raw_text truncation at 4 KB.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._windows_etw import (
    EtwEvent,
    EtwEventLevel,
    EtwQueryResult,
    _level_to_microsoft_int,
    _parse_wevtutil_text_output,
    _split_event_blocks,
    query_audio_etw_events,
)


def _fake_run(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raise_exc: type[BaseException] | None = None,
) -> Any:
    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        if raise_exc is not None:
            raise raise_exc("simulated failure")
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    return _factory


# ── Cross-platform safety ─────────────────────────────────────────


class TestCrossPlatformSafety:
    def test_linux_returns_unknown_per_channel(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events()
        assert len(results) >= 1
        for r in results:
            assert isinstance(r, EtwQueryResult)
            assert r.events == ()
            assert any("non-windows" in n for n in r.notes)

    def test_darwin_returns_unknown_per_channel(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("non-windows" in n for n in r.notes)

    def test_returned_tuple_matches_default_channels(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events()
        # Defaults to the three audio channels.
        assert len(results) == 3  # noqa: PLR2004
        names = {r.channel for r in results}
        assert "Microsoft-Windows-Audio/Operational" in names

    def test_custom_channel_set_respected(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events(channels=("CustomChannel",))
        assert len(results) == 1
        assert results[0].channel == "CustomChannel"


# ── Probe failure isolation ───────────────────────────────────────


class TestProbeFailures:
    def test_wevtutil_missing_returns_per_channel_note(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=None),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("not found" in n for n in r.notes)

    def test_wevtutil_timeout_isolated_per_channel(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("wevtutil", 5),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("timed out" in n for n in r.notes)

    def test_wevtutil_oserror_isolated(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch("subprocess.run", side_effect=OSError("spawn boom")),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert any("spawn failed" in n for n in r.notes)

    def test_channel_not_found_stderr_classified(self) -> None:
        # wevtutil returns rc != 0 with "could not be found" in stderr
        # for missing channels — classify as NOT present, not error.
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    returncode=15007,
                    stderr="The specified channel could not be found.",
                ),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("not present" in n for n in r.notes)

    def test_unknown_nonzero_exit_collapses_to_note(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(returncode=42, stderr="some weird error"),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert any("exited 42" in n for n in r.notes)


# ── Successful parse ─────────────────────────────────────────────


_SAMPLE_WEVTUTIL_TEXT = """\
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
  The audio engine encountered a glitch report on endpoint
  {0.0.1.00000000}.{abc123-def456}.

Event[1]:
  Log Name: Microsoft-Windows-Audio/Operational
  Source: Microsoft-Windows-Audio
  Date: 2026-04-25T12:30:00.000Z
  Event ID: 12
  Task: N/A
  Level: Error
  Opcode: N/A
  Keyword: N/A
  User: S-1-5-18
  User Name: NT AUTHORITY\\SYSTEM
  Computer: HOSTNAME
  Description:
  Endpoint enumeration failed for capture device.
"""


class TestSuccessfulParse:
    def test_parses_two_events_from_sample(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout=_SAMPLE_WEVTUTIL_TEXT, returncode=0),
            ),
        ):
            results = query_audio_etw_events(channels=("OneChannel",))
        assert len(results) == 1
        r = results[0]
        assert r.notes == ()
        assert len(r.events) == 2  # noqa: PLR2004

    def test_first_event_fields_parsed(self) -> None:
        events = _parse_wevtutil_text_output(
            _SAMPLE_WEVTUTIL_TEXT,
            channel="Microsoft-Windows-Audio/Operational",
        )
        assert events[0].event_id == 65  # noqa: PLR2004
        assert events[0].level is EtwEventLevel.WARNING
        assert events[0].timestamp_iso == "2026-04-25T12:34:56.789Z"
        assert events[0].provider == "Microsoft-Windows-Audio"
        assert "glitch report" in events[0].description

    def test_second_event_classified_as_error(self) -> None:
        events = _parse_wevtutil_text_output(
            _SAMPLE_WEVTUTIL_TEXT,
            channel="Microsoft-Windows-Audio/Operational",
        )
        assert events[1].event_id == 12  # noqa: PLR2004
        assert events[1].level is EtwEventLevel.ERROR
        assert "Endpoint enumeration failed" in events[1].description

    def test_raw_text_carries_event_block(self) -> None:
        events = _parse_wevtutil_text_output(
            _SAMPLE_WEVTUTIL_TEXT,
            channel="Microsoft-Windows-Audio/Operational",
        )
        assert "Event ID: 65" in events[0].raw_text or "65" in events[0].raw_text
        assert "Level: Warning" in events[0].raw_text

    def test_raw_text_truncated_to_4kb(self) -> None:
        # Build a single event with a giant description.
        big_desc = "x" * 10_000
        big_event = (
            "Event[0]:\n"
            "  Log Name: Microsoft-Windows-Audio/Operational\n"
            "  Source: Microsoft-Windows-Audio\n"
            "  Date: 2026-04-25T00:00:00.000Z\n"
            "  Event ID: 1\n"
            "  Level: Information\n"
            "  Description:\n"
            f"  {big_desc}\n"
        )
        events = _parse_wevtutil_text_output(
            big_event,
            channel="Microsoft-Windows-Audio/Operational",
        )
        assert len(events) == 1
        assert len(events[0].raw_text) == 4096  # noqa: PLR2004

    def test_description_truncated_to_512(self) -> None:
        big_desc = "y" * 1000
        big_event = (
            "Event[0]:\n"
            "  Source: X\n"
            "  Event ID: 1\n"
            "  Level: Information\n"
            "  Description:\n"
            f"  {big_desc}\n"
        )
        events = _parse_wevtutil_text_output(big_event, channel="C")
        assert len(events[0].description) == 512  # noqa: PLR2004


# ── Empty / malformed input ──────────────────────────────────────


class TestEdgeCases:
    def test_empty_stdout_yields_no_events(self) -> None:
        events = _parse_wevtutil_text_output("", channel="C")
        assert events == ()

    def test_stdout_without_event_headers_yields_no_events(self) -> None:
        events = _parse_wevtutil_text_output("some\nnoise\n", channel="C")
        assert events == ()

    def test_event_with_missing_level_parses_as_unknown(self) -> None:
        block = (
            "Event[0]:\n  Source: X\n  Event ID: 99\n  Description:\n  no level field present\n"
        )
        events = _parse_wevtutil_text_output(block, channel="C")
        assert len(events) == 1
        assert events[0].level is EtwEventLevel.UNKNOWN
        assert events[0].event_id == 99  # noqa: PLR2004

    def test_event_with_garbage_event_id_parses_as_zero(self) -> None:
        block = "Event[0]:\n  Source: X\n  Event ID: not-a-number\n  Level: Warning\n"
        events = _parse_wevtutil_text_output(block, channel="C")
        assert events[0].event_id == 0


# ── Block splitter ───────────────────────────────────────────────


class TestEventBlockSplitter:
    def test_splits_two_blocks(self) -> None:
        blocks = _split_event_blocks(_SAMPLE_WEVTUTIL_TEXT)
        assert len(blocks) == 2  # noqa: PLR2004

    def test_dropsleading_noise_before_first_event(self) -> None:
        # Anything before the first "Event[N]:" line is discarded.
        text = "leading garbage\nmore garbage\n" + _SAMPLE_WEVTUTIL_TEXT
        blocks = _split_event_blocks(text)
        assert len(blocks) == 2  # noqa: PLR2004


# ── Level → Microsoft integer mapping ────────────────────────────


class TestLevelMapping:
    def test_critical_maps_to_1(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.CRITICAL) == 1

    def test_error_maps_to_2(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.ERROR) == 2  # noqa: PLR2004

    def test_warning_maps_to_3(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.WARNING) == 3  # noqa: PLR2004

    def test_info_maps_to_4(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.INFO) == 4  # noqa: PLR2004

    def test_verbose_maps_to_5(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.VERBOSE) == 5  # noqa: PLR2004

    def test_unknown_falls_back_to_4(self) -> None:
        # UNKNOWN should default to "everything except verbose" so we
        # don't lose useful events when a contrived caller passes it.
        assert _level_to_microsoft_int(EtwEventLevel.UNKNOWN) == 4  # noqa: PLR2004


# ── Bounds clamping ──────────────────────────────────────────────


class TestBounds:
    def test_lookback_clamped_to_one_minute_min(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(lookback_seconds=0, channels=("X",))
        # The XPath argument should reflect the clamped 60_000 ms.
        call_argv = run_mock.call_args.args[0]
        xpath_arg = next(a for a in call_argv if a.startswith("/q:"))
        assert "60000" in xpath_arg

    def test_lookback_clamped_to_one_day_max(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(lookback_seconds=10_000_000, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        xpath_arg = next(a for a in call_argv if a.startswith("/q:"))
        # 86400 s × 1000 = 86_400_000 ms.
        assert "86400000" in xpath_arg

    def test_max_events_clamped_to_one_min(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(max_events_per_channel=0, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        c_arg = next(a for a in call_argv if a.startswith("/c:"))
        assert c_arg == "/c:1"

    def test_max_events_clamped_to_500(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(max_events_per_channel=10_000, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        c_arg = next(a for a in call_argv if a.startswith("/c:"))
        assert c_arg == "/c:500"


# ── Report contract ──────────────────────────────────────────────


class TestEventContract:
    def test_event_is_frozen_dataclass(self) -> None:
        ev = EtwEvent(
            channel="C",
            level=EtwEventLevel.WARNING,
            event_id=1,
        )
        with pytest.raises(Exception) as exc:  # noqa: PT011 — FrozenInstanceError
            ev.event_id = 2  # type: ignore[misc]
        assert (
            "frozen" in str(exc.value).lower()
            or "FrozenInstanceError"
            in type(
                exc.value,
            ).__name__
        )

    def test_level_enum_value_stable(self) -> None:
        # StrEnum values are part of the public contract — dashboard
        # consumes the string token.
        assert EtwEventLevel.CRITICAL.value == "critical"
        assert EtwEventLevel.ERROR.value == "error"
        assert EtwEventLevel.WARNING.value == "warning"
        assert EtwEventLevel.INFO.value == "information"
        assert EtwEventLevel.VERBOSE.value == "verbose"
        assert EtwEventLevel.UNKNOWN.value == "unknown"


pytestmark = pytest.mark.timeout(10)
