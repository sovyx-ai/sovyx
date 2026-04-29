"""Tests for the AGC2 adaptive noise-floor tracker [Phase 4 T4.51].

Coverage:

* :class:`AdaptiveFloorConfig` window-frames derivation +
  quantile-bounds validation.
* :class:`AdaptiveNoiseFloorTracker`:
  - bootstrap state (no samples → ``floor_db is None``),
  - update accepts finite values, rejects ±inf / NaN,
  - first-quartile (or operator-tuned quantile) computed from
    deque contents,
  - sliding-window eviction respects ``maxlen``,
  - reset clears state,
  - clamping to ``[-90, -20]`` dBFS.
* :func:`build_agc2_adaptive_floor` enabled/disabled matrix.
* :class:`AGC2` integration:
  - default constructor passes None → fixed-floor behaviour preserved
    bit-exactly,
  - wired tracker overrides the fixed gate once at least one
    sample has been observed,
  - ``effective_silence_floor_dbfs`` reports the live gate.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sovyx.voice._agc2 import AGC2
from sovyx.voice._agc2_adaptive_floor import (
    AdaptiveFloorConfig,
    AdaptiveNoiseFloorTracker,
    build_agc2_adaptive_floor,
)

# ── AdaptiveFloorConfig ──────────────────────────────────────────────────


class TestAdaptiveFloorConfig:
    def _cfg(self, **overrides: object) -> AdaptiveFloorConfig:
        base: dict[str, object] = {
            "enabled": True,
            "window_seconds": 10.0,
            "quantile": 0.25,
            "sample_rate": 16_000,
            "frame_size_samples": 512,
        }
        base.update(overrides)
        return AdaptiveFloorConfig(**base)  # type: ignore[arg-type]

    def test_window_frames_at_10s_16k_512(self) -> None:
        # 10 s × 16 000 / 512 = 312.5 → rounded to 312.
        assert self._cfg().window_frames == 312

    def test_window_frames_floored_at_one(self) -> None:
        assert self._cfg(window_seconds=0.001).window_frames == 1


# ── AdaptiveNoiseFloorTracker ────────────────────────────────────────────


@pytest.fixture()
def tracker() -> AdaptiveNoiseFloorTracker:
    cfg = AdaptiveFloorConfig(
        enabled=True,
        window_seconds=10.0,
        quantile=0.25,
        sample_rate=16_000,
        frame_size_samples=512,
    )
    return AdaptiveNoiseFloorTracker(cfg)


class TestAdaptiveNoiseFloorTracker:
    def test_bootstrap_floor_is_none(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        assert tracker.floor_db is None
        assert tracker.sample_count == 0

    def test_update_accepts_finite(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        tracker.update(-50.0)
        assert tracker.sample_count == 1
        # Single sample → Q1 == that sample (quantile is exact).
        # Clamp doesn't kick in for -50 dBFS (within [-90, -20]).
        assert tracker.floor_db == pytest.approx(-50.0, abs=0.01)

    def test_update_rejects_minus_infinity(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        # -inf would always be Q1 → gate would never engage.
        tracker.update(float("-inf"))
        assert tracker.sample_count == 0
        assert tracker.floor_db is None

    def test_update_rejects_nan(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        tracker.update(float("nan"))
        assert tracker.sample_count == 0

    def test_q1_at_quartile_position(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        # 4 samples at -80, -70, -50, -30 → Q1 = -72.5 by linear
        # interpolation between idx 0 (-80) and idx 1 (-70) at the
        # 0.25 fractional position.
        for v in (-80.0, -70.0, -50.0, -30.0):
            tracker.update(v)
        assert tracker.floor_db == pytest.approx(-72.5, abs=0.5)

    def test_clamp_at_lower_bound(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        # Feed only ultra-quiet frames → Q1 ≈ -100 → clamped to -90.
        for _ in range(5):
            tracker.update(-100.0)
        assert tracker.floor_db == -90.0

    def test_clamp_at_upper_bound(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        # Feed only loud frames → Q1 ≈ -10 → clamped to -20.
        for _ in range(5):
            tracker.update(-10.0)
        assert tracker.floor_db == -20.0

    def test_sliding_window_evicts_old_samples(self) -> None:
        # Tiny 3-frame window so we can exercise eviction.
        # quantile=0.01 acts approximately like a minimum tracker
        # (linear interpolation puts the result close to the
        # smallest observed value).
        cfg = AdaptiveFloorConfig(
            enabled=True,
            window_seconds=3 * 512 / 16_000,  # exactly 3 frames
            quantile=0.01,
            sample_rate=16_000,
            frame_size_samples=512,
        )
        t = AdaptiveNoiseFloorTracker(cfg)
        # Quiet sample falls off the deque after 3 louder samples.
        t.update(-80.0)
        assert t.floor_db == pytest.approx(-80.0, abs=0.5)
        for _ in range(3):
            t.update(-50.0)
        # The first update evicted the -80; quantile of the
        # remaining 3 samples (all -50) ≈ -50.
        assert t.floor_db == pytest.approx(-50.0, abs=0.5)

    def test_quantile_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\(0.0, 1.0\)"):
            AdaptiveNoiseFloorTracker(
                AdaptiveFloorConfig(
                    enabled=True,
                    window_seconds=1.0,
                    quantile=0.0,
                    sample_rate=16_000,
                    frame_size_samples=512,
                ),
            )

    def test_quantile_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\(0.0, 1.0\)"):
            AdaptiveNoiseFloorTracker(
                AdaptiveFloorConfig(
                    enabled=True,
                    window_seconds=1.0,
                    quantile=1.0,
                    sample_rate=16_000,
                    frame_size_samples=512,
                ),
            )

    def test_reset_clears_history(self, tracker: AdaptiveNoiseFloorTracker) -> None:
        for v in (-70.0, -65.0, -60.0):
            tracker.update(v)
        assert tracker.sample_count == 3
        tracker.reset()
        assert tracker.sample_count == 0
        assert tracker.floor_db is None


# ── build_agc2_adaptive_floor ───────────────────────────────────────────


class TestBuildAgc2AdaptiveFloor:
    def test_disabled_returns_none(self) -> None:
        assert build_agc2_adaptive_floor(enabled=False) is None

    def test_enabled_returns_tracker(self) -> None:
        t = build_agc2_adaptive_floor(enabled=True)
        assert isinstance(t, AdaptiveNoiseFloorTracker)

    def test_quantile_propagates(self) -> None:
        t = build_agc2_adaptive_floor(enabled=True, quantile=0.1)
        assert t is not None
        for v in (-80.0, -70.0, -50.0, -30.0):
            t.update(v)
        # Q0.1 → first 10% — close to the minimum sample (-80).
        assert t.floor_db is not None
        assert t.floor_db < -70.0


# ── AGC2 integration ────────────────────────────────────────────────────


class TestAgc2AdaptiveIntegration:
    def test_default_constructor_no_adaptive_floor(self) -> None:
        # Pre-T4.51 contract: no adaptive_floor kwarg passed → AGC2
        # uses fixed silence_floor_dbfs unchanged.
        agc = AGC2()
        assert agc.adaptive_floor is None
        # effective_silence_floor_dbfs returns the fixed config value.
        assert agc.effective_silence_floor_dbfs == agc.config.silence_floor_dbfs

    def test_adaptive_tracker_overrides_fixed_gate_once_bootstrapped(self) -> None:
        # Wire a tracker, feed loud frames so Q1 settles ABOVE the
        # fixed -60 default → effective floor moves up.
        tracker = build_agc2_adaptive_floor(enabled=True, window_seconds=1.0)
        assert tracker is not None
        agc = AGC2(adaptive_floor=tracker)
        # Bootstrap: tracker empty → fall back to fixed gate.
        assert agc.effective_silence_floor_dbfs == agc.config.silence_floor_dbfs
        # Feed loud frames (~-30 dBFS RMS each). amplitude ≈ 1000 →
        # RMS = 1000 / 32768 ≈ -30.3 dBFS. -30.3 sits within the
        # ``[-90, -20]`` clamp so it passes through unchanged.
        loud = np.full(512, 1_000, dtype=np.int16)
        for _ in range(10):
            agc.process(loud)
        assert agc.adaptive_floor is not None
        floor = agc.adaptive_floor.floor_db
        assert floor is not None
        # Q1 of constant ~-30.3 dBFS samples is ~-30.3.
        assert floor == pytest.approx(-30.3, abs=1.0)
        assert agc.effective_silence_floor_dbfs == floor

    def test_adaptive_tracker_does_not_mutate_signal(self) -> None:
        # Sanity: wiring the tracker doesn't change AGC2's output
        # for the same input vs the no-tracker baseline.
        tracker = build_agc2_adaptive_floor(enabled=True)
        agc_with = AGC2(adaptive_floor=tracker)
        agc_without = AGC2()
        # Identical input frame.
        rng = np.random.default_rng(0)
        frame = rng.integers(-5_000, 5_000, 512).astype(np.int16)
        # First-frame output: AGC2 hasn't converged yet, both
        # should produce the same result (gain still at 0 dB).
        out_with = agc_with.process(frame.copy())
        out_without = agc_without.process(frame.copy())
        np.testing.assert_array_equal(out_with, out_without)

    def test_silent_frames_still_count_against_silenced_counter(self) -> None:
        # When the adaptive floor anchors at, say, -30 dBFS
        # (loud-room scenario), frames at -50 dBFS get gated. The
        # frames_silenced counter must still reflect this.
        tracker = build_agc2_adaptive_floor(enabled=True, window_seconds=1.0)
        agc = AGC2(adaptive_floor=tracker)
        # Boot the tracker into "loud room" mode: 10 frames @ ~-20.
        loud = np.full(512, 3_300, dtype=np.int16)  # ≈ -20 dBFS
        for _ in range(10):
            agc.process(loud)
        baseline_silenced = agc.frames_silenced
        # Now feed a quiet frame; floor is ~-20 → -50 dBFS gets gated.
        quiet = np.full(512, 100, dtype=np.int16)  # ≈ -50 dBFS
        agc.process(quiet)
        assert agc.frames_silenced == baseline_silenced + 1


def test_module_imports() -> None:
    """Sanity: math import not removed by future cleanup."""
    assert math is not None
