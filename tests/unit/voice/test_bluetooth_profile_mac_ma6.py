"""Tests for the MA6 macOS Bluetooth profile detector."""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice._bluetooth_profile_mac import (
    BluetoothAudioProfile,
    BluetoothReport,
    detect_bluetooth_audio_profile,
)

# Real-shape system_profiler output (sanitized) — used by the parse
# tests so we exercise the actual indented-tree parser, not a
# simplified fake.

_SP_OUTPUT_AIRPODS_HFP = """
Bluetooth:

    AirPods Pro:
        Address: AA:BB:CC:DD:EE:FF
        Connected: Yes
        Services: 0x0000001
        Audio Source: Yes
        Audio Sink: Yes
        Vendor ID: 0x004C

    Apple Mouse:
        Address: 11:22:33:44:55:66
        Connected: Yes
        Audio Source: No
        Audio Sink: No
"""

_SP_OUTPUT_AIRPODS_A2DP_ONLY = """
Bluetooth:

    AirPods Pro:
        Address: AA:BB:CC:DD:EE:FF
        Connected: Yes
        Services: 0x0000001
        Audio Source: No
        Audio Sink: Yes
        Vendor ID: 0x004C
"""

_SP_OUTPUT_DISCONNECTED = """
Bluetooth:

    AirPods Pro:
        Address: AA:BB:CC:DD:EE:FF
        Connected: No
        Audio Source: Yes
        Audio Sink: Yes
"""

_SP_OUTPUT_NO_DEVICES = """
Bluetooth:

"""


def _fake_run(stdout: str, *, returncode: int = 0) -> Any:
    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    return _factory


# ── Cross-platform branches ───────────────────────────────────────


class TestNonDarwinShortCircuit:
    def test_linux_returns_empty(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("non-darwin" in n for n in report.notes)

    def test_windows_returns_empty(self) -> None:
        with patch.object(sys, "platform", "win32"):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()


# ── Subprocess failure handling ───────────────────────────────────


class TestSubprocessFailures:
    def test_system_profiler_missing(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value=None),
        ):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("not found" in n for n in report.notes)

    def test_system_profiler_timeout(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sp", 8)),
        ):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("timed out" in n for n in report.notes)

    def test_system_profiler_nonzero_exit(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=_fake_run("", returncode=1),
            ),
        ):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("exited 1" in n for n in report.notes)

    def test_system_profiler_oserror(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=OSError("permission denied")),
        ):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("spawn failed" in n for n in report.notes)


# ── Parse logic (real-shape system_profiler output) ──────────────


class TestParseRealShapes:
    def test_airpods_with_both_audio_returns_hfp(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=_fake_run(_SP_OUTPUT_AIRPODS_HFP)),
        ):
            report = detect_bluetooth_audio_profile()
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.HFP_ACTIVE
        assert airpods.is_input_capable is True
        assert airpods.is_output_capable is True
        assert airpods.address == "AA:BB:CC:DD:EE:FF"

    def test_airpods_a2dp_only_classified_correctly(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(_SP_OUTPUT_AIRPODS_A2DP_ONLY),
            ),
        ):
            report = detect_bluetooth_audio_profile()
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.A2DP_ONLY
        assert airpods.is_input_capable is False
        assert airpods.is_output_capable is True

    def test_disconnected_returns_not_connected(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(_SP_OUTPUT_DISCONNECTED),
            ),
        ):
            report = detect_bluetooth_audio_profile()
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.NOT_CONNECTED

    def test_non_audio_devices_parsed_as_neither(self) -> None:
        # Apple Mouse → no audio source/sink → should still be in
        # devices list with profile UNKNOWN.
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=_fake_run(_SP_OUTPUT_AIRPODS_HFP)),
        ):
            report = detect_bluetooth_audio_profile()
        mouse = next(d for d in report.devices if d.name == "Apple Mouse")
        assert mouse.is_input_capable is False
        assert mouse.is_output_capable is False
        assert mouse.profile is BluetoothAudioProfile.UNKNOWN

    def test_no_devices_in_output_returns_empty_with_note(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(_SP_OUTPUT_NO_DEVICES),
            ),
        ):
            report = detect_bluetooth_audio_profile()
        assert report.devices == ()
        assert any("no audio-capable" in n for n in report.notes)


# ── A2DP-only filter (load-bearing predicate) ─────────────────────


class TestA2DPOnlyDevicesFilter:
    def test_filter_returns_only_a2dp_input_capable(self) -> None:
        # Mixed scenario: A2DP-only headset (the failure case) +
        # speaker-only (output capable but not input capable). Only
        # the headset matters for the dashboard's mic remediation.
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(_SP_OUTPUT_AIRPODS_A2DP_ONLY),
            ),
        ):
            report = detect_bluetooth_audio_profile()
        # AirPods are in A2DP-only; their is_input_capable is FALSE
        # (we only see Audio Sink: Yes). So the filter returns nothing
        # in this case — the predicate is correctly conservative.
        assert report.a2dp_only_devices == ()

    def test_empty_report_filter_returns_empty(self) -> None:
        report = BluetoothReport()
        assert report.a2dp_only_devices == ()


# ── Report contract ──────────────────────────────────────────────


class TestReportContract:
    def test_profile_enum_values_stable(self) -> None:
        assert BluetoothAudioProfile.A2DP_ONLY.value == "a2dp_only"
        assert BluetoothAudioProfile.HFP_ACTIVE.value == "hfp_active"
        assert BluetoothAudioProfile.UNKNOWN.value == "unknown"
        assert BluetoothAudioProfile.NOT_CONNECTED.value == "not_connected"

    def test_default_report_safe(self) -> None:
        report = BluetoothReport()
        assert report.devices == ()
        assert report.notes == ()


pytestmark = pytest.mark.timeout(15)
