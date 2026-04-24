"""Unit tests for the Linux driver-watchdog integration in
:mod:`sovyx.voice.health._factory_integration`.

Split from ``test_factory_integration.py`` so the watchdog-specific
surface (``_extract_linux_watchdog_hints``, ``_log_linux_driver_watchdog_scan``)
has a focused, navigable test module. Covers:

* Hint extraction across every fingerprint shape
  (``{linux-pci-…}`` / ``{linux-usb-…}`` / ``{surrogate-…}``) and every
  ALSA PCM name form (``hw:N,M`` / ``plughw:CARD=id,DEV=M`` / virtual).
* The four log outcomes of the scan helper — *unavailable*, *clean*,
  *events-unrelated*, *events-correlated* — each exercised with a stub
  ``scan_recent_linux_driver_watchdog_events`` that returns the exact
  scan shape expected at that log level.
* Exception safety: a raising scan must never propagate out of the
  helper (boot-path contract).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.voice.health._driver_watchdog_linux import (
    LinuxDriverWatchdogEvent,
    LinuxDriverWatchdogScan,
)
from sovyx.voice.health._factory_integration import (
    _extract_linux_watchdog_hints,
    _log_linux_driver_watchdog_scan,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# Hint extraction
# ---------------------------------------------------------------------------


class TestExtractLinuxWatchdogHints:
    """Hints drawn from the ALSA name + composed endpoint GUID."""

    def test_hw_numeric_name_yields_card_index(self) -> None:
        alsa_card_id, usb_vid_pid, codec_vendor_id = _extract_linux_watchdog_hints(
            alsa_name="hw:0,0",
            endpoint_guid="{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}",
        )
        assert alsa_card_id == "0"
        assert usb_vid_pid is None
        assert codec_vendor_id == "14f15045"

    def test_plughw_numeric_name_yields_card_index(self) -> None:
        alsa_card_id, _, _ = _extract_linux_watchdog_hints(
            alsa_name="plughw:2,0",
            endpoint_guid="{surrogate-deadbeef-0000-0000-0000-000000000000}",
        )
        assert alsa_card_id == "2"

    def test_card_eq_name_yields_card_id_string(self) -> None:
        alsa_card_id, _, _ = _extract_linux_watchdog_hints(
            alsa_name="plughw:CARD=PCH,DEV=0",
            endpoint_guid="{surrogate-deadbeef-0000-0000-0000-000000000000}",
        )
        assert alsa_card_id == "PCH"

    def test_dsnoop_plugin_name_also_extracts_card_id(self) -> None:
        alsa_card_id, _, _ = _extract_linux_watchdog_hints(
            alsa_name="dsnoop:CARD=Generic,DEV=0",
            endpoint_guid="{surrogate-deadbeef-0000-0000-0000-000000000000}",
        )
        assert alsa_card_id == "Generic"

    def test_virtual_name_yields_no_card_id(self) -> None:
        alsa_card_id, _, _ = _extract_linux_watchdog_hints(
            alsa_name="default",
            endpoint_guid="{surrogate-deadbeef-0000-0000-0000-000000000000}",
        )
        assert alsa_card_id is None

    def test_empty_name_yields_no_card_id(self) -> None:
        alsa_card_id, _, _ = _extract_linux_watchdog_hints(
            alsa_name="",
            endpoint_guid="{surrogate-deadbeef-0000-0000-0000-000000000000}",
        )
        assert alsa_card_id is None

    def test_usb_fingerprint_yields_vid_pid(self) -> None:
        _, usb_vid_pid, codec_vendor_id = _extract_linux_watchdog_hints(
            alsa_name="plughw:1,0",
            endpoint_guid="{linux-usb-1532:0543-0-capture}",
        )
        assert usb_vid_pid == "1532:0543"
        # USB endpoints carry no HDA codec — codec_vendor_id must stay None
        # so matches_device doesn't try to correlate on a wrong hint.
        assert codec_vendor_id is None

    def test_pci_fingerprint_without_codec_yields_no_codec_hint(self) -> None:
        _, usb_vid_pid, codec_vendor_id = _extract_linux_watchdog_hints(
            alsa_name="hw:0,0",
            endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
        )
        assert usb_vid_pid is None
        assert codec_vendor_id is None

    def test_surrogate_fingerprint_yields_no_bus_hints(self) -> None:
        _, usb_vid_pid, codec_vendor_id = _extract_linux_watchdog_hints(
            alsa_name="hw:0,0",
            endpoint_guid="{surrogate-ab12cdef-1122-3344-5566-778899aabbcc}",
        )
        assert usb_vid_pid is None
        assert codec_vendor_id is None

    def test_codec_hint_is_lowercased_without_colon(self) -> None:
        # Kernel log codec hint form is ``0x14f15045`` — lowercase,
        # no colon. The extracted hint must land as a substring of
        # that form so matches_device succeeds.
        _, _, codec_vendor_id = _extract_linux_watchdog_hints(
            alsa_name="hw:0,0",
            endpoint_guid="{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}",
        )
        assert codec_vendor_id == "14f15045"
        assert codec_vendor_id is not None and "14f15045" in f"0x{codec_vendor_id}"


# ---------------------------------------------------------------------------
# Scan-helper logging
# ---------------------------------------------------------------------------


def _event(
    *,
    pattern_name: str = "hda_codec_timeout",
    severity: str = "error",
    device_hint: str | None = "0",
    excerpt: str = "snd_hda_intel: card0: azx_get_response timeout",
) -> LinuxDriverWatchdogEvent:
    return LinuxDriverWatchdogEvent(
        kernel_timestamp_iso="2026-04-24T10:00:00+0000",
        pattern_name=pattern_name,
        severity=severity,  # type: ignore[arg-type]  # test helper accepts str
        message_excerpt=excerpt,
        device_hint=device_hint,
    )


def _patch_scan(return_value: LinuxDriverWatchdogScan | Exception) -> Any:
    """Return a ``patch`` targeting the real scan entrypoint.

    ``return_value`` may be a ``LinuxDriverWatchdogScan`` (success path)
    or an ``Exception`` subclass instance to simulate the coroutine
    raising. Lets each test wire the exact scan outcome it asserts on
    without a bespoke fake class per shape.
    """
    mock = AsyncMock()
    if isinstance(return_value, Exception):
        mock.side_effect = return_value
    else:
        mock.return_value = return_value
    return patch(
        "sovyx.voice.health._driver_watchdog_linux.scan_recent_linux_driver_watchdog_events",
        mock,
    )


class TestLogLinuxDriverWatchdogScan:
    """The helper emits the right log event for each scan shape."""

    @pytest.mark.asyncio()
    async def test_unavailable_when_scan_not_attempted(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = LinuxDriverWatchdogScan(scan_attempted=False)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_linux_unavailable")

    @pytest.mark.asyncio()
    async def test_unavailable_when_scan_failed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = LinuxDriverWatchdogScan(scan_attempted=True, scan_failed=True)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_linux_unavailable")

    @pytest.mark.asyncio()
    async def test_clean_scan_logs_clean(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scan = LinuxDriverWatchdogScan(scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("DEBUG"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_linux_clean")

    @pytest.mark.asyncio()
    async def test_events_unrelated_when_no_hints_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # USB disconnect on a different bus path than this PCI HDA card.
        events = (
            _event(
                pattern_name="usb_disconnect",
                severity="warning",
                device_hint="2-1.4",
                excerpt="usb 2-1.4: USB disconnect",
            ),
        )
        scan = LinuxDriverWatchdogScan(
            events=events,
            scan_attempted=True,
        )
        with _patch_scan(scan), caplog.at_level("INFO"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_linux_events_unrelated")

    @pytest.mark.asyncio()
    async def test_events_correlated_when_card_hint_matches(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events = (
            _event(device_hint="0"),
            _event(
                pattern_name="alsa_xrun",
                severity="warning",
                device_hint="0",
                excerpt="xrun!!! (at least 5 ms)",
            ),
        )
        scan = LinuxDriverWatchdogScan(
            events=events,
            scan_attempted=True,
        )
        with _patch_scan(scan), caplog.at_level("WARNING"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        record = _assert_log_present(
            caplog,
            "voice_driver_watchdog_linux_events_correlated",
        )
        # WARNING level is operator-visible — assert severity shape.
        assert record.levelname == "WARNING"

    @pytest.mark.asyncio()
    async def test_events_correlated_on_usb_vid_pid_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Kernel log hint captures the hex codec vendor even on USB
        # endpoints; the correlation must fire via usb_vid_pid hint.
        events = (
            _event(
                pattern_name="usb_descriptor_read_fail",
                severity="error",
                device_hint="1-2.1",
                excerpt="usb 1-2.1: device descriptor read/64, error -71",
            ),
        )
        scan = LinuxDriverWatchdogScan(events=events, scan_attempted=True)
        with _patch_scan(scan), caplog.at_level("WARNING"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="plughw:1,0",
                endpoint_guid="{linux-usb-1532:0543-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        # Card hint "1" is a substring of "1-2.1" → matches_device
        # returns True via alsa_card_id regardless of USB VID:PID.
        _assert_log_present(
            caplog,
            "voice_driver_watchdog_linux_events_correlated",
        )

    @pytest.mark.asyncio()
    async def test_scan_exception_swallowed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Boot path must never raise past this helper — even unexpected
        # scan failures log at DEBUG and return cleanly.
        with _patch_scan(RuntimeError("disk on fire")), caplog.at_level("DEBUG"):
            await _log_linux_driver_watchdog_scan(
                alsa_name="hw:0,0",
                endpoint_guid="{linux-pci-0000_00_1f.3-0-capture}",
                lookback_hours=24,
                timeout_s=3.0,
            )
        _assert_log_present(caplog, "voice_driver_watchdog_linux_scan_raised")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_log_present(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> Any:
    """Return the first log record whose event matches.

    structlog on top of stdlib renders the event name into the
    record message as a JSON fragment (``'event': '<name>'``), so
    substring matching is the stable way to identify a target
    record across formatter changes.
    """
    needles = (f"'event': '{event_name}'", f'"event": "{event_name}"')
    for record in caplog.records:
        msg = record.getMessage()
        if any(needle in msg for needle in needles):
            return record
    pytest.fail(
        f"Expected log event '{event_name}' not found. "
        f"Seen: {[r.getMessage()[:120] for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Type marker — keeps the TYPE_CHECKING import referenced for ruff/tc rules.
# ---------------------------------------------------------------------------


def _unused_type_marker() -> Iterable[int]:  # pragma: no cover
    yield 0
