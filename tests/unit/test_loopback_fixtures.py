"""Tests for :mod:`tests._loopback_fixtures` (TS2)."""

from __future__ import annotations

import subprocess
import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._loopback_fixtures import (
    LoopbackKind,
    LoopbackReport,
    detect_loopback,
    loopback_device_name,
    requires_loopback,
)

# ── Cross-platform branches ────────────────────────────────────────


class TestUnsupportedPlatforms:
    def test_freebsd_returns_none(self) -> None:
        with patch.object(sys, "platform", "freebsd"):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert r.available is False
        assert any("unsupported platform" in n for n in r.notes)


# ── Linux: snd_aloop ───────────────────────────────────────────────


class TestSndAloopDetection:
    def test_module_loaded_returns_snd_aloop(self, tmp_path: Any) -> None:
        # Stub /proc/modules + /proc/asound/cards via Path patching.
        modules_path = MagicMock()
        modules_path.exists.return_value = True
        modules_path.read_text.return_value = "snd_aloop 12345 0\nsnd_pcm 67890 1\n"
        cards_path = MagicMock()
        cards_path.exists.return_value = True
        cards_path.read_text.return_value = (
            " 0 [PCH            ]: HDA-Intel - HDA Intel\n"
            " 1 [Loopback       ]: Loopback - Loopback\n"
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "tests._loopback_fixtures.Path",
                side_effect=lambda p: modules_path if "modules" in p else cards_path,
            ),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.SND_ALOOP
        assert r.available is True
        assert r.device_name == "hw:1,1"

    def test_module_not_loaded_returns_none(self) -> None:
        modules_path = MagicMock()
        modules_path.exists.return_value = True
        modules_path.read_text.return_value = "snd_pcm 12345 0\n"
        with (
            patch.object(sys, "platform", "linux"),
            patch("tests._loopback_fixtures.Path", return_value=modules_path),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("not loaded" in n for n in r.notes)

    def test_proc_modules_missing_returns_none(self) -> None:
        modules_path = MagicMock()
        modules_path.exists.return_value = False
        with (
            patch.object(sys, "platform", "linux"),
            patch("tests._loopback_fixtures.Path", return_value=modules_path),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("not found" in n for n in r.notes)


# ── macOS: BlackHole ───────────────────────────────────────────────


class TestBlackHoleDetection:
    def test_blackhole_present_returns_blackhole(self) -> None:
        sp_output = (
            "Audio:\n  Devices:\n    BlackHole 2ch:\n      Manufacturer: Existential Audio Inc.\n"
        )
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=sp_output, stderr=""),
            ),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.BLACKHOLE
        assert r.available is True
        assert "BlackHole" in r.device_name

    def test_no_blackhole_returns_none(self) -> None:
        sp_output = (
            "Audio:\n  Devices:\n    Built-in Microphone:\n      Manufacturer: Apple Inc.\n"
        )
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=sp_output, stderr=""),
            ),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("not present" in n for n in r.notes)

    def test_system_profiler_missing_returns_none(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value=None),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("not found" in n for n in r.notes)

    def test_system_profiler_timeout_returns_none(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/sbin/system_profiler"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sp", 5)),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("timed out" in n for n in r.notes)


# ── Windows: VB-CABLE ──────────────────────────────────────────────


def _fake_winreg(
    *,
    capture_root_open_error: type[BaseException] | None = None,
    endpoints: list[tuple[str, str]] | None = None,
) -> ModuleType:
    """Build a minimal winreg fake with the capture-root + endpoint
    Properties subkeys keyed by friendly name."""
    module = ModuleType("winreg")
    module.HKEY_LOCAL_MACHINE = 0x80000002  # type: ignore[attr-defined]
    if endpoints is None:
        endpoints = [
            ("{ENDPOINT-1}", "CABLE Output (VB-Audio Virtual Cable)"),
        ]

    capture_root_key = MagicMock(name="capture_root")

    def open_key(parent: Any, subkey: str) -> Any:
        if isinstance(parent, int):
            if capture_root_open_error is not None:
                raise capture_root_open_error("simulated open failure")
            return capture_root_key
        # Endpoint or Properties subkey lookup — flatten via path.
        return MagicMock(name=f"sub_{subkey}")

    def enum_key(_root: Any, idx: int) -> str:
        if idx >= len(endpoints):
            raise OSError("end of enumeration")
        return endpoints[idx][0]

    def query_value_ex(_key: Any, name: str) -> tuple[Any, int]:
        # Each Properties subkey holds the friendly name keyed by
        # the canonical PKEY GUID; we look it up in the endpoints
        # by inspecting the key's name (set above).
        for ep_id, friendly in endpoints:
            if ep_id in str(_key):
                return friendly, 1
        # If we can't disambiguate, return the first endpoint's name.
        return endpoints[0][1], 1

    def close_key(_key: Any) -> None:
        return None

    module.OpenKey = open_key  # type: ignore[attr-defined]
    module.EnumKey = enum_key  # type: ignore[attr-defined]
    module.QueryValueEx = query_value_ex  # type: ignore[attr-defined]
    module.CloseKey = close_key  # type: ignore[attr-defined]
    return module


class TestVbCableDetection:
    def test_vb_cable_present_returns_vb_cable(self) -> None:
        fake = _fake_winreg(
            endpoints=[("{ENDPOINT-1}", "CABLE Output (VB-Audio Virtual Cable)")],
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.VB_CABLE
        assert r.available is True
        assert "CABLE Output" in r.device_name

    def test_no_vb_cable_returns_none(self) -> None:
        fake = _fake_winreg(
            endpoints=[("{ENDPOINT-1}", "Realtek HD Audio Microphone")],
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("not found" in n for n in r.notes)

    def test_capture_root_open_failure_returns_none(self) -> None:
        fake = _fake_winreg(capture_root_open_error=PermissionError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            r = detect_loopback()
        assert r.kind is LoopbackKind.NONE
        assert any("capture root open failed" in n for n in r.notes)


# ── Public helpers ─────────────────────────────────────────────────


class TestPublicHelpers:
    def test_loopback_device_name_returns_empty_when_none(self) -> None:
        with patch.object(sys, "platform", "freebsd"):
            assert loopback_device_name() == ""

    def test_requires_loopback_skips_when_unavailable(self) -> None:
        # Patch detection to return NONE so the decorator wraps
        # with skip — accessed by inspecting the resulting marker.
        unavailable = LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=("test stub",),
        )
        with patch("tests._loopback_fixtures.detect_loopback", return_value=unavailable):

            @requires_loopback
            def _f() -> None:
                pass  # pragma: no cover — should be skipped

            # pytest.mark.skip wraps the function with a pytestmark
            # attribute; verify it's set.
            assert hasattr(_f, "pytestmark")
            marks = _f.pytestmark  # type: ignore[attr-defined]
            assert any(m.name == "skip" for m in marks)

    def test_requires_loopback_passes_through_when_available(self) -> None:
        available = LoopbackReport(
            kind=LoopbackKind.SND_ALOOP,
            available=True,
            device_name="hw:Loopback,1",
        )
        with patch("tests._loopback_fixtures.detect_loopback", return_value=available):

            @requires_loopback
            def _f() -> None:
                pass

            # No skip mark applied.
            assert not hasattr(_f, "pytestmark")


# ── Report contract ────────────────────────────────────────────────


class TestReportContract:
    def test_kind_enum_values_stable(self) -> None:
        assert LoopbackKind.SND_ALOOP.value == "snd_aloop"
        assert LoopbackKind.BLACKHOLE.value == "blackhole"
        assert LoopbackKind.VB_CABLE.value == "vb_cable"
        assert LoopbackKind.NONE.value == "none"

    def test_default_report_safe(self) -> None:
        r = LoopbackReport(kind=LoopbackKind.NONE, available=False)
        assert r.device_name == ""
        assert r.notes == ()


pytestmark = pytest.mark.timeout(10)
