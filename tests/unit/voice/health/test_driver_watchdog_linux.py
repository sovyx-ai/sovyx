"""Unit tests for :mod:`sovyx.voice.health._driver_watchdog_linux`.

Sprint 1B deliverable. Exercises the pattern catalog, the
journalctl envelope parser, device-hint extraction, subprocess
degradation paths, and the public ``matches_device`` contract —
every path is injection-friendly (``journalctl_exe`` swapped for
a test double) so the tests never touch the real systemd journal.
"""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sovyx.voice.health._driver_watchdog_linux import (
    _DISTRESS_PATTERNS,
    _JOURNALCTL_EXE,
    LinuxDriverWatchdogEvent,
    LinuxDriverWatchdogScan,
    _extract_device_hint,
    _parse_events,
    scan_recent_linux_driver_watchdog_events,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# ── Pure parser tests (no subprocess) ──────────────────────────────


class TestPatternCatalog:
    """Catalog invariants that external consumers (dashboards,
    telemetry) key off. Treat renames as breaking changes."""

    def test_has_all_named_patterns(self) -> None:
        names = {p.name for p in _DISTRESS_PATTERNS}
        expected = {
            "hda_codec_timeout",
            "hda_codec_disconnected",
            "hda_no_pin_widget",
            "hda_irq_timing_workaround",
            "usb_descriptor_read_fail",
            "usb_disconnect",
            "alsa_xrun",
        }
        assert expected <= names, (
            f"Pattern catalog lost a known signal: missing {expected - names}"
        )

    def test_severity_values_are_valid(self) -> None:
        for pattern in _DISTRESS_PATTERNS:
            assert pattern.severity in ("warning", "error"), (
                f"Pattern {pattern.name!r} has invalid severity {pattern.severity!r}"
            )

    def test_error_severity_for_hard_failures(self) -> None:
        hard_fails = {p.name for p in _DISTRESS_PATTERNS if p.severity == "error"}
        # These THREE are classified as hard failures by design;
        # any future demotion requires a rationale + telemetry
        # re-calibration.
        assert "hda_codec_timeout" in hard_fails
        assert "hda_codec_disconnected" in hard_fails
        assert "usb_descriptor_read_fail" in hard_fails


class TestParseEvents:
    """Walk realistic journalctl output through the parser."""

    def test_empty_payload_returns_empty(self) -> None:
        assert _parse_events("") == ()

    def test_whitespace_only_returns_empty(self) -> None:
        assert _parse_events("  \n\t\n   ") == ()

    def test_hda_codec_timeout_matched(self) -> None:
        payload = dedent(
            """\
            2026-04-24T10:00:01+0000 host kernel: snd_hda_intel 0000:00:1f.3: azx_get_response timeout (last cmd=0x204f0000)
            """
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_codec_timeout"
        assert events[0].severity == "error"
        assert events[0].kernel_timestamp_iso == "2026-04-24T10:00:01+0000"
        assert "azx_get_response timeout" in events[0].message_excerpt

    def test_hda_codec_disconnected_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "snd_hda_intel 0000:00:1f.3: codec_disconnected=1\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_codec_disconnected"
        assert events[0].severity == "error"

    def test_hda_no_pin_widget_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "snd_hda_codec_generic hdaudioC0D0: no pin widget found\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_no_pin_widget"

    def test_hda_irq_timing_workaround_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "snd_hda_intel 0000:00:1f.3: IRQ timing workaround is activated\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_irq_timing_workaround"
        assert events[0].severity == "warning"

    def test_usb_descriptor_read_fail_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "usb 1-1.2: device descriptor read/64, error -71\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "usb_descriptor_read_fail"
        assert events[0].severity == "error"

    def test_usb_disconnect_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: usb 1-1.2: USB disconnect, device number 4\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "usb_disconnect"
        assert events[0].severity == "warning"

    def test_alsa_xrun_matched(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: snd_pcm_lib: xrun!!! (at least 123.4 ms long)\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "alsa_xrun"

    def test_no_match_on_benign_line(self) -> None:
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "eth0: Link is Up - 1Gbps/Full - flow control rx/tx\n"
            "2026-04-24T10:00:02+0000 host kernel: "
            "BTRFS info (device sda1): relocating block group 123\n"
        )
        assert _parse_events(payload) == ()

    def test_mixed_events_preserve_order(self) -> None:
        payload = dedent(
            """\
            2026-04-24T10:00:01+0000 host kernel: snd_hda_intel 0000:00:1f.3: IRQ timing workaround activated
            2026-04-24T10:00:02+0000 host kernel: usb 1-1.2: USB disconnect, device number 4
            2026-04-24T10:00:03+0000 host kernel: snd_hda_intel 0000:00:1f.3: azx_get_response timeout
            """
        )
        events = _parse_events(payload)
        assert len(events) == 3
        names = [e.pattern_name for e in events]
        assert names == [
            "hda_irq_timing_workaround",
            "usb_disconnect",
            "hda_codec_timeout",
        ]

    def test_one_event_per_line_first_pattern_wins(self) -> None:
        """If a line could match two patterns, the catalog-order
        first wins — avoids double-count inflation."""
        # Craft a line that contains both an HDA signal and an
        # xrun text (artificial but tests the single-event rule).
        payload = (
            "2026-04-24T10:00:01+0000 host kernel: "
            "snd_hda_intel: azx_get_response timeout; xrun!!! (at least 5.0 ms long)\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_codec_timeout"

    def test_line_without_envelope_still_scans(self) -> None:
        """Some journalctl formats (rare) emit lines without the
        ``<ts> host kernel:`` envelope. The parser still tries
        distress patterns against the raw line."""
        payload = "snd_hda_intel: azx_get_response timeout on codec\n"
        events = _parse_events(payload)
        assert len(events) == 1
        assert events[0].pattern_name == "hda_codec_timeout"
        assert events[0].kernel_timestamp_iso == ""

    def test_max_events_cap_enforced(self) -> None:
        """Attacker-injected log floods are capped at _MAX_EVENTS."""
        from sovyx.voice.health._driver_watchdog_linux import _MAX_EVENTS

        line = "2026-04-24T10:00:01+0000 host kernel: snd_hda_intel: azx_get_response timeout"
        payload = "\n".join([line] * (_MAX_EVENTS + 50))
        events = _parse_events(payload)
        assert len(events) == _MAX_EVENTS

    def test_message_excerpt_truncated(self) -> None:
        from sovyx.voice.health._driver_watchdog_linux import _MESSAGE_TRUNCATE_CHARS

        long_suffix = "x" * (_MESSAGE_TRUNCATE_CHARS + 100)
        payload = (
            f"2026-04-24T10:00:01+0000 host kernel: "
            f"snd_hda_intel: azx_get_response timeout {long_suffix}\n"
        )
        events = _parse_events(payload)
        assert len(events) == 1
        assert len(events[0].message_excerpt) <= _MESSAGE_TRUNCATE_CHARS


class TestDeviceHintExtraction:
    """Verify _extract_device_hint finds the first available hint
    in each of three orthogonal shapes."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("snd_hda_intel: card0: azx_get_response timeout", "0"),
            ("ALSA card[PCH]: no pin widget", "[PCH]"),
            ("usb 1-1.2: device descriptor read/64, error -71", "1-1.2"),
            ("usb 1-1.2:1.0: USB disconnect", "1-1.2:1.0"),
            ("HDA codec Vendor Id: 0x14f15045 on card0", "0"),  # card wins (earlier)
            ("no device info here", None),
        ],
    )
    def test_extraction(self, message: str, expected: str | None) -> None:
        assert _extract_device_hint(message) == expected

    def test_hint_absent_when_pattern_not_found(self) -> None:
        assert _extract_device_hint("plain message") is None


# ── LinuxDriverWatchdogScan semantics ──────────────────────────────


def _mk_event(
    *,
    pattern_name: str = "hda_codec_timeout",
    device_hint: str | None = "card0",
) -> LinuxDriverWatchdogEvent:
    return LinuxDriverWatchdogEvent(
        kernel_timestamp_iso="2026-04-24T10:00:01+0000",
        pattern_name=pattern_name,
        severity="error",
        message_excerpt="synthetic",
        device_hint=device_hint,
    )


class TestLinuxDriverWatchdogScan:
    def test_any_events_false_when_empty(self) -> None:
        assert LinuxDriverWatchdogScan().any_events is False

    def test_any_events_true_when_populated(self) -> None:
        scan = LinuxDriverWatchdogScan(events=(_mk_event(),))
        assert scan.any_events is True

    def test_matches_device_needs_some_hint(self) -> None:
        scan = LinuxDriverWatchdogScan(events=(_mk_event(device_hint="card0"),))
        # All None → never matches.
        assert (
            scan.matches_device(alsa_card_id=None, usb_vid_pid=None, codec_vendor_id=None) is False
        )

    def test_matches_device_alsa_card_id(self) -> None:
        scan = LinuxDriverWatchdogScan(events=(_mk_event(device_hint="card0"),))
        assert scan.matches_device(alsa_card_id="0") is True
        assert scan.matches_device(alsa_card_id="1") is False

    def test_matches_device_usb_vid_pid(self) -> None:
        scan = LinuxDriverWatchdogScan(
            events=(_mk_event(device_hint="1-1.2:1.0"),),
        )
        assert scan.matches_device(usb_vid_pid="1-1.2") is True
        assert scan.matches_device(usb_vid_pid="2-1.0") is False

    def test_matches_device_case_insensitive(self) -> None:
        scan = LinuxDriverWatchdogScan(events=(_mk_event(device_hint="[PCH]"),))
        assert scan.matches_device(alsa_card_id="pch") is True
        assert scan.matches_device(alsa_card_id="PCH") is True

    def test_matches_device_empty_strings_ignored(self) -> None:
        scan = LinuxDriverWatchdogScan(events=(_mk_event(device_hint="card0"),))
        assert scan.matches_device(alsa_card_id="", usb_vid_pid="  ") is False

    def test_matches_device_any_event_matches(self) -> None:
        """If multiple events exist, one match suffices."""
        scan = LinuxDriverWatchdogScan(
            events=(
                _mk_event(device_hint="card0"),
                _mk_event(device_hint="1-1.2"),
            ),
        )
        assert scan.matches_device(usb_vid_pid="1-1.2") is True
        assert scan.matches_device(alsa_card_id="0") is True


# ── Async subprocess harness ───────────────────────────────────────


class _FakeProcess:
    """Minimal async-compatible stand-in for ``asyncio.subprocess.Process``.

    The real one is hard to build in-process; this class implements the
    subset the scanner touches (``returncode``, ``communicate()``,
    ``kill()``, ``wait()``).
    """

    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
        communicate_delay_s: float = 0.0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = communicate_delay_s
        self._killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self._killed = True

    async def wait(self) -> int:
        return self.returncode


class TestScanRecentEventsDegradation:
    """Verify each degradation path returns the right flag shape.

    All paths guarded by ``sys.platform`` patching so these tests
    pass on Windows dev hosts identically to Linux CI.
    """

    @pytest.mark.asyncio()
    async def test_non_linux_returns_scan_not_attempted(self) -> None:
        with patch.object(sys, "platform", "win32"):
            scan = await scan_recent_linux_driver_watchdog_events()
        assert scan.scan_attempted is False
        assert scan.scan_failed is False
        assert scan.events == ()

    @pytest.mark.asyncio()
    async def test_journalctl_not_found_returns_not_attempted(self) -> None:
        async def _raising(*_args: object, **_kw: object) -> _FakeProcess:
            raise FileNotFoundError("journalctl")

        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=_raising,
            ),
        ):
            scan = await scan_recent_linux_driver_watchdog_events()
        assert scan.scan_attempted is False

    @pytest.mark.asyncio()
    async def test_subprocess_timeout_flags_scan_failed(self) -> None:
        fake = _FakeProcess(stdout=b"", communicate_delay_s=5.0)

        async def _spawn(*_args: object, **_kw: object) -> _FakeProcess:
            return fake

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn),
        ):
            scan = await scan_recent_linux_driver_watchdog_events(
                timeout_s=0.05,
            )
        assert scan.scan_attempted is True
        assert scan.scan_failed is True
        assert scan.events == ()

    @pytest.mark.asyncio()
    async def test_nonzero_exit_flags_scan_failed(self) -> None:
        """journalctl exits nonzero on permission denied, no journal,
        etc. Scan marks as failed rather than attempting to parse
        potentially-truncated output."""
        fake = _FakeProcess(
            stdout=b"",
            stderr=b"journalctl: Failed to open journal (Permission denied)",
            returncode=1,
        )

        async def _spawn(*_args: object, **_kw: object) -> _FakeProcess:
            return fake

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn),
        ):
            scan = await scan_recent_linux_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is True

    @pytest.mark.asyncio()
    async def test_empty_stdout_scan_clean_no_events(self) -> None:
        fake = _FakeProcess(stdout=b"")

        async def _spawn(*_args: object, **_kw: object) -> _FakeProcess:
            return fake

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn),
        ):
            scan = await scan_recent_linux_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert scan.events == ()
        assert scan.any_events is False

    @pytest.mark.asyncio()
    async def test_happy_path_parses_events(self) -> None:
        payload = dedent(
            """\
            2026-04-24T10:00:01+0000 host kernel: snd_hda_intel 0000:00:1f.3: azx_get_response timeout
            2026-04-24T10:00:02+0000 host kernel: usb 1-1.2: USB disconnect, device number 4
            """
        ).encode("utf-8")
        fake = _FakeProcess(stdout=payload)

        async def _spawn(*_args: object, **_kw: object) -> _FakeProcess:
            return fake

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn),
        ):
            scan = await scan_recent_linux_driver_watchdog_events()
        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert len(scan.events) == 2
        assert scan.events[0].pattern_name == "hda_codec_timeout"
        assert scan.events[1].pattern_name == "usb_disconnect"


class TestScanArgvShape:
    """Regression: the argv built for ``journalctl`` must be stable
    so CI-Linux can match it across subprocess mocks."""

    @pytest.mark.asyncio()
    async def test_argv_matches_expected(self) -> None:
        captured: dict[str, Sequence[str]] = {}

        async def _spawn(*args: str, **_kw: object) -> _FakeProcess:
            captured["argv"] = args
            return _FakeProcess(stdout=b"")

        with (
            patch.object(sys, "platform", "linux"),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn),
        ):
            await scan_recent_linux_driver_watchdog_events(
                lookback_hours=12,
                journalctl_exe="/usr/bin/journalctl",
            )
        assert captured["argv"] == (
            "/usr/bin/journalctl",
            "-k",
            "--no-pager",
            "--since",
            "-12h",
            "--output",
            "short-iso",
        )

    @pytest.mark.asyncio()
    async def test_default_exe_is_bare_journalctl(self) -> None:
        """Default binary lookup goes through PATH — matches the
        Sprint 1A audio-service-monitor convention."""
        assert _JOURNALCTL_EXE == "journalctl"
