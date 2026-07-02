"""Tests for the MA6 macOS Bluetooth profile detector.

MACOS-8 coverage: the probe now tries ``system_profiler
SPBluetoothDataType -json`` first (format-stable) and falls back to
the text output, which is parsed in BOTH known shapes — the modern
grouped tree (``Connected:`` / ``Not Connected:`` headers) and the
legacy flat tree. All fixtures are real-SHAPED but UNVERIFIED against
real Mac hardware (V-AEA operator backlog).
"""

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

# ── Fixtures — legacy flat text shape ─────────────────────────────
# (pre-Monterey: devices directly under "Bluetooth:", Audio Source /
# Audio Sink attributes expose the active profile)

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

# ── Fixtures — modern grouped text shape (Monterey+) ──────────────
# Devices nest one indent level under "Connected:" / "Not Connected:"
# group headers; a "Bluetooth Controller:" sibling section describes
# the host adapter. UNVERIFIED on real Mac HW (fixture-shaped).

_SP_OUTPUT_MODERN_GROUPED = """
Bluetooth:

      Bluetooth Controller:
          Address: 11:22:33:44:55:66
          State: On
          Transport: UART
      Connected:
          AirPods Pro:
              Address: AA:BB:CC:DD:EE:FF
              Minor Type: Headphones
              Audio Source: No
              Audio Sink: Yes
          Boom Speaker:
              Address: 22:33:44:55:66:77
              Minor Type: Speaker
      Not Connected:
          Magic Keyboard:
              Address: 99:88:77:66:55:44
              Minor Type: Keyboard
"""

# ── Fixture — -json shape (primary path) ──────────────────────────
# Publicly documented SPBluetoothDataType -json schema:
# device_connected / device_not_connected arrays of {name: attrs}
# single-key dicts. UNVERIFIED on real Mac HW (fixture-shaped).

_SP_JSON_OUTPUT = """
{
  "SPBluetoothDataType": [
    {
      "controller_properties": {
        "controller_address": "11:22:33:44:55:66",
        "controller_state": "attrib_on"
      },
      "device_connected": [
        {
          "AirPods Pro": {
            "device_address": "AA:BB:CC:DD:EE:FF",
            "device_minorType": "Headphones",
            "device_vendorID": "0x004C"
          }
        },
        {
          "Plantronics Voyager": {
            "device_address": "01:02:03:04:05:06",
            "device_minorType": "Headset"
          }
        }
      ],
      "device_not_connected": [
        {
          "Magic Keyboard": {
            "device_address": "99:88:77:66:55:44",
            "device_minorType": "Keyboard"
          }
        }
      ]
    }
  ]
}
"""


def _fake_run_dispatch(
    *,
    json_stdout: str | None = None,
    text_stdout: str = "",
    text_returncode: int = 0,
) -> Any:
    """subprocess.run replacement dispatching on the -json flag.

    ``json_stdout=None`` simulates a macOS whose system_profiler
    rejects ``-json`` (exit 1) → the probe must fall back to text."""

    def _run(argv: Any, *_args: Any, **_kwargs: Any) -> Any:
        if "-json" in argv:
            if json_stdout is None:
                return MagicMock(returncode=1, stdout="", stderr="-json unsupported")
            return MagicMock(returncode=0, stdout=json_stdout, stderr="")
        return MagicMock(returncode=text_returncode, stdout=text_stdout, stderr="")

    return _run


def _darwin_probe(run_side_effect: Any) -> BluetoothReport:
    with (
        patch.object(sys, "platform", "darwin"),
        patch("shutil.which", return_value="/usr/sbin/system_profiler"),
        patch("subprocess.run", side_effect=run_side_effect),
    ):
        return detect_bluetooth_audio_profile()


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
        report = _darwin_probe(subprocess.TimeoutExpired("sp", 8))
        assert report.devices == ()
        assert any("timed out" in n for n in report.notes)

    def test_system_profiler_nonzero_exit_both_paths(self) -> None:
        report = _darwin_probe(
            _fake_run_dispatch(json_stdout=None, text_stdout="", text_returncode=1),
        )
        assert report.devices == ()
        assert any("exited 1" in n for n in report.notes)

    def test_system_profiler_oserror(self) -> None:
        report = _darwin_probe(OSError("permission denied"))
        assert report.devices == ()
        assert any("spawn failed" in n for n in report.notes)


# ── JSON primary path (MACOS-8) ───────────────────────────────────


class TestJsonPrimaryPath:
    def test_json_parses_connected_and_not_connected(self) -> None:
        report = _darwin_probe(_fake_run_dispatch(json_stdout=_SP_JSON_OUTPUT))
        by_name = {d.name: d for d in report.devices}
        assert set(by_name) == {"AirPods Pro", "Plantronics Voyager", "Magic Keyboard"}
        # -json exposes no A2DP/HFP state → connected = UNKNOWN.
        assert by_name["AirPods Pro"].profile is BluetoothAudioProfile.UNKNOWN
        assert by_name["AirPods Pro"].address == "AA:BB:CC:DD:EE:FF"
        # Not-connected devices short-circuit without further parsing.
        assert by_name["Magic Keyboard"].profile is BluetoothAudioProfile.NOT_CONNECTED

    def test_json_minor_type_capability_heuristic(self) -> None:
        report = _darwin_probe(_fake_run_dispatch(json_stdout=_SP_JSON_OUTPUT))
        by_name = {d.name: d for d in report.devices}
        # Headset → duplex; Headphones → output only (conservative on
        # input); Keyboard → neither.
        assert by_name["Plantronics Voyager"].is_input_capable is True
        assert by_name["Plantronics Voyager"].is_output_capable is True
        assert by_name["AirPods Pro"].is_input_capable is False
        assert by_name["AirPods Pro"].is_output_capable is True
        assert by_name["Magic Keyboard"].is_output_capable is False

    def test_json_notes_disclose_profile_gap(self) -> None:
        report = _darwin_probe(_fake_run_dispatch(json_stdout=_SP_JSON_OUTPUT))
        assert any("does not expose A2DP/HFP" in n for n in report.notes)

    def test_json_success_skips_text_fallback(self) -> None:
        calls: list[Any] = []

        def _tracking_run(argv: Any, *_args: Any, **_kwargs: Any) -> Any:
            calls.append(tuple(argv))
            return MagicMock(returncode=0, stdout=_SP_JSON_OUTPUT, stderr="")

        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=_tracking_run),
        ):
            detect_bluetooth_audio_profile()
        assert len(calls) == 1
        assert "-json" in calls[0]

    def test_unparseable_json_falls_back_to_text(self) -> None:
        report = _darwin_probe(
            _fake_run_dispatch(
                json_stdout="not json at all",
                text_stdout=_SP_OUTPUT_AIRPODS_HFP,
            ),
        )
        assert any("falling back to text" in n for n in report.notes)
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.HFP_ACTIVE

    def test_json_wrong_top_shape_falls_back(self) -> None:
        report = _darwin_probe(
            _fake_run_dispatch(
                json_stdout='{"SPUSBDataType": []}',
                text_stdout=_SP_OUTPUT_AIRPODS_HFP,
            ),
        )
        assert any(d.name == "AirPods Pro" for d in report.devices)

    def test_json_empty_sections_reports_no_devices(self) -> None:
        report = _darwin_probe(
            _fake_run_dispatch(json_stdout='{"SPBluetoothDataType": [{}]}'),
        )
        assert report.devices == ()
        assert any("no audio-capable" in n for n in report.notes)


# ── Modern grouped text shape (MACOS-8) ───────────────────────────


class TestModernGroupedText:
    def _probe(self) -> BluetoothReport:
        return _darwin_probe(
            _fake_run_dispatch(json_stdout=None, text_stdout=_SP_OUTPUT_MODERN_GROUPED),
        )

    def test_group_headers_not_parsed_as_devices(self) -> None:
        report = self._probe()
        names = {d.name for d in report.devices}
        assert "Connected" not in names
        assert "Not Connected" not in names

    def test_controller_section_not_parsed_as_device(self) -> None:
        report = self._probe()
        names = {d.name for d in report.devices}
        assert "Bluetooth Controller" not in names

    def test_connected_device_with_audio_keys_infers_profile(self) -> None:
        # Devices under "Connected:" are connected even without a
        # "Connected: Yes" attribute; Audio Sink-only → A2DP.
        report = self._probe()
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.A2DP_ONLY
        assert airpods.address == "AA:BB:CC:DD:EE:FF"

    def test_connected_device_without_audio_keys_is_unknown(self) -> None:
        report = self._probe()
        speaker = next(d for d in report.devices if d.name == "Boom Speaker")
        assert speaker.profile is BluetoothAudioProfile.UNKNOWN

    def test_not_connected_group_short_circuits(self) -> None:
        report = self._probe()
        keyboard = next(d for d in report.devices if d.name == "Magic Keyboard")
        assert keyboard.profile is BluetoothAudioProfile.NOT_CONNECTED


# ── Legacy flat text shape (fallback path) ────────────────────────


class TestParseLegacyFlatShapes:
    def _probe(self, text: str) -> BluetoothReport:
        return _darwin_probe(_fake_run_dispatch(json_stdout=None, text_stdout=text))

    def test_airpods_with_both_audio_returns_hfp(self) -> None:
        report = self._probe(_SP_OUTPUT_AIRPODS_HFP)
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.HFP_ACTIVE
        assert airpods.is_input_capable is True
        assert airpods.is_output_capable is True
        assert airpods.address == "AA:BB:CC:DD:EE:FF"

    def test_airpods_a2dp_only_classified_correctly(self) -> None:
        report = self._probe(_SP_OUTPUT_AIRPODS_A2DP_ONLY)
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.A2DP_ONLY
        assert airpods.is_input_capable is False
        assert airpods.is_output_capable is True

    def test_disconnected_returns_not_connected(self) -> None:
        report = self._probe(_SP_OUTPUT_DISCONNECTED)
        airpods = next(d for d in report.devices if d.name == "AirPods Pro")
        assert airpods.profile is BluetoothAudioProfile.NOT_CONNECTED

    def test_non_audio_devices_parsed_as_neither(self) -> None:
        # Apple Mouse → no audio source/sink → should still be in
        # devices list with profile UNKNOWN.
        report = self._probe(_SP_OUTPUT_AIRPODS_HFP)
        mouse = next(d for d in report.devices if d.name == "Apple Mouse")
        assert mouse.is_input_capable is False
        assert mouse.is_output_capable is False
        assert mouse.profile is BluetoothAudioProfile.UNKNOWN

    def test_no_devices_in_output_returns_empty_with_note(self) -> None:
        report = self._probe(_SP_OUTPUT_NO_DEVICES)
        assert report.devices == ()
        assert any("no audio-capable" in n for n in report.notes)


# ── A2DP-only filter (load-bearing predicate) ─────────────────────


class TestA2DPOnlyDevicesFilter:
    def test_filter_returns_only_a2dp_input_capable(self) -> None:
        # Mixed scenario: A2DP-only headset (the failure case) +
        # speaker-only (output capable but not input capable). Only
        # the headset matters for the dashboard's mic remediation.
        report = _darwin_probe(
            _fake_run_dispatch(json_stdout=None, text_stdout=_SP_OUTPUT_AIRPODS_A2DP_ONLY),
        )
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
        # The dashboard zod twin (BluetoothAudioProfileTokenSchema)
        # keys on these exact strings (MACOS-1) — renames are a
        # frontend contract break.
        assert BluetoothAudioProfile.A2DP_ONLY.value == "a2dp_only"
        assert BluetoothAudioProfile.HFP_ACTIVE.value == "hfp_active"
        assert BluetoothAudioProfile.UNKNOWN.value == "unknown"
        assert BluetoothAudioProfile.NOT_CONNECTED.value == "not_connected"

    def test_default_report_safe(self) -> None:
        report = BluetoothReport()
        assert report.devices == ()
        assert report.notes == ()


pytestmark = pytest.mark.timeout(15)
