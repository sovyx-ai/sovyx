"""Tests for Linux sandbox detection [Phase 5 T5.45].

Coverage:

* Non-Linux returns ``platform_supported=False`` snapshot.
* No sandbox env vars → ``kind=NONE`` with ``platform_supported=True``.
* ``FLATPAK_ID`` set → ``kind=FLATPAK`` + flatpak_id field.
* ``SNAP`` set → ``kind=SNAP``; reads ``SNAP_INSTANCE_NAME``
  preferring it over ``SNAP_NAME``.
* ``APPIMAGE`` set → ``kind=APPIMAGE`` + appimage_path.
* Flatpak wins over AppImage when both env vars present.
* ``log_sandbox_snapshot`` emits structured INFO with the right
  remediation hint for each kind.
"""

from __future__ import annotations

import logging
import sys

import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures

from sovyx.voice.health import _sandbox_detector as sd

_LOGGER = "sovyx.voice.health._sandbox_detector"


class TestNonLinux:
    def test_windows_returns_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.platform_supported is False
        assert snapshot.kind == sd.LinuxSandboxKind.NONE

    def test_macos_returns_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.platform_supported is False


class TestLinuxNoSandbox:
    def test_native_install_returns_none_kind(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        for var in ("FLATPAK_ID", "SNAP", "SNAP_NAME", "APPIMAGE"):
            monkeypatch.delenv(var, raising=False)
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.platform_supported is True
        assert snapshot.kind == sd.LinuxSandboxKind.NONE
        assert snapshot.flatpak_id is None
        assert snapshot.snap_name is None
        assert snapshot.appimage_path is None


class TestFlatpak:
    def test_flatpak_id_detected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("FLATPAK_ID", "ai.sovyx.Sovyx")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.kind == sd.LinuxSandboxKind.FLATPAK
        assert snapshot.flatpak_id == "ai.sovyx.Sovyx"


class TestSnap:
    def test_snap_with_instance_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("SNAP", "/snap/sovyx/x42")
        monkeypatch.setenv("SNAP_INSTANCE_NAME", "sovyx_dev")
        monkeypatch.setenv("SNAP_NAME", "sovyx")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.kind == sd.LinuxSandboxKind.SNAP
        # Instance name wins over generic SNAP_NAME.
        assert snapshot.snap_name == "sovyx_dev"

    def test_snap_falls_back_to_snap_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("SNAP", "/snap/sovyx/x42")
        monkeypatch.delenv("SNAP_INSTANCE_NAME", raising=False)
        monkeypatch.setenv("SNAP_NAME", "sovyx")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.snap_name == "sovyx"


class TestAppImage:
    def test_appimage_path_detected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("FLATPAK_ID", raising=False)
        monkeypatch.delenv("SNAP", raising=False)
        monkeypatch.setenv("APPIMAGE", "/home/user/sovyx.AppImage")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.kind == sd.LinuxSandboxKind.APPIMAGE
        assert snapshot.appimage_path == "/home/user/sovyx.AppImage"


class TestPriorityOrdering:
    def test_flatpak_wins_over_appimage(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A Flatpak running an AppImage internally would set both;
        # the OUTERMOST sandbox is the meaningful one.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("FLATPAK_ID", "ai.sovyx.Sovyx")
        monkeypatch.setenv("APPIMAGE", "/inside/sovyx.AppImage")
        snapshot = sd.detect_linux_sandbox()
        assert snapshot.kind == sd.LinuxSandboxKind.FLATPAK


class TestLogSnapshot:
    def test_flatpak_emits_remediation(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = sd.SandboxSnapshot(
            platform_supported=True,
            kind=sd.LinuxSandboxKind.FLATPAK,
            flatpak_id="ai.sovyx.Sovyx",
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            sd.log_sandbox_snapshot(snapshot)
        records = [r for r in caplog.records if r.name == _LOGGER and isinstance(r.msg, dict)]
        assert len(records) == 1
        payload = records[0].msg
        assert payload["voice.sandbox.kind"] == "flatpak"
        assert "Flatpak" in str(payload["voice.sandbox.remediation"])

    def test_snap_emits_remediation(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = sd.SandboxSnapshot(
            platform_supported=True,
            kind=sd.LinuxSandboxKind.SNAP,
            snap_name="sovyx",
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            sd.log_sandbox_snapshot(snapshot)
        records = [r for r in caplog.records if r.name == _LOGGER and isinstance(r.msg, dict)]
        assert len(records) == 1
        payload = records[0].msg
        assert payload["voice.sandbox.kind"] == "snap"
        assert "audio-record" in str(payload["voice.sandbox.remediation"])

    def test_appimage_no_remediation(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # AppImage isn't sandboxed; remediation field absent.
        snapshot = sd.SandboxSnapshot(
            platform_supported=True,
            kind=sd.LinuxSandboxKind.APPIMAGE,
            appimage_path="/x.AppImage",
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            sd.log_sandbox_snapshot(snapshot)
        records = [r for r in caplog.records if r.name == _LOGGER and isinstance(r.msg, dict)]
        assert len(records) == 1
        payload = records[0].msg
        assert "voice.sandbox.remediation" not in payload

    def test_non_linux_silent(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = sd.SandboxSnapshot(
            platform_supported=False,
            kind=sd.LinuxSandboxKind.NONE,
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            sd.log_sandbox_snapshot(snapshot)
        records = [r for r in caplog.records if r.name == _LOGGER]
        assert records == []
