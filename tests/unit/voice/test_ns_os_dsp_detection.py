"""Tests for the OS-NS auto-disable logic [Phase 4 T4.18 + T4.20].

Coverage:

* :func:`_detect_os_noise_suppression` cross-platform dispatch:
  Windows uses Voice Clarity; Linux uses PipeWire echo-cancel;
  macOS uses HAL plug-ins.
* Detector errors fall back to ``False`` (refusing to ship NS
  because the probe crashed would surprise opted-in operators).
* :func:`_build_noise_suppressor` honours the
  ``voice_use_os_dsp_when_available`` flag matrix:
  - default ``False`` → ship NS regardless of OS-NS state,
  - ``True`` + OS-NS active → return ``None`` + emit
    ``voice.ns.deferred_to_os_dsp``,
  - ``True`` + OS-NS inactive → ship NS as usual.
"""

from __future__ import annotations

import sys

import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._noise_suppression import SpectralGatingSuppressor
from sovyx.voice.factory import (
    _build_noise_suppressor,
    _detect_os_noise_suppression,
)

# ── _detect_os_noise_suppression cross-platform dispatch ────────────────


class TestDetectOsNoiseSuppression:
    """Each platform path resolves to the correct probe."""

    def test_windows_dispatches_to_voice_clarity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        from sovyx.voice import factory

        called: dict[str, str | None] = {}

        def _stub_detect(name: str | None) -> bool:
            called["name"] = name
            return True

        monkeypatch.setattr(factory, "_detect_voice_clarity_active", _stub_detect)
        result = _detect_os_noise_suppression(resolved_name="Razer BlackShark V2")
        assert result is True
        assert called["name"] == "Razer BlackShark V2"

    def test_linux_dispatches_to_pipewire_echo_cancel(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        import sovyx.voice.health._pipewire as _pipewire_mod

        class _FakeReport:
            echo_cancel_loaded = True

        monkeypatch.setattr(_pipewire_mod, "detect_pipewire", lambda: _FakeReport())
        assert _detect_os_noise_suppression() is True

    def test_linux_returns_false_when_echo_cancel_not_loaded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        import sovyx.voice.health._pipewire as _pipewire_mod

        class _FakeReport:
            echo_cancel_loaded = False

        monkeypatch.setattr(_pipewire_mod, "detect_pipewire", lambda: _FakeReport())
        assert _detect_os_noise_suppression() is False

    def test_macos_dispatches_to_hal_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        import sovyx.voice._hal_detector_mac as _hal_mod

        class _FakeHalReport:
            virtual_audio_active = False
            audio_enhancement_active = True  # e.g. Krisp

        monkeypatch.setattr(_hal_mod, "detect_hal_plugins", lambda: _FakeHalReport())
        assert _detect_os_noise_suppression() is True

    def test_macos_either_flag_triggers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        import sovyx.voice._hal_detector_mac as _hal_mod

        class _VirtualOnly:
            virtual_audio_active = True
            audio_enhancement_active = False

        monkeypatch.setattr(_hal_mod, "detect_hal_plugins", lambda: _VirtualOnly())
        assert _detect_os_noise_suppression() is True

    def test_macos_neither_flag_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        import sovyx.voice._hal_detector_mac as _hal_mod

        class _Clean:
            virtual_audio_active = False
            audio_enhancement_active = False

        monkeypatch.setattr(_hal_mod, "detect_hal_plugins", lambda: _Clean())
        assert _detect_os_noise_suppression() is False

    def test_unknown_platform_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "freebsd14")
        assert _detect_os_noise_suppression() is False

    def test_detector_exception_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Probe crashes → swallow + return False so the operator's
        # NS choice isn't silently overridden by a broken detector.
        monkeypatch.setattr(sys, "platform", "linux")
        import sovyx.voice.health._pipewire as _pipewire_mod

        def _raise() -> None:
            raise RuntimeError("simulated probe failure")

        monkeypatch.setattr(_pipewire_mod, "detect_pipewire", _raise)
        assert _detect_os_noise_suppression() is False


# ── _build_noise_suppressor + voice_use_os_dsp_when_available matrix ────


class TestBuildNsRespectsOsDspFlag:
    """Auto-disable only fires when the operator opts in."""

    def test_default_flag_is_false(self) -> None:
        tuning = VoiceTuningConfig()
        assert tuning.voice_use_os_dsp_when_available is False

    def test_default_flag_off_ignores_os_ns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator did NOT opt into OS deference → ship NS even
        # though OS-NS is detected (preserves "predictability"
        # default per master mission §Phase 4 / T4.19).
        from sovyx.voice import factory

        monkeypatch.setattr(
            factory,
            "_detect_os_noise_suppression",
            lambda *, resolved_name=None: True,
        )
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
        )
        ns = _build_noise_suppressor(tuning, resolved_name="Voice Clarity Mic")
        assert isinstance(ns, SpectralGatingSuppressor)

    def test_flag_on_with_os_ns_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Operator opted into OS deference + OS-NS detected →
        # auto-disable in-process NS.
        import logging as _logging

        from sovyx.voice import factory

        monkeypatch.setattr(
            factory,
            "_detect_os_noise_suppression",
            lambda *, resolved_name=None: True,
        )
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
            voice_use_os_dsp_when_available=True,
        )
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            ns = _build_noise_suppressor(tuning, resolved_name="Voice Clarity Mic")
        assert ns is None
        deferred = [r for r in caplog.records if "voice.ns.deferred_to_os_dsp" in r.getMessage()]
        assert len(deferred) == 1

    def test_flag_on_without_os_ns_ships_ns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator opted in but OS-NS NOT detected → ship NS as
        # usual (the deference is conditional, not absolute).
        from sovyx.voice import factory

        monkeypatch.setattr(
            factory,
            "_detect_os_noise_suppression",
            lambda *, resolved_name=None: False,
        )
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
            voice_use_os_dsp_when_available=True,
        )
        ns = _build_noise_suppressor(tuning, resolved_name="Plain Mic")
        assert isinstance(ns, SpectralGatingSuppressor)

    def test_flag_on_disabled_ns_returns_none(
        self,
    ) -> None:
        # NS master switch off → return None regardless of OS-NS
        # state (no probe even runs).
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=False,
            voice_use_os_dsp_when_available=True,
        )
        assert _build_noise_suppressor(tuning) is None

    def test_resolved_name_propagates_to_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice import factory

        captured: dict[str, str | None] = {}

        def _fake_detect(*, resolved_name: str | None = None) -> bool:
            captured["name"] = resolved_name
            return False

        monkeypatch.setattr(factory, "_detect_os_noise_suppression", _fake_detect)
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_use_os_dsp_when_available=True,
        )
        _build_noise_suppressor(tuning, resolved_name="My Mic")
        assert captured["name"] == "My Mic"
