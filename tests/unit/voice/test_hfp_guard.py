"""Unit tests for :mod:`sovyx.voice._hfp_guard`.

Covers the macOS ``system_profiler`` dispatcher: platform gating,
payload walking under multiple macOS JSON shapes, and classification
via each of the three signal strengths (service-record / minor-type /
name-hint).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice._hfp_guard import (
    HfpDevice,
    HfpGuardReport,
    guard_active_capture_endpoint,
)


class _FakeCompleted:
    """Duck-typed :class:`subprocess.CompletedProcess` for ``run`` mocks."""

    def __init__(self, stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _sp_payload(*devices: dict[str, Any], shape: str = "device_title") -> str:
    """Render a fake ``system_profiler SPBluetoothDataType -json`` body.

    ``shape="device_title"`` mirrors macOS 12+; ``"connected_split"``
    mirrors early Sonoma where devices live under ``device_connected``.
    """
    if shape == "connected_split":
        section = {
            "device_connected": [
                {dev.pop("_key", f"Device {i}"): dev} for i, dev in enumerate(devices)
            ],
        }
    else:
        section = {
            "device_title": {dev.pop("_key", f"Device {i}"): dev for i, dev in enumerate(devices)},
        }
    return json.dumps({"SPBluetoothDataType": [section]})


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


class TestPlatformGate:
    def test_non_darwin_returns_tool_available_empty_report(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = guard_active_capture_endpoint()
        assert report.connected_hfp == []
        assert report.any_connected_bluetooth is False
        # On non-macOS we claim "tool_available=True" so callers can
        # treat the report as authoritative ("no HFP here").
        assert report.tool_available is True

    def test_win32_returns_empty(self) -> None:
        with patch.object(sys, "platform", "win32"):
            report = guard_active_capture_endpoint()
        assert report.connected_hfp == []
        assert report.tool_available is True


# ---------------------------------------------------------------------------
# system_profiler missing / failing
# ---------------------------------------------------------------------------


class TestToolMissing:
    def test_missing_binary_marks_tool_unavailable(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value=None),
        ):
            report = guard_active_capture_endpoint()
        assert report.tool_available is False
        assert report.connected_hfp == []

    def test_timeout_marks_tool_unavailable(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd="system_profiler",
                    timeout=2.5,
                ),
            ),
        ):
            report = guard_active_capture_endpoint()
        assert report.tool_available is False

    def test_non_zero_exit_marks_tool_unavailable(self) -> None:
        proc = _FakeCompleted("", returncode=2, stderr="boom")
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert report.tool_available is False

    def test_malformed_json_marks_tool_unavailable(self) -> None:
        proc = _FakeCompleted("{not json")
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert report.tool_available is False


# ---------------------------------------------------------------------------
# Classification — service-record signal (strongest)
# ---------------------------------------------------------------------------


class TestServiceSignal:
    def test_hfp_service_flags_device(self) -> None:
        device = {
            "_key": "AirPods Pro",
            "device_address": "AA:BB:CC:DD:EE:FF",
            "device_minorType": "Headphones",
            "device_isconnected": "attrib_Yes",
            "device_services": ["HFP", "A2DP"],
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert report.tool_available is True
        assert report.any_connected_bluetooth is True
        assert len(report.connected_hfp) == 1
        hit = report.connected_hfp[0]
        assert hit.name == "AirPods Pro"
        assert hit.address == "AA:BB:CC:DD:EE:FF"
        assert hit.reason == "services"

    def test_service_case_insensitive_and_punctuated(self) -> None:
        device = {
            "_key": "Sony WH-1000XM5",
            "device_isconnected": "attrib_Yes",
            "device_services": ["Hands-Free", "A2DP Sink"],
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        # "Hands-Free" should normalise to "handsfree" which matches neither
        # our service-name set exactly — the fallback path (name_hint) kicks
        # in because "sony wh" is in _HFP_NAME_HINTS.
        hit = report.connected_hfp[0]
        assert hit.reason == "name_hint"


# ---------------------------------------------------------------------------
# Classification — minor-type signal
# ---------------------------------------------------------------------------


class TestMinorTypeSignal:
    def test_minor_type_flags_device(self) -> None:
        device = {
            "_key": "Generic BT Headset",
            "device_minorType": "Handsfree",
            "device_isconnected": "attrib_Yes",
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        hit = report.connected_hfp[0]
        assert hit.reason == "minor_type"
        assert hit.minor_type == "Handsfree"

    def test_minor_type_headphones_flagged(self) -> None:
        device = {
            "_key": "Bose QC45",
            "device_minorType": "Headphones",
            "device_isconnected": "attrib_Yes",
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        # Bose matches BOTH name-hint AND minor-type; minor-type ranks
        # higher in the classification ladder so that's what we report.
        hit = report.connected_hfp[0]
        assert hit.reason == "minor_type"


# ---------------------------------------------------------------------------
# Classification — name-hint fallback
# ---------------------------------------------------------------------------


class TestNameHintSignal:
    def test_name_hint_matches_airpods(self) -> None:
        device = {
            "_key": "AirPods Pro",
            "device_isconnected": "attrib_Yes",
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        hit = report.connected_hfp[0]
        assert hit.reason == "name_hint"

    def test_disconnected_device_not_flagged(self) -> None:
        device = {
            "_key": "AirPods Pro",
            "device_isconnected": "attrib_No",
            "device_services": ["HFP"],
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert report.connected_hfp == []
        assert report.any_connected_bluetooth is False

    def test_unrelated_connected_device_is_counted_not_flagged(self) -> None:
        device = {
            "_key": "Magic Mouse",
            "device_minorType": "Mouse",
            "device_isconnected": "attrib_Yes",
        }
        proc = _FakeCompleted(_sp_payload(device))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert report.connected_hfp == []
        # any_connected_bluetooth remains True — a mouse is connected.
        assert report.any_connected_bluetooth is True


# ---------------------------------------------------------------------------
# Alternate macOS JSON shape
# ---------------------------------------------------------------------------


class TestAlternateShape:
    def test_connected_split_shape_parsed(self) -> None:
        device = {
            "_key": "AirPods Max",
            "device_minorType": "Headphones",
            "device_isconnected": "attrib_Yes",
        }
        proc = _FakeCompleted(_sp_payload(device, shape="connected_split"))
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", return_value=proc),
        ):
            report = guard_active_capture_endpoint()
        assert len(report.connected_hfp) == 1
        assert report.connected_hfp[0].name == "AirPods Max"


# ---------------------------------------------------------------------------
# Data-class surface
# ---------------------------------------------------------------------------


class TestDataClasses:
    @pytest.mark.parametrize(
        ("reason",),
        [("services",), ("minor_type",), ("name_hint",)],
    )
    def test_hfp_device_reason_roundtrip(self, reason: str) -> None:
        device = HfpDevice(name="X", reason=reason)
        assert device.reason == reason

    def test_report_defaults(self) -> None:
        report = HfpGuardReport()
        assert report.connected_hfp == []
        assert report.any_connected_bluetooth is False
        assert report.tool_available is True
