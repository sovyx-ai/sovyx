"""Tests for :mod:`sovyx.voice.health._driver_watchdog_macos`.

Sprint 3 deliverable: ``log show --predicate`` scan for coreaudiod
distress signals, peer of the Linux journalctl scan and Windows
Kernel-PnP scan. The scanner shells out to ``/usr/bin/log``; these
tests patch :func:`asyncio.create_subprocess_exec` so no real log
command runs.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice.health._driver_watchdog_macos import (
    _DISTRESS_PATTERNS,
    MacosDriverWatchdogEvent,
    MacosDriverWatchdogScan,
    _extract_device_hint,
    _parse_events,
    scan_recent_macos_driver_watchdog_events,
)

# ---------------------------------------------------------------------------
# Pattern catalog invariants
# ---------------------------------------------------------------------------


class TestPatternCatalog:
    """The pattern tuple shape must survive refactors intact."""

    def test_pattern_names_are_unique(self) -> None:
        names = [p.name for p in _DISTRESS_PATTERNS]
        assert len(names) == len(set(names))

    def test_severities_are_constrained(self) -> None:
        for pattern in _DISTRESS_PATTERNS:
            assert pattern.severity in {"warning", "error"}

    def test_hard_failures_are_error(self) -> None:
        hard_fail = {
            "hal_io_engine_error",
            "aggregate_device_disconnected",
            "io_audio_family_allocation_failed",
            "usb_audio_lost_device",
            "coreaudiod_watchdog_reset",
        }
        for pattern in _DISTRESS_PATTERNS:
            if pattern.name in hard_fail:
                assert pattern.severity == "error", (
                    f"{pattern.name} is a hard failure — must be 'error'"
                )


# ---------------------------------------------------------------------------
# Per-pattern parsing — one realistic line per catalog entry
# ---------------------------------------------------------------------------


def _log_line(ts: str, proc: str, msg: str) -> str:
    """Render a ``log show --style syslog`` line."""
    # Format: ts thread-id severity 0x0 pid 0x0 process: message
    return f"{ts} 0x123456 Default     0x0  456    0    {proc}: {msg}"


class TestParseEvents:
    """Each pattern must match a realistic ``log show`` body."""

    def test_hal_io_engine_error(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:30.000000-0700",
            "coreaudiod",
            "HALS_IOA1Engine: Err 0x1717 from IOAudioEngine",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "hal_io_engine_error"
        assert events[0].severity == "error"

    def test_aggregate_device_disconnected(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:31.000000-0700",
            "coreaudiod",
            "AudioAggregateDevice: slave Disconnected",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "aggregate_device_disconnected"

    def test_io_audio_family_allocation_failed(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:32.000000-0700",
            "kernel",
            "IOAudioFamily: Failed to allocate stream buffer",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "io_audio_family_allocation_failed"

    def test_usb_audio_lost_device(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:33.000000-0700",
            "coreaudiod",
            "USBAudio: Lost device during stream",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "usb_audio_lost_device"

    def test_coreaudiod_watchdog_reset(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:34.000000-0700",
            "launchd",
            "coreaudiod hit watchdog timeout, restarting",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "coreaudiod_watchdog_reset"

    def test_audio_unit_buffer_overrun(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:35.000000-0700",
            "coreaudiod",
            "AUGraph: kAudioUnitErr_TooManyFramesToProcess under load",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "audio_unit_buffer_overrun"
        assert events[0].severity == "warning"

    def test_input_overload_detected(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:36.000000-0700",
            "coreaudiod",
            "HAL: input overload detected on device 12345",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "input_overload_detected"

    def test_empty_input(self) -> None:
        assert _parse_events("") == ()

    def test_whitespace_and_blank_lines_skipped(self) -> None:
        assert _parse_events("\n\n   \n\n") == ()

    def test_unrelated_lines_skipped(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:40.000000-0700",
            "coreaudiod",
            "routine configuration change, no error",
        )
        assert _parse_events(line) == ()

    def test_max_events_cap(self) -> None:
        # More than _MAX_EVENTS matching lines — ensure the cap holds.
        lines = "\n".join(
            _log_line(
                "2026-04-24 10:20:30.000000-0700",
                "coreaudiod",
                "HALS_IOA1Engine: Err abc",
            )
            for _ in range(200)
        )
        events = _parse_events(lines)
        assert len(events) == 100  # _MAX_EVENTS

    def test_multiple_lines_yield_multiple_events(self) -> None:
        lines = "\n".join(
            [
                _log_line(
                    "2026-04-24 10:20:30.000000-0700",
                    "coreaudiod",
                    "HALS_IOA1Engine: Err 0x1",
                ),
                _log_line(
                    "2026-04-24 10:20:31.000000-0700",
                    "coreaudiod",
                    "kAudioUnitErr_TooManyFramesToProcess",
                ),
            ],
        )
        events = _parse_events(lines)
        assert len(events) == 2
        assert events[0].pattern_name == "hal_io_engine_error"
        assert events[1].pattern_name == "audio_unit_buffer_overrun"

    def test_envelope_strip_timestamp_preserved(self) -> None:
        line = _log_line(
            "2026-04-24 10:20:30.123456-0700",
            "coreaudiod",
            "HALS_IOA1Engine: Err 0x1",
        )
        events = _parse_events(line)
        assert len(events) == 1
        # Timestamp captured; format preserved.
        assert "2026-04-24" in events[0].kernel_timestamp_iso

    def test_first_pattern_wins_on_overlap(self) -> None:
        # Message matches two patterns — hal_io_engine_error is first.
        line = _log_line(
            "2026-04-24 10:20:30.000000-0700",
            "coreaudiod",
            "HALS_IOA1Engine: Err coreaudiod watchdog timeout",
        )
        events = _parse_events(line)
        assert len(events) == 1
        assert events[0].pattern_name == "hal_io_engine_error"


# ---------------------------------------------------------------------------
# Device-hint extraction
# ---------------------------------------------------------------------------


class TestDeviceHintExtraction:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("deviceUID='BuiltInMicrophoneDevice' failed", "BuiltInMicrophoneDevice"),
            (
                "USB device VendorID=0x1532 ProductID=0x0543 lost",
                "1532:0543",
            ),
            ("HAL error on name=MacBookMic,4", "MacBookMic"),
            ("no device info here", None),
        ],
    )
    def test_extraction(self, message: str, expected: str | None) -> None:
        assert _extract_device_hint(message) == expected


# ---------------------------------------------------------------------------
# matches_device semantics
# ---------------------------------------------------------------------------


def _event(
    *,
    pattern_name: str = "hal_io_engine_error",
    severity: str = "error",
    device_hint: str | None = "BuiltInMicrophoneDevice",
) -> MacosDriverWatchdogEvent:
    return MacosDriverWatchdogEvent(
        kernel_timestamp_iso="2026-04-24T10:00:00+0000",
        pattern_name=pattern_name,
        severity=severity,  # type: ignore[arg-type]
        message_excerpt="HALS_IOA1Engine: Err 0x1",
        device_hint=device_hint,
    )


class TestMatchesDevice:
    def test_no_hints_returns_false(self) -> None:
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        assert scan.matches_device() is False

    def test_matches_on_device_uid(self) -> None:
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        assert scan.matches_device(device_uid="BuiltInMicrophoneDevice") is True

    def test_matches_case_insensitive(self) -> None:
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        assert scan.matches_device(device_uid="BUILTINMICROPHONEDEVICE") is True

    def test_matches_on_substring(self) -> None:
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        # Partial match — needle inside haystack.
        assert scan.matches_device(device_uid="BuiltIn") is True

    def test_matches_on_reverse_substring(self) -> None:
        scan = MacosDriverWatchdogScan(
            events=(_event(device_hint="BuiltIn"),),
            scan_attempted=True,
        )
        # Partial match — haystack inside needle (observed in the wild
        # when log shortens the UID).
        assert scan.matches_device(device_uid="BuiltInMicrophoneDevice") is True

    def test_no_match_returns_false(self) -> None:
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        assert scan.matches_device(device_uid="completely-different-uid") is False

    def test_any_match_wins(self) -> None:
        scan = MacosDriverWatchdogScan(
            events=(
                _event(device_hint="Device A"),
                _event(device_hint="Device B"),
            ),
            scan_attempted=True,
        )
        assert scan.matches_device(device_name="Device B") is True

    def test_none_hints_skipped(self) -> None:
        # Only one hint provided; the other two are None — still matches.
        scan = MacosDriverWatchdogScan(events=(_event(),), scan_attempted=True)
        assert scan.matches_device(device_uid="BuiltIn") is True


# ---------------------------------------------------------------------------
# Subprocess degradation paths
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Stand-in for ``asyncio.subprocess.Process`` used by the scanner."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate_raises: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._communicate_raises = communicate_raises
        self.kill_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._communicate_raises is not None:
            raise self._communicate_raises
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.kill_called = True

    async def wait(self) -> int:
        return self.returncode


def _fake_subprocess_exec(proc: _FakeProcess | BaseException) -> Any:
    """Return an async stand-in for ``asyncio.create_subprocess_exec``."""

    async def _exec(*_argv, **_kwargs):  # noqa: ANN002 — mimic stdlib signature
        if isinstance(proc, BaseException):
            raise proc
        return proc

    return _exec


class TestScanDegradation:
    @pytest.mark.asyncio()
    async def test_non_darwin_skips_subprocess(self) -> None:
        called = False

        async def _exec(*_argv, **_kwargs):  # noqa: ANN002 — mimic stdlib
            nonlocal called
            called = True

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", _exec),
        ):
            scan = await scan_recent_macos_driver_watchdog_events()
        assert scan.scan_attempted is False
        assert scan.scan_failed is False
        assert called is False

    @pytest.mark.asyncio()
    async def test_spawn_failure_marks_not_attempted(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "asyncio.create_subprocess_exec",
                _fake_subprocess_exec(FileNotFoundError("log")),
            ),
        ):
            scan = await scan_recent_macos_driver_watchdog_events()
        assert scan.scan_attempted is False
        assert scan.scan_failed is False

    @pytest.mark.asyncio()
    async def test_timeout_marks_attempted_and_failed(self) -> None:
        proc = _FakeProcess(communicate_raises=TimeoutError())
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "asyncio.create_subprocess_exec",
                _fake_subprocess_exec(proc),
            ),
            patch("asyncio.wait_for", side_effect=TimeoutError),
        ):
            scan = await scan_recent_macos_driver_watchdog_events(timeout_s=0.1)
        assert scan.scan_attempted is True
        assert scan.scan_failed is True

    @pytest.mark.asyncio()
    async def test_nonzero_exit_marks_failed(self) -> None:
        proc = _FakeProcess(returncode=1, stderr=b"permission denied")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "asyncio.create_subprocess_exec",
                _fake_subprocess_exec(proc),
            ),
        ):
            scan = await scan_recent_macos_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is True

    @pytest.mark.asyncio()
    async def test_empty_stdout_succeeds_with_no_events(self) -> None:
        proc = _FakeProcess(stdout=b"")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "asyncio.create_subprocess_exec",
                _fake_subprocess_exec(proc),
            ),
        ):
            scan = await scan_recent_macos_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert scan.any_events is False

    @pytest.mark.asyncio()
    async def test_happy_path_returns_events(self) -> None:
        body = _log_line(
            "2026-04-24 10:20:30.000000-0700",
            "coreaudiod",
            "HALS_IOA1Engine: Err 0x1",
        )
        proc = _FakeProcess(stdout=body.encode("utf-8"))
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "asyncio.create_subprocess_exec",
                _fake_subprocess_exec(proc),
            ),
        ):
            scan = await scan_recent_macos_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert scan.any_events is True
        assert scan.events[0].pattern_name == "hal_io_engine_error"


# ---------------------------------------------------------------------------
# Argv shape regression
# ---------------------------------------------------------------------------


class TestScanArgvShape:
    """The invocation shape is user-observable (process list).

    These tests lock the argv contract so accidental refactors
    surface in the test suite rather than in production.
    """

    @pytest.mark.asyncio()
    async def test_argv_contains_predicate_and_lookback(self) -> None:
        captured: list[tuple[str, ...]] = []
        proc = _FakeProcess(stdout=b"")

        async def _exec(*argv, **_kwargs):  # noqa: ANN002 — mimic stdlib
            captured.append(argv)
            return proc

        with (
            patch.object(sys, "platform", "darwin"),
            patch("asyncio.create_subprocess_exec", _exec),
        ):
            await scan_recent_macos_driver_watchdog_events(
                lookback_hours=6,
                log_exe="/custom/path/log",
            )

        assert len(captured) == 1
        argv = captured[0]
        assert argv[0] == "/custom/path/log"
        assert "show" in argv
        assert "--last" in argv
        assert "6h" in argv
        assert "--predicate" in argv
        assert any("coreaudiod" in a for a in argv)
        assert "--style" in argv
        assert "syslog" in argv
