"""Tests for :mod:`sovyx.voice._snr_estimator` [Phase 4 T4.31].

Coverage:

* :class:`SnrEstimatorConfig` — derived properties (window-frames,
  silence-floor linear).
* :func:`estimate_frame_power` — pure-DSP power computation.
* :class:`SnrEstimator` — minimum-tracker behaviour:
  - silent frames return floor + don't update tracker,
  - constant-power input → SNR ≈ 0 dB after first frame,
  - loud-then-quiet sequence → high SNR for loud frames,
  - sliding window expires old samples,
  - reset clears state.
* :func:`build_snr_estimator` and
  :func:`build_frame_normalizer_snr_estimator` factory matrix.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures

from sovyx.voice._snr_estimator import (
    SnrEstimator,
    SnrEstimatorConfig,
    build_frame_normalizer_snr_estimator,
    build_snr_estimator,
    estimate_frame_power,
)

# ── SnrEstimatorConfig derived properties ────────────────────────────────


class TestSnrEstimatorConfig:
    def _cfg(self, **overrides: object) -> SnrEstimatorConfig:
        base: dict[str, object] = {
            "enabled": True,
            "sample_rate": 16_000,
            "frame_size_samples": 512,
            "noise_window_seconds": 5.0,
            "silence_floor_db": -90.0,
        }
        base.update(overrides)
        return SnrEstimatorConfig(**base)  # type: ignore[arg-type]

    def test_window_frames_at_5s_16k_512(self) -> None:
        # 5 s * 16000 / 512 = 156.25 → rounded to 156.
        cfg = self._cfg()
        assert cfg.noise_window_frames == 156

    def test_window_frames_floored_at_one(self) -> None:
        # 0.001 s window → 0.03 frames → floored to 1.
        cfg = self._cfg(noise_window_seconds=0.001)
        assert cfg.noise_window_frames == 1

    def test_silence_floor_at_minus_90_dbfs(self) -> None:
        # Mean-square scale: -90 dBFS = 32768² × 10^(-180/10) ≈ 1.07.
        cfg = self._cfg(silence_floor_db=-90.0)
        assert 0.5 < cfg.silence_floor_linear_sq < 2.0

    def test_silence_floor_at_minus_60_dbfs(self) -> None:
        # -60 dBFS = 32768² × 10^(-120/10) ≈ 1074.
        cfg = self._cfg(silence_floor_db=-60.0)
        assert 1000 < cfg.silence_floor_linear_sq < 1100


# ── estimate_frame_power ────────────────────────────────────────────────


class TestEstimateFramePower:
    def test_zero_input_returns_zero(self) -> None:
        assert estimate_frame_power(np.zeros(512, dtype=np.int16)) == 0.0

    def test_constant_full_scale(self) -> None:
        # All samples at int16 max → power = 32767² ≈ 1.073e9.
        frame = np.full(512, 32_767, dtype=np.int16)
        result = estimate_frame_power(frame)
        assert result == pytest.approx(32_767**2, rel=1e-6)

    def test_constant_half_scale(self) -> None:
        frame = np.full(512, 16_384, dtype=np.int16)
        result = estimate_frame_power(frame)
        # 16384² = 2.68e8.
        assert result == pytest.approx(16_384**2, rel=1e-6)

    def test_empty_returns_zero(self) -> None:
        assert estimate_frame_power(np.array([], dtype=np.int16)) == 0.0

    def test_rejects_non_int16(self) -> None:
        with pytest.raises(ValueError, match="dtype"):
            estimate_frame_power(np.zeros(512, dtype=np.float32))


# ── SnrEstimator behaviour ──────────────────────────────────────────────


@pytest.fixture()
def cfg() -> SnrEstimatorConfig:
    return SnrEstimatorConfig(
        enabled=True,
        sample_rate=16_000,
        frame_size_samples=512,
        noise_window_seconds=5.0,
        silence_floor_db=-90.0,
    )


class TestSnrEstimator:
    def test_silent_frame_returns_floor(self, cfg: SnrEstimatorConfig) -> None:
        est = SnrEstimator(cfg)
        # Below -90 dBFS → silent → no tracker update + return floor.
        snr = est.estimate(np.zeros(512, dtype=np.int16))
        assert snr == -120.0
        assert est.frames_seen == 0
        assert est.noise_floor_estimate is None

    def test_first_loud_frame_reports_ceiling(self, cfg: SnrEstimatorConfig) -> None:
        # First non-silent frame: noise tracker is empty → after
        # update, the only sample IS the noise floor. SNR = 0 dB
        # by the formula (P_signal == P_noise → speech_power = 0).
        # The estimator's branch returns 0.0 in that case (NOT
        # ceiling — ceiling is only for noise_floor at silence).
        est = SnrEstimator(cfg)
        snr = est.estimate(np.full(512, 8_000, dtype=np.int16))
        assert snr == 0.0
        assert est.frames_seen == 1

    def test_loud_after_quiet_yields_positive_snr(
        self,
        cfg: SnrEstimatorConfig,
    ) -> None:
        # Feed a quiet frame first to anchor the noise floor, then
        # a loud frame. SNR should be substantially positive.
        est = SnrEstimator(cfg)
        # Quiet frame ≈ -50 dBFS RMS (amplitude 100).
        quiet = np.full(512, 100, dtype=np.int16)
        est.estimate(quiet)
        # Loud frame ≈ -10 dBFS RMS (amplitude 10000).
        loud = np.full(512, 10_000, dtype=np.int16)
        snr = est.estimate(loud)
        # Loud signal vs quiet noise → SNR ≥ 30 dB expected
        # (power ratio 10000²/100² = 1e4 → 40 dB).
        assert snr > 30.0
        assert snr < 50.0

    def test_constant_power_holds_zero_snr(
        self,
        cfg: SnrEstimatorConfig,
    ) -> None:
        # Constant amplitude → noise floor == current power
        # forever → SNR stays at 0 dB.
        est = SnrEstimator(cfg)
        frame = np.full(512, 5_000, dtype=np.int16)
        for _ in range(10):
            snr = est.estimate(frame)
            assert snr == 0.0

    def test_noise_floor_tracks_minimum(
        self,
        cfg: SnrEstimatorConfig,
    ) -> None:
        est = SnrEstimator(cfg)
        # Feed loud, loud, quiet, loud — the tracker should anchor
        # to the quiet frame's power.
        loud = np.full(512, 8_000, dtype=np.int16)
        quiet = np.full(512, 200, dtype=np.int16)
        est.estimate(loud)
        est.estimate(loud)
        est.estimate(quiet)
        est.estimate(loud)
        # Noise floor is the minimum of the four samples.
        floor = est.noise_floor_estimate
        assert floor is not None
        # Quiet frame's power = 200² = 40 000.
        assert floor == pytest.approx(40_000.0, rel=1e-6)

    def test_sliding_window_expires_old_samples(self) -> None:
        # Tiny 3-frame window so we can exercise eviction quickly.
        cfg = SnrEstimatorConfig(
            enabled=True,
            sample_rate=16_000,
            frame_size_samples=512,
            noise_window_seconds=3 * 512 / 16_000,  # exactly 3 frames
            silence_floor_db=-90.0,
        )
        est = SnrEstimator(cfg)
        # Feed: quiet, loud, loud, loud — after the 4th frame the
        # quiet sample falls off the deque so the floor jumps up.
        quiet = np.full(512, 200, dtype=np.int16)
        loud = np.full(512, 8_000, dtype=np.int16)
        est.estimate(quiet)
        floor_after_quiet = est.noise_floor_estimate
        assert floor_after_quiet == pytest.approx(40_000.0, rel=1e-6)
        est.estimate(loud)
        est.estimate(loud)
        est.estimate(loud)  # this push evicts the quiet sample.
        # Floor is now the minimum of three loud frames.
        floor_after_evict = est.noise_floor_estimate
        assert floor_after_evict is not None
        assert floor_after_evict == pytest.approx(8_000**2, rel=1e-6)

    def test_silent_frames_do_not_pollute_tracker(
        self,
        cfg: SnrEstimatorConfig,
    ) -> None:
        # Silent frames are skipped, so the tracker never gets
        # pinned at floor-level garbage that would falsely report
        # huge SNR for the next real frame.
        est = SnrEstimator(cfg)
        for _ in range(10):
            est.estimate(np.zeros(512, dtype=np.int16))
        assert est.frames_seen == 0
        # Now a real frame: tracker is empty, this becomes the
        # only sample → SNR = 0 dB.
        snr = est.estimate(np.full(512, 5_000, dtype=np.int16))
        assert snr == 0.0

    def test_reset_clears_tracker(
        self,
        cfg: SnrEstimatorConfig,
    ) -> None:
        est = SnrEstimator(cfg)
        for _ in range(5):
            est.estimate(np.full(512, 200, dtype=np.int16))
        assert est.frames_seen == 5
        est.reset()
        assert est.frames_seen == 0
        assert est.noise_floor_estimate is None
        # Post-reset state is fresh: feeding a loud frame returns
        # 0 dB (becomes the new floor).
        snr = est.estimate(np.full(512, 10_000, dtype=np.int16))
        assert snr == 0.0

    def test_snr_clamped_at_ceiling(self, cfg: SnrEstimatorConfig) -> None:
        # Pre-load the tracker with a near-silence-floor sample,
        # then feed a loud frame. The SNR ratio would be >> 1e12
        # → the formula caps at +120 dB.
        est = SnrEstimator(cfg)
        # Just-above-floor frame (amp=2 → power=4 ≥ silence_floor 1.07).
        est.estimate(np.full(512, 2, dtype=np.int16))
        snr = est.estimate(np.full(512, 30_000, dtype=np.int16))
        assert 60.0 < snr <= 120.0


# ── Factory ──────────────────────────────────────────────────────────────


class TestBuildSnrEstimator:
    def test_disabled_returns_none(self) -> None:
        cfg = SnrEstimatorConfig(
            enabled=False,
            sample_rate=16_000,
            frame_size_samples=512,
            noise_window_seconds=5.0,
            silence_floor_db=-90.0,
        )
        assert build_snr_estimator(cfg) is None

    def test_enabled_returns_estimator(self) -> None:
        cfg = SnrEstimatorConfig(
            enabled=True,
            sample_rate=16_000,
            frame_size_samples=512,
            noise_window_seconds=5.0,
            silence_floor_db=-90.0,
        )
        est = build_snr_estimator(cfg)
        assert isinstance(est, SnrEstimator)

    def test_enabled_via_replace(self) -> None:
        # Verifies dataclasses.replace flow that operators / tests
        # use to override one knob without rebuilding the full
        # config.
        base = SnrEstimatorConfig(
            enabled=False,
            sample_rate=16_000,
            frame_size_samples=512,
            noise_window_seconds=5.0,
            silence_floor_db=-90.0,
        )
        on = dataclasses.replace(base, enabled=True)
        assert build_snr_estimator(on) is not None


class TestBuildFrameNormalizerSnrEstimator:
    """The 16 kHz / 512-sample factory pins the FrameNormalizer invariants."""

    def test_disabled_returns_none(self) -> None:
        assert (
            build_frame_normalizer_snr_estimator(
                enabled=False,
                noise_window_seconds=5.0,
                silence_floor_db=-90.0,
            )
            is None
        )

    def test_enabled_returns_real_estimator(self) -> None:
        est = build_frame_normalizer_snr_estimator(
            enabled=True,
            noise_window_seconds=5.0,
            silence_floor_db=-90.0,
        )
        assert isinstance(est, SnrEstimator)
        # Verify it accepts a 512-sample frame cleanly.
        snr = est.estimate(np.full(512, 1_000, dtype=np.int16))
        assert snr == 0.0  # First frame anchors floor to itself.
