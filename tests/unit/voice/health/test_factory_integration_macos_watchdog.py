"""Unit tests for the macOS driver-watchdog integration in
:mod:`sovyx.voice.health._factory_integration`.

Peer of ``test_factory_integration_linux_watchdog.py``. Covers:

* Hint extraction across every macOS fingerprint shape
  (``{macos-usb-…}`` / ``{macos-builtin-…}`` / ``{macos-bluetooth-…}``
  / ``{surrogate-…}``).
* The four log outcomes of the scan helper — *unavailable*, *clean*,
  *events-unrelated*, *events-correlated* — each with a stub
  ``scan_recent_macos_driver_watchdog_events`` returning the exact
  scan shape expected at that log level.
* Exception safety: a raising scan must never propagate (boot-path
  contract).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.voice.health._driver_watchdog_macos import (
    MacosDriverWatchdogEvent,
    MacosDriverWatchdogScan,
)
from sovyx.voice.health._factory_integration import (
    _extract_macos_watchdog_hints,
    _log_macos_driver_watchdog_scan,
)

# ---------------------------------------------------------------------------
# Hint extraction
# ---------------------------------------------------------------------------


class TestExtractMacosWatchdogHints:
    def test_usb_fingerprint_yields_device_uid(self) -> None:
        device_uid, device_name = _extract_macos_watchdog_hints(
            device_name="Razer Seiren X",
            endpoint_guid="{macos-usb-AppleUSBAudioEngine_Razer_Seiren-input}",
        )
        assert device_uid == "AppleUSBAudioEngine_Razer_Seiren"
        assert device_name == "Razer Seiren X"

    def test_builtin_fingerprint_yields_device_uid(self) -> None:
        device_uid, _ = _extract_macos_watchdog_hints(
            device_name="MacBook Pro Microphone",
            endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
        )
        assert device_uid == "BuiltInMicrophoneDevice"

    def test_bluetooth_fingerprint_yields_device_uid(self) -> None:
        device_uid, _ = _extract_macos_watchdog_hints(
            device_name="AirPods Pro",
            endpoint_guid="{macos-bluetooth-BluetoothA2DP_AA_BB_CC_DD_EE_FF-duplex}",
        )
        assert device_uid == "BluetoothA2DP_AA_BB_CC_DD_EE_FF"

    def test_surrogate_fingerprint_yields_no_device_uid(self) -> None:
        device_uid, device_name = _extract_macos_watchdog_hints(
            device_name="Some Device",
            endpoint_guid="{surrogate-ab12cdef-1122-3344-5566-778899aabbcc}",
        )
        assert device_uid is None
        assert device_name == "Some Device"

    def test_linux_fingerprint_yields_no_device_uid(self) -> None:
        # The macOS back-parser must not accidentally match on a
        # Linux fingerprint that happens to share ``-`` separators.
        device_uid, _ = _extract_macos_watchdog_hints(
            device_name="hw:0,0",
            endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
        )
        assert device_uid is None

    def test_empty_device_name_collapses_to_none(self) -> None:
        _, device_name = _extract_macos_watchdog_hints(
            device_name="",
            endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
        )
        assert device_name is None

    def test_whitespace_device_name_collapses_to_none(self) -> None:
        _, device_name = _extract_macos_watchdog_hints(
            device_name="   ",
            endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
        )
        assert device_name is None


# ---------------------------------------------------------------------------
# Scan-helper logging
# ---------------------------------------------------------------------------


def _event(
    *,
    pattern_name: str = "hal_io_engine_error",
    severity: str = "error",
    device_hint: str | None = "BuiltInMicrophoneDevice",
    excerpt: str = "HALS_IOA1Engine: Err 0x1",
) -> MacosDriverWatchdogEvent:
    return MacosDriverWatchdogEvent(
        kernel_timestamp_iso="2026-04-24T10:00:00+0000",
        pattern_name=pattern_name,
        severity=severity,  # type: ignore[arg-type]
        message_excerpt=excerpt,
        device_hint=device_hint,
    )


def _patch_scan(return_value: MacosDriverWatchdogScan | Exception) -> Any:
    mock = AsyncMock()
    if isinstance(return_value, Exception):
        mock.side_effect = return_value
    else:
        mock.return_value = return_value
    return patch(
        "sovyx.voice.health._driver_watchdog_macos.scan_recent_macos_driver_watchdog_events",
        mock,
    )


def _assert_log_present(caplog: pytest.LogCaptureFixture, event_name: str) -> Any:
    needles = (f"'event': '{event_name}'", f'"event": "{event_name}"')
    for record in caplog.records:
        msg = record.getMessage()
        if any(needle in msg for needle in needles):
            return record
    pytest.fail(
        f"Expected log event '{event_name}' not found. "
        f"Seen: {[r.getMessage()[:120] for r in caplog.records]}"
    )


class TestLogMacosDriverWatchdogScan:
    @pytest.mark.asyncio()
    async def test_unavailable_when_scan_not_attempted(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = MacosDriverWatchdogScan(scan_attempted=False)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_unavailable")

    @pytest.mark.asyncio()
    async def test_unavailable_when_scan_failed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = MacosDriverWatchdogScan(scan_attempted=True, scan_failed=True)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_unavailable")

    @pytest.mark.asyncio()
    async def test_clean_scan_logs_clean(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = MacosDriverWatchdogScan(scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_clean")

    @pytest.mark.asyncio()
    async def test_events_unrelated_when_no_hints_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events = (
            _event(
                pattern_name="usb_audio_lost_device",
                device_hint="DifferentDevice",
                excerpt="USBAudio: Lost device during stream",
            ),
        )
        scan = MacosDriverWatchdogScan(events=events, scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("INFO"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_events_unrelated")

    @pytest.mark.asyncio()
    async def test_events_correlated_on_device_uid_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events = (
            _event(device_hint="BuiltInMicrophoneDevice"),
            _event(
                pattern_name="audio_unit_buffer_overrun",
                severity="warning",
                device_hint="BuiltIn",
                excerpt="kAudioUnitErr_TooManyFramesToProcess",
            ),
        )
        scan = MacosDriverWatchdogScan(events=events, scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("WARNING"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        record = _assert_log_present(
            caplog,
            "voice_driver_watchdog_macos_events_correlated",
        )
        assert record.levelname == "WARNING"

    @pytest.mark.asyncio()
    async def test_events_correlated_on_device_name_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Surrogate fingerprint → no device_uid extracted, correlation
        # must still fire via device_name substring match.
        events = (
            _event(
                pattern_name="input_overload_detected",
                severity="warning",
                device_hint="name=MacBook Pro Microphone",
                excerpt="HAL: input overload detected on MacBook Pro Microphone",
            ),
        )
        scan = MacosDriverWatchdogScan(events=events, scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("WARNING"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{surrogate-ab12cdef-1122-3344-5566-778899aabbcc}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_events_correlated")

    @pytest.mark.asyncio()
    async def test_scan_exception_swallowed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with _patch_scan(RuntimeError("log show on fire")), caplog.at_level("DEBUG"):
            await _log_macos_driver_watchdog_scan(
                device_name="MacBook Pro Microphone",
                endpoint_guid="{macos-builtin-BuiltInMicrophoneDevice-input}",
                lookback_hours=24,
                timeout_s=5.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_macos_scan_raised")
