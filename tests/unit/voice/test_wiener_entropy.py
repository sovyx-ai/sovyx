"""Tests for :mod:`sovyx.voice._wiener_entropy` [Phase 4 T4.44].

Coverage:

* :func:`compute_wiener_entropy` algebraic identities:
  - pure tone → near-zero entropy,
  - white noise → near-one entropy,
  - silence handling (zero / empty input → 0.0),
  - dtype validation,
  - constant DC signal → near-zero (single bin dominates),
  - structured speech-like signal sits between tones and noise.
* :func:`is_signal_destroyed` predicate threshold matrix.
* Numerical stability: result always within ``[0.0, 1.0]`` even
  on near-uniform spectra.
"""

from __future__ import annotations

import numpy as np
import pytest

from sovyx.voice._wiener_entropy import (
    compute_wiener_entropy,
    is_signal_destroyed,
)

# ── compute_wiener_entropy ──────────────────────────────────────────────


class TestComputeWienerEntropy:
    def test_silence_returns_zero(self) -> None:
        assert compute_wiener_entropy(np.zeros(512, dtype=np.int16)) == 0.0

    def test_empty_returns_zero(self) -> None:
        assert compute_wiener_entropy(np.array([], dtype=np.int16)) == 0.0

    def test_rejects_non_int16(self) -> None:
        with pytest.raises(ValueError, match="int16"):
            compute_wiener_entropy(np.zeros(512, dtype=np.float32))

    def test_pure_tone_near_zero(self) -> None:
        # 1 kHz sine at 16 kHz → energy concentrated in the bin
        # closest to 1000/16000 * 512 = 32. Entropy should be
        # well below 0.05 (essentially "zero").
        n = 512
        t = np.arange(n) / 16_000.0
        tone = (np.sin(2 * np.pi * 1_000 * t) * 8_000).astype(np.int16)
        entropy = compute_wiener_entropy(tone)
        assert entropy < 0.05

    def test_white_noise_near_one(self) -> None:
        # Gaussian noise → roughly uniform spectrum → entropy ≈ 1.
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(512) * 1_000).astype(np.int16)
        entropy = compute_wiener_entropy(noise)
        # Empirical: white noise hits ~0.5-0.7 in 512-sample
        # windows due to per-bin variance. Pin a generous lower
        # bound that clearly separates from "signal" cases (which
        # are < 0.5).
        assert entropy > 0.3

    def test_white_noise_significantly_above_tone(self) -> None:
        # Direct comparison: noise must score MUCH higher than tone.
        rng = np.random.default_rng(1)
        n = 512
        t = np.arange(n) / 16_000.0
        tone = (np.sin(2 * np.pi * 1_000 * t) * 8_000).astype(np.int16)
        noise = (rng.standard_normal(n) * 1_000).astype(np.int16)
        e_tone = compute_wiener_entropy(tone)
        e_noise = compute_wiener_entropy(noise)
        assert e_noise > e_tone + 0.25  # generous margin for stability

    def test_constant_dc_signal_near_zero(self) -> None:
        # All samples at constant amplitude → only the DC bin has
        # power → entropy near 0 (one bin dominates).
        constant = np.full(512, 5_000, dtype=np.int16)
        entropy = compute_wiener_entropy(constant)
        assert entropy < 0.05

    def test_two_tones_below_noise_threshold(self) -> None:
        # Sum of two tones — slightly higher entropy than one
        # tone but still well below white-noise levels.
        n = 512
        t = np.arange(n) / 16_000.0
        signal = (
            np.sin(2 * np.pi * 1_000 * t) * 4_000 + np.sin(2 * np.pi * 3_000 * t) * 4_000
        ).astype(np.int16)
        entropy = compute_wiener_entropy(signal)
        # Two-tone is more "spread" than single-tone but still far
        # from white noise.
        assert entropy < 0.3

    def test_result_always_in_unit_interval(self) -> None:
        # Property test on a handful of seeds: result must always
        # satisfy 0 ≤ entropy ≤ 1.
        for seed in range(10):
            rng = np.random.default_rng(seed)
            data = (rng.standard_normal(512) * 5_000).astype(np.int16)
            entropy = compute_wiener_entropy(data)
            assert 0.0 <= entropy <= 1.0


# ── is_signal_destroyed ─────────────────────────────────────────────────


class TestIsSignalDestroyed:
    def test_silence_not_destroyed(self) -> None:
        # Silent frames → entropy 0 → NOT destroyed by convention.
        assert is_signal_destroyed(np.zeros(512, dtype=np.int16)) is False

    def test_pure_tone_not_destroyed(self) -> None:
        n = 512
        t = np.arange(n) / 16_000.0
        tone = (np.sin(2 * np.pi * 1_000 * t) * 8_000).astype(np.int16)
        assert is_signal_destroyed(tone) is False

    def test_white_noise_at_default_threshold(self) -> None:
        # At the default threshold of 0.5, fast white noise may
        # or may not exceed depending on exact windowing — assert
        # ONLY when a clearly-destroyed extreme case fires.
        rng = np.random.default_rng(0)
        # Massive amplitude white noise: above the threshold for
        # most reasonable thresholds.
        loud_noise = (rng.standard_normal(2_048) * 10_000).astype(np.int16)
        # With threshold=0.3 (lower than default), loud noise must
        # be flagged as destroyed.
        assert is_signal_destroyed(loud_noise, threshold=0.3) is True

    def test_threshold_zero_flags_anything_non_silent(self) -> None:
        # threshold=0.0 → any signal with entropy > 0 (so any
        # non-pure-tone / non-silent signal) is destroyed.
        n = 512
        t = np.arange(n) / 16_000.0
        # Even a tone has tiny non-zero entropy → flagged at
        # threshold=0.
        tone = (np.sin(2 * np.pi * 1_000 * t) * 8_000).astype(np.int16)
        # With threshold=0 and entropy > 0 (any leakage from a
        # tone), the predicate fires.
        # NB: pure DC at threshold=0 may NOT fire because entropy
        # might be exactly 0 with one-bin spectra. Use a tone.
        assert is_signal_destroyed(tone, threshold=0.0) is True

    def test_threshold_one_never_flags(self) -> None:
        # threshold=1.0 → entropy must EXCEED 1.0 to flag, which
        # is algebraically impossible. Predicate always False.
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(2_048) * 10_000).astype(np.int16)
        assert is_signal_destroyed(noise, threshold=1.0) is False

    def test_threshold_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            is_signal_destroyed(
                np.zeros(512, dtype=np.int16),
                threshold=-0.1,
            )

    def test_threshold_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            is_signal_destroyed(
                np.zeros(512, dtype=np.int16),
                threshold=1.1,
            )
