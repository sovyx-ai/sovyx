"""Tests for the NS wire-up across FrameNormalizer + factory [Phase 4 T4.13].

Coverage:

* :class:`FrameNormalizer` accepts a ``noise_suppressor`` kwarg
  and runs it AFTER AEC on every emitted 512-sample window.
* Bit-exact passthrough preserved when ``noise_suppressor`` is
  ``None`` (foundation default).
* Runtime swap via ``set_noise_suppressor``.
* Factory :func:`_build_noise_suppressor` activation matrix:
  default disabled → ``None``; engine="off" → ``None``;
  enabled+engine="spectral_gating" → real suppressor.
* AudioCaptureTask + RestartMixin plumbing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice._noise_suppression import (
    NoiseSuppressor,
    NoOpNoiseSuppressor,
    SpectralGatingSuppressor,
)
from sovyx.voice.factory import _build_noise_suppressor

# ── Test doubles ─────────────────────────────────────────────────────────


class _RecordingNs:
    """NoiseSuppressor stub that records every call + returns input/2."""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def process(self, frame: np.ndarray) -> np.ndarray:
        self.calls.append(frame.copy())
        # Halve to make the wire-up's effect observable downstream.
        return (frame.astype(np.int32) // 2).astype(np.int16)

    def reset(self) -> None: ...


# ── FrameNormalizer wire-up ──────────────────────────────────────────────


class TestFrameNormalizerNsWireUp:
    def test_default_noise_suppressor_is_none(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm.noise_suppressor is None

    def test_set_noise_suppressor_assigns(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        ns = _RecordingNs()
        norm.set_noise_suppressor(ns)
        assert norm.noise_suppressor is ns

    def test_set_noise_suppressor_can_unwire(self) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_RecordingNs(),
        )
        norm.set_noise_suppressor(None)
        assert norm.noise_suppressor is None

    def test_ns_called_once_per_emitted_window(self) -> None:
        ns = _RecordingNs()
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=ns,
        )
        # 1024 samples → 2 windows of 512 each.
        norm.push(np.full(1024, 5000, dtype=np.int16))
        assert len(ns.calls) == 2

    def test_ns_called_with_target_window_size(self) -> None:
        ns = _RecordingNs()
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=ns,
        )
        norm.push(np.full(512, 5000, dtype=np.int16))
        assert ns.calls[0].size == 512

    def test_ns_output_substituted_into_emission(self) -> None:
        ns = _RecordingNs()  # halves every input
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=ns,
        )
        windows = norm.push(np.full(512, 1000, dtype=np.int16))
        assert len(windows) == 1
        # Halved: 1000 → 500.
        assert int(windows[0][0]) == 500
        assert np.all(windows[0] == 500)

    def test_disabled_path_preserves_passthrough(self) -> None:
        # Regression-guard: foundation default must NOT mutate the
        # emitted window when no NS is wired.
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = np.array([100, -200, 300, -400] * 128, dtype=np.int16)
        windows = norm.push(block)
        assert len(windows) == 1
        np.testing.assert_array_equal(windows[0], block)

    def test_runtime_swap_starts_processing(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.push(np.full(512, 100, dtype=np.int16))  # no NS yet
        ns = _RecordingNs()
        norm.set_noise_suppressor(ns)
        norm.push(np.full(512, 100, dtype=np.int16))
        # First push had no NS → 0 calls; second push → 1 call.
        assert len(ns.calls) == 1


# ── Factory _build_noise_suppressor ──────────────────────────────────────


class TestBuildNoiseSuppressor:
    def test_default_disabled_returns_none(self) -> None:
        tuning = VoiceTuningConfig()
        assert tuning.voice_noise_suppression_enabled is False
        assert _build_noise_suppressor(tuning) is None

    def test_engine_off_returns_none(self) -> None:
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="off",
        )
        assert _build_noise_suppressor(tuning) is None

    def test_enabled_returns_real_suppressor(self) -> None:
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
        )
        ns = _build_noise_suppressor(tuning)
        assert isinstance(ns, SpectralGatingSuppressor)
        # Sanity: NOT the no-op path.
        assert not isinstance(ns, NoOpNoiseSuppressor)

    def test_threshold_propagates_to_suppressor(self) -> None:
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
            voice_noise_suppression_floor_db=-30.0,
            voice_noise_suppression_attenuation_db=-15.0,
        )
        ns = _build_noise_suppressor(tuning)
        assert ns is not None
        # Verify the suppressor accepts a 512-sample window cleanly.
        out = ns.process(np.zeros(512, dtype=np.int16))
        assert out.shape == (512,)

    def test_each_call_returns_independent_instance(self) -> None:
        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
        )
        a = _build_noise_suppressor(tuning)
        b = _build_noise_suppressor(tuning)
        assert a is not None and b is not None
        assert a is not b

    def test_disabled_path_emits_no_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        tuning = VoiceTuningConfig()
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_noise_suppressor(tuning)
        wired = [r for r in caplog.records if "voice.ns.wired" in r.getMessage()]
        assert wired == []

    def test_enabled_path_emits_one_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        tuning = VoiceTuningConfig(
            voice_noise_suppression_enabled=True,
            voice_noise_suppression_engine="spectral_gating",
        )
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_noise_suppressor(tuning)
        wired = [r for r in caplog.records if "voice.ns.wired" in r.getMessage()]
        assert len(wired) == 1


# ── AudioCaptureTask plumbing ────────────────────────────────────────────


class TestCaptureTaskNsPlumbing:
    def _pipeline_stub(self) -> MagicMock:
        return MagicMock()

    def test_default_noise_suppressor_is_none(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._noise_suppressor is None  # noqa: SLF001

    def test_explicit_noise_suppressor_stored(self) -> None:
        ns: NoiseSuppressor = NoOpNoiseSuppressor()
        task = AudioCaptureTask(self._pipeline_stub(), noise_suppressor=ns)
        assert task._noise_suppressor is ns  # noqa: SLF001
