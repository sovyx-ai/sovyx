"""Tests for MA14 — macOS recent audio events probe (Step 6.d).

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.d.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

from sovyx.voice.health._macos_sysdiagnose import (
    MacosLogEventLevel,
    _level_for_token,
    _parse_log_line,
    query_audio_log_events,
)


class TestLevelForToken:
    def test_default_maps_to_info(self) -> None:
        assert _level_for_token("Default") is MacosLogEventLevel.INFO

    def test_error_maps_to_warning(self) -> None:
        assert _level_for_token("Error") is MacosLogEventLevel.WARNING

    def test_fault_maps_to_fault(self) -> None:
        assert _level_for_token("Fault") is MacosLogEventLevel.FAULT

    def test_debug_maps_to_debug(self) -> None:
        assert _level_for_token("Debug") is MacosLogEventLevel.DEBUG

    def test_unknown_falls_back_to_info(self) -> None:
        assert _level_for_token("Unknown") is MacosLogEventLevel.INFO


class TestParseLogLine:
    def test_parses_full_line_with_subsystem(self) -> None:
        line = (
            "2026-04-25 12:34:56.123456-0300  Default  0x1234   coreaudiod: "
            "(com.apple.audio.AudioHardwareService) Device added: USB Headset"
        )
        event = _parse_log_line(line)
        assert event is not None
        assert event.level is MacosLogEventLevel.INFO
        assert event.process == "coreaudiod"
        assert event.subsystem == "com.apple.audio.AudioHardwareService"
        assert "Device added" in event.description

    def test_parses_line_without_subsystem(self) -> None:
        line = (
            "2026-04-25 12:34:56.123456-0300  Error  0x9999   AudioComponentRegistrar: "
            "Plug-in registration failed"
        )
        event = _parse_log_line(line)
        assert event is not None
        assert event.level is MacosLogEventLevel.WARNING
        assert event.process == "AudioComponentRegistrar"
        assert event.subsystem == ""
        assert "registration failed" in event.description

    def test_truncates_long_descriptions(self) -> None:
        long_msg = "x" * 500
        line = f"2026-04-25 12:34:56.123456-0300  Default  0x1   proc: {long_msg}"
        event = _parse_log_line(line)
        assert event is not None
        assert len(event.description) <= 256

    def test_empty_line_returns_none(self) -> None:
        assert _parse_log_line("") is None
        assert _parse_log_line("   \n") is None

    def test_malformed_line_returns_none(self) -> None:
        assert _parse_log_line("not enough fields") is None


class TestQueryAudioLogEvents:
    def test_non_darwin_returns_empty_with_note(self) -> None:
        with patch.object(sys, "platform", "linux"):
            result = query_audio_log_events()
        assert result.events == ()
        assert any("non-darwin" in n for n in result.notes)

    def test_log_binary_missing_returns_empty(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value=None,
            ),
        ):
            result = query_audio_log_events()
        assert result.events == ()
        assert any("log binary not found" in n for n in result.notes)

    def test_happy_path_parses_events_newest_first(self) -> None:
        synthetic_stdout = (
            "Filtering the log data using \"subsystem == 'com.apple.audio'\"\n"
            "Skipping info and debug messages, pass --info and --debug to include.\n"
            "Timestamp                       (process)[PID]\n"
            "==========\n"
            "2026-04-25 12:34:55.000000-0300  Default  0x1   coreaudiod: "
            "(com.apple.audio) First event\n"
            "2026-04-25 12:34:56.000000-0300  Default  0x1   coreaudiod: "
            "(com.apple.audio) Second event\n"
            "2026-04-25 12:34:57.000000-0300  Error    0x1   coreaudiod: "
            "(com.apple.audio) Third event\n"
        )
        mock_result = MagicMock(returncode=0, stdout=synthetic_stdout, stderr="")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value="/usr/bin/log",
            ),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.subprocess.run",
                return_value=mock_result,
            ),
        ):
            result = query_audio_log_events()
        # Newest-first: Third event (Error) should come first.
        assert len(result.events) == 3
        assert "Third event" in result.events[0].description
        assert result.events[0].level is MacosLogEventLevel.WARNING
        # Second event in the middle.
        assert "Second event" in result.events[1].description
        # First event last (oldest).
        assert "First event" in result.events[2].description

    def test_max_events_bounds_result(self) -> None:
        # Generate 200 synthetic lines; cap to 50.
        lines = [
            f"2026-04-25 12:34:{i:02d}.000000-0300  Default  0x1   coreaudiod: msg-{i}"
            for i in range(60)
        ]
        synthetic_stdout = "\n".join(lines)
        mock_result = MagicMock(returncode=0, stdout=synthetic_stdout, stderr="")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value="/usr/bin/log",
            ),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.subprocess.run",
                return_value=mock_result,
            ),
        ):
            result = query_audio_log_events(max_events=50)
        assert len(result.events) == 50

    def test_subprocess_timeout_returns_empty(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value="/usr/bin/log",
            ),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="log", timeout=8.0),
            ),
        ):
            result = query_audio_log_events()
        assert result.events == ()
        assert any("timed out" in n for n in result.notes)

    def test_subprocess_nonzero_returns_empty(self) -> None:
        mock_result = MagicMock(
            returncode=1,
            stdout="",
            stderr="log: invalid predicate",
        )
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value="/usr/bin/log",
            ),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.subprocess.run",
                return_value=mock_result,
            ),
        ):
            result = query_audio_log_events()
        assert result.events == ()
        assert any("exited 1" in n for n in result.notes)

    def test_lookback_is_recorded_in_result(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sysdiagnose.shutil.which",
                return_value=None,
            ),
        ):
            result = query_audio_log_events(lookback="1h")
        assert result.lookback == "1h"
