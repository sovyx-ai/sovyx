"""Unit tests for ``sovyx doctor platform`` — cross-OS diagnostics CLI.

Mirror surface to ``GET /api/voice/platform-diagnostics``: same
detector dispatch, same per-OS branches, same probe-failure-isolation
contract — but driven from a typer CLI that operators can run without
a live daemon. Coverage:

* Exit 0 always (this is a diagnostic, never a gate).
* JSON output payload shape (``platform``, ``mic_permission``,
  per-OS branch keys).
* Per-OS branch dispatch on linux / win32 / darwin.
* Unknown platform → ``platform="other"`` with mic_permission still
  attempted.
* Probe-failure isolation — a synthetic exception inside one detector
  does NOT take out the rest.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.main import app

runner = CliRunner()


# ── Top-level smoke ───────────────────────────────────────────────


class TestDoctorPlatformSmoke:
    def test_command_exits_zero(self) -> None:
        result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0

    def test_json_output_is_valid_json(self) -> None:
        result = runner.invoke(app, ["doctor", "platform", "--json"])
        body = json.loads(result.stdout)
        assert isinstance(body, dict)

    def test_json_output_contains_platform_key(self) -> None:
        result = runner.invoke(app, ["doctor", "platform", "--json"])
        body = json.loads(result.stdout)
        assert "platform" in body
        assert "mic_permission" in body


# ── Per-OS branch dispatch ────────────────────────────────────────


class TestPerOsBranch:
    def test_linux_branch_populated_on_linux(self) -> None:
        with patch.object(sys, "platform", "linux"):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["platform"] == "linux"
        assert "linux" in body
        assert "pipewire" in body["linux"]
        assert "alsa_ucm" in body["linux"]

    def test_windows_branch_populated_on_win32(self) -> None:
        with patch.object(sys, "platform", "win32"):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["platform"] == "win32"
        assert "windows" in body
        assert "audio_service" in body["windows"]
        assert "etw_audio_events" in body["windows"]
        assert isinstance(body["windows"]["etw_audio_events"], list)

    def test_darwin_branch_populated_on_darwin(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["platform"] == "darwin"
        assert "macos" in body
        assert "hal_plugins" in body["macos"]
        assert "bluetooth" in body["macos"]
        assert "code_signing" in body["macos"]


# ── Unknown platform fallback ─────────────────────────────────────


class TestUnknownPlatform:
    def test_freebsd_returns_other_with_mic_permission(self) -> None:
        with patch.object(sys, "platform", "freebsd"):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["platform"] == "other"
        # mic_permission still attempted.
        assert "mic_permission" in body
        assert body["mic_permission"]["status"] == "unknown"
        # No per-OS branch populated.
        assert "linux" not in body
        assert "windows" not in body
        assert "macos" not in body


# ── Probe failure isolation ───────────────────────────────────────


class TestProbeFailureIsolation:
    def test_pipewire_crash_does_not_take_out_alsa_ucm(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice.health._pipewire.detect_pipewire",
                side_effect=RuntimeError("pipewire boom"),
            ),
        ):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        # PipeWire collapsed to unknown, alsa_ucm still ran.
        assert body["linux"]["pipewire"]["status"] == "unknown"
        assert "alsa_ucm" in body["linux"]
        assert body["linux"]["alsa_ucm"] is not None

    def test_hal_crash_does_not_take_out_bluetooth_or_codesign(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac.detect_hal_plugins",
                side_effect=RuntimeError("hal boom"),
            ),
        ):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        # HAL collapsed to unknown sentinel.
        assert body["macos"]["hal_plugins"]["status"] == "unknown"
        # Bluetooth + code_signing still populated.
        assert "devices" in body["macos"]["bluetooth"]
        assert "verdict" in body["macos"]["code_signing"]

    def test_etw_crash_does_not_take_out_audio_service(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._windows_etw.query_audio_etw_events",
                side_effect=RuntimeError("etw boom"),
            ),
        ):
            result = runner.invoke(app, ["doctor", "platform", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert "audiosrv" in body["windows"]["audio_service"]
        # ETW collapsed to empty list.
        assert body["windows"]["etw_audio_events"] == []


# ── Default (non-JSON) rendering ──────────────────────────────────


class TestRichTableRender:
    def test_default_output_includes_header(self) -> None:
        result = runner.invoke(app, ["doctor", "platform"])
        assert result.exit_code == 0
        # The header line is always present.
        assert "Sovyx Platform Diagnostics" in result.stdout
        assert "platform=" in result.stdout

    def test_default_output_renders_mic_permission_label(self) -> None:
        result = runner.invoke(app, ["doctor", "platform"])
        assert result.exit_code == 0
        assert "microphone permission" in result.stdout


# ── Mic permission OS-aware ──────────────────────────────────────


class TestMicPermissionPayload:
    def test_status_serialised_as_string(self) -> None:
        result = runner.invoke(app, ["doctor", "platform", "--json"])
        body = json.loads(result.stdout)
        assert isinstance(body["mic_permission"]["status"], str)
        assert body["mic_permission"]["status"] in (
            "granted",
            "denied",
            "unknown",
        )


# ── Helper coverage ──────────────────────────────────────────────


class TestSerialiseHelpers:
    def test_serialise_dataclass_or_unknown_handles_none(self) -> None:
        from sovyx.cli.commands.doctor import _serialise_dataclass_or_unknown

        out: Any = _serialise_dataclass_or_unknown(None)
        assert out == {"status": "unknown", "notes": ["probe returned None"]}

    def test_coerce_to_jsonable_unwraps_strenum(self) -> None:
        from enum import StrEnum

        from sovyx.cli.commands.doctor import _coerce_to_jsonable

        class _S(StrEnum):
            X = "x_value"

        out = _coerce_to_jsonable({"k": _S.X})
        assert out == {"k": "x_value"}

    def test_coerce_to_jsonable_recurses_into_lists(self) -> None:
        from sovyx.cli.commands.doctor import _coerce_to_jsonable

        out = _coerce_to_jsonable([{"a": 1}, {"b": 2}])
        assert out == [{"a": 1}, {"b": 2}]


pytestmark = pytest.mark.timeout(30)
