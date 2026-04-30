"""Tests for AGC2 VAD-feedback gate [Phase 4 T4.52].

Coverage:

* :func:`set_last_verdict` + :func:`get_last_verdict` freshness
  contract (default 0.5 s window).
* Stale verdict returns ``None``; AGC2 falls back to RMS gate.
* Empty channel returns ``None``.
* :class:`AGC2` foundation: ``vad_feedback_enabled=False`` is
  bit-exact pre-T4.52 — speech-level estimator updates every
  RMS-above-floor frame regardless of the published verdict.
* :class:`AGC2` with ``vad_feedback_enabled=True``:
  - VAD says speech AND RMS above floor → estimator updates.
  - VAD says NOT speech AND RMS above floor → estimator
    BLOCKED, ``frames_vad_silenced`` increments.
  - VAD says NOT speech AND RMS below floor → estimator blocked
    (the existing RMS gate would have blocked too); counted as
    ``frames_silenced`` (RMS path), not ``frames_vad_silenced``.
  - No verdict published yet (None) → estimator follows RMS
    gate (fallback). Allows AGC2 to adapt during the warm-up
    period before the first VAD inference completes.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sovyx.voice._agc2 import AGC2, AGC2Config
from sovyx.voice.health._vad_feedback import (
    get_last_verdict,
    reset_for_tests,
    set_last_verdict,
)

_INT16_FULL_SCALE = float((1 << 15) - 1)


def _frame_at_dbfs(target_dbfs: float, *, samples: int = 512) -> np.ndarray:
    """Build a sinusoidal int16 frame with a controlled RMS dBFS."""
    if target_dbfs == float("-inf"):
        return np.zeros(samples, dtype=np.int16)
    rms_linear = (10.0 ** (target_dbfs / 20.0)) * _INT16_FULL_SCALE
    # sin RMS = peak / sqrt(2) → peak = RMS * sqrt(2).
    peak = rms_linear * math.sqrt(2)
    n = np.arange(samples)
    sig = np.sin(2 * np.pi * 1_000 * n / 16_000) * peak
    return sig.astype(np.int16)


@pytest.fixture(autouse=True)
def _clear_feedback() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestVadFeedbackChannel:
    def test_empty_returns_none(self) -> None:
        assert get_last_verdict(now_monotonic=100.0) is None

    def test_fresh_verdict_returns_value(self) -> None:
        set_last_verdict(is_speech=True, monotonic=100.0)
        assert get_last_verdict(now_monotonic=100.1) is True

    def test_stale_verdict_returns_none(self) -> None:
        set_last_verdict(is_speech=True, monotonic=100.0)
        # Default freshness window = 0.5 s.
        assert get_last_verdict(now_monotonic=101.0) is None

    def test_overwrite_preserves_freshness(self) -> None:
        set_last_verdict(is_speech=True, monotonic=100.0)
        set_last_verdict(is_speech=False, monotonic=100.4)
        assert get_last_verdict(now_monotonic=100.5) is False

    def test_custom_max_age_extends_window(self) -> None:
        set_last_verdict(is_speech=True, monotonic=100.0)
        # 5 s old is normally stale; with max_age=10 s it's fresh.
        assert get_last_verdict(now_monotonic=105.0, max_age_seconds=10.0) is True


class TestAgc2VadFeedbackDisabled:
    """Foundation default: bit-exact pre-T4.52 (no VAD gating)."""

    def test_estimator_updates_when_rms_above_floor_regardless_of_verdict(
        self,
    ) -> None:
        # Disabled flag → AGC2 ignores published verdicts.
        agc2 = AGC2(AGC2Config())
        # Publish a "not speech" verdict; AGC2 should NOT consult it.
        set_last_verdict(is_speech=False, monotonic=100.0)

        # -25 dBFS = above the default -60 dBFS silence floor.
        loud = _frame_at_dbfs(-25.0)
        agc2.process(loud)
        assert agc2.frames_processed == 1
        assert agc2.frames_silenced == 0
        # The new T4.52 counter stays at zero — gate didn't run.
        assert agc2.frames_vad_silenced == 0


class TestAgc2VadFeedbackEnabled:
    """``vad_feedback_enabled=True``: gate is RMS AND VAD."""

    def _enabled_agc2(self) -> AGC2:
        return AGC2(AGC2Config(), vad_feedback_enabled=True)

    def test_speech_verdict_above_floor_allows_update(self) -> None:
        import time as _time

        agc2 = self._enabled_agc2()
        set_last_verdict(is_speech=True, monotonic=_time.monotonic())
        loud = _frame_at_dbfs(-25.0)
        agc2.process(loud)
        # Update path executed; VAD-silenced counter stays 0.
        assert agc2.frames_silenced == 0
        assert agc2.frames_vad_silenced == 0

    def test_non_speech_verdict_above_floor_blocks_update(self) -> None:
        import time as _time

        agc2 = self._enabled_agc2()
        # Use the real monotonic clock so AGC2's freshness check
        # (which also reads time.monotonic) sees the verdict as
        # fresh. Hard-coded fake values would land "stale" relative
        # to the host's monotonic tick.
        set_last_verdict(is_speech=False, monotonic=_time.monotonic())
        loud = _frame_at_dbfs(-25.0)
        agc2.process(loud)
        # The classic noise-pumping pattern: RMS would have
        # let AGC2 adapt, but VAD vetoed.
        assert agc2.frames_vad_silenced == 1
        # frames_silenced (RMS-path) stays 0 — RMS WAS above floor.
        assert agc2.frames_silenced == 0

    def test_non_speech_verdict_below_floor_uses_rms_silenced_counter(
        self,
    ) -> None:
        import time as _time

        agc2 = self._enabled_agc2()
        set_last_verdict(is_speech=False, monotonic=_time.monotonic())
        # -80 dBFS = below the -60 dBFS silence floor.
        quiet = _frame_at_dbfs(-80.0)
        agc2.process(quiet)
        # RMS gate fires first (well-defined "below floor" path);
        # the new T4.52 counter stays at 0.
        assert agc2.frames_silenced == 1
        assert agc2.frames_vad_silenced == 0

    def test_no_published_verdict_falls_back_to_rms_gate(self) -> None:
        # No set_last_verdict() call → channel returns None.
        # AGC2 must still adapt on RMS-above-floor (warm-up
        # behaviour before the first VAD inference).
        agc2 = self._enabled_agc2()
        loud = _frame_at_dbfs(-25.0)
        agc2.process(loud)
        # Updated as if T4.52 weren't enabled — fallback works.
        assert agc2.frames_silenced == 0
        assert agc2.frames_vad_silenced == 0

    def test_stale_verdict_falls_back_to_rms_gate(self) -> None:
        # Verdict published 10 s ago — well past the 0.5 s freshness
        # window. AGC2 reads None and falls back to RMS-only gate.
        # NOTE: this test depends on the time module's monotonic
        # clock, so we use the real clock with a "obviously stale"
        # write.
        agc2 = self._enabled_agc2()
        set_last_verdict(is_speech=False, monotonic=0.0)  # epoch ~0
        # Real monotonic clock is now ≫ 0, so the verdict is stale.
        loud = _frame_at_dbfs(-25.0)
        agc2.process(loud)
        # Fallback → estimator updates as in pre-T4.52.
        assert agc2.frames_vad_silenced == 0
