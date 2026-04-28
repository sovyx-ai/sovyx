"""Tests for :mod:`sovyx.voice._speaker_consistency` — T1.39.

Covers the spectral-centroid drift detector that complements
T1.36's zero-energy validation:

* :func:`compute_spectral_centroid` — pure DSP function.
* :class:`SpeakerConsistencyMonitor` — per-session rolling-window
  drift state machine.

Reference: ``docs-internal/T1.39-speaker-consistency-check-rfc.md``
"""

from __future__ import annotations

import numpy as np
import pytest

from sovyx.voice._speaker_consistency import (
    SpeakerConsistencyMonitor,
    compute_spectral_centroid,
)

# ── compute_spectral_centroid (pure DSP) ────────────────────────────


class TestComputeSpectralCentroid:
    """Pin the DSP contract: magnitude-weighted mean of the rfft spectrum."""

    def test_empty_array_returns_zero(self) -> None:
        """Empty input → 0.0 sentinel (caller skips drift check)."""
        result = compute_spectral_centroid(
            np.array([], dtype=np.int16),
            sample_rate=22050,
        )
        assert result == 0.0

    def test_all_zero_array_returns_zero(self) -> None:
        """Silent input has no magnitude → 0.0 sentinel."""
        result = compute_spectral_centroid(
            np.zeros(1024, dtype=np.int16),
            sample_rate=22050,
        )
        assert result == 0.0

    def test_pure_440hz_sine_returns_centroid_near_440hz(self) -> None:
        """A pure tone has all its spectral mass at one frequency.
        The centroid MUST land within 1% of that frequency (FFT bin
        resolution + numpy float64 precision)."""
        sample_rate = 22050
        duration_s = 1.0
        target_hz = 440.0
        samples = int(sample_rate * duration_s)
        t = np.arange(samples) / sample_rate
        signal = (np.sin(2 * np.pi * target_hz * t) * 16384).astype(np.int16)
        centroid = compute_spectral_centroid(signal, sample_rate)
        assert abs(centroid - target_hz) / target_hz < 0.01, (
            f"Pure {target_hz} Hz sine centroid was {centroid:.1f} Hz; "
            f"expected within 1 % of {target_hz}"
        )

    def test_dual_tone_centroid_is_between_components(self) -> None:
        """Equal-magnitude dual tone (440 Hz + 880 Hz) → centroid ≈ 660 Hz."""
        sample_rate = 22050
        duration_s = 1.0
        samples = int(sample_rate * duration_s)
        t = np.arange(samples) / sample_rate
        signal = ((np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 880 * t)) * 8192).astype(
            np.int16
        )
        centroid = compute_spectral_centroid(signal, sample_rate)
        assert 600.0 < centroid < 720.0, (
            f"Dual-tone (440+880) centroid was {centroid:.1f} Hz; expected near 660 Hz (midpoint)"
        )

    def test_higher_frequency_signal_has_higher_centroid(self) -> None:
        """Sanity: a 2 kHz tone has a higher centroid than a 500 Hz tone.
        This is the property the drift detector actually leans on."""
        sample_rate = 22050
        samples = sample_rate
        t = np.arange(samples) / sample_rate
        low = (np.sin(2 * np.pi * 500 * t) * 16384).astype(np.int16)
        high = (np.sin(2 * np.pi * 2000 * t) * 16384).astype(np.int16)
        centroid_low = compute_spectral_centroid(low, sample_rate)
        centroid_high = compute_spectral_centroid(high, sample_rate)
        assert centroid_high > centroid_low * 2, (
            f"2 kHz tone centroid {centroid_high:.1f} should be much "
            f"higher than 500 Hz tone centroid {centroid_low:.1f}"
        )

    def test_short_signal_yields_finite_positive_centroid(self) -> None:
        """A short signal (1024 samples ≈ 46 ms at 22050 Hz) is a
        realistic lower bound for streaming TTS chunks. The centroid
        MUST be a finite positive number — coarse FFT resolution at
        small N produces spectral leakage that shifts the centroid
        from the dominant frequency, but the detector only cares
        that the SAME signal produces the SAME centroid (i.e. the
        function is deterministic + non-degenerate)."""
        sample_rate = 22050
        samples = 1024
        t = np.arange(samples) / sample_rate
        signal = (np.sin(2 * np.pi * 1500 * t) * 16384).astype(np.int16)
        centroid = compute_spectral_centroid(signal, sample_rate)
        assert centroid > 0.0
        assert centroid < sample_rate / 2.0  # below Nyquist
        # Determinism — same signal MUST give same centroid every call.
        assert compute_spectral_centroid(signal, sample_rate) == centroid


# ── SpeakerConsistencyMonitor (rolling-window state machine) ────────


class TestSpeakerConsistencyMonitor:
    """Pin the rolling-window contract — RFC §Q2 + §Q3."""

    def test_warm_up_observations_dont_trigger(self) -> None:
        """First N-1 observations populate the window silently."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        for centroid in [2000.0, 2010.0, 1995.0, 2005.0]:
            drift, baseline, ratio = monitor.observe(centroid)
            assert drift is False
            assert baseline == 0.0
            assert ratio == 0.0

    def test_nth_observation_completes_warm_up(self) -> None:
        """Even the Nth observation doesn't trigger if it lands within
        the threshold (the baseline established by the prior N-1
        observations)."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        # Fill the window with 5 samples.
        for centroid in [2000.0, 2010.0, 1995.0, 2005.0, 2000.0]:
            monitor.observe(centroid)
        # 6th observation lands within 5% of the ~2000 Hz baseline.
        drift, baseline, ratio = monitor.observe(2020.0)
        assert drift is False
        # Baseline is the mean of the prior 5 samples.
        assert 1990.0 < baseline < 2010.0
        # Ratio is small (~1%).
        assert ratio < 0.05

    def test_observation_past_threshold_triggers_drift(self) -> None:
        """A centroid >5% above the baseline triggers drift_detected=True."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        # Establish a baseline ≈ 2000 Hz.
        for _ in range(5):
            monitor.observe(2000.0)
        # 2200 Hz = 10 % above 2000 Hz baseline → fires.
        drift, baseline, ratio = monitor.observe(2200.0)
        assert drift is True
        assert abs(baseline - 2000.0) < 1.0
        assert abs(ratio - 0.10) < 0.001

    def test_observation_below_threshold_triggers_drift(self) -> None:
        """Drift detection is symmetric — a centroid >5% BELOW the
        baseline also fires."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        for _ in range(5):
            monitor.observe(2000.0)
        # 1800 Hz = 10 % below 2000 Hz baseline → fires.
        drift, _baseline, ratio = monitor.observe(1800.0)
        assert drift is True
        assert abs(ratio - 0.10) < 0.001

    def test_window_slides_after_each_observation(self) -> None:
        """A single anomalous observation falls out of the baseline
        after ``window_size`` further observations — self-healing
        for transients."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        # 4 normal samples + 1 anomalous = full window with one outlier.
        for centroid in [2000.0, 2000.0, 2000.0, 2000.0, 2400.0]:
            monitor.observe(centroid)
        # 5 more normal samples — the outlier should slide out.
        for _ in range(5):
            monitor.observe(2000.0)
        # The 11th observation at 2000 Hz should NOT trigger
        # because the baseline is now back to ~2000 Hz.
        drift, baseline, _ratio = monitor.observe(2000.0)
        assert drift is False
        assert abs(baseline - 2000.0) < 1.0

    def test_zero_centroid_skipped(self) -> None:
        """``compute_spectral_centroid`` returns 0.0 for empty/silent
        input. The monitor skips such observations — they don't
        populate the window AND don't trigger."""
        monitor = SpeakerConsistencyMonitor(window_size=3, drift_ratio_threshold=0.05)
        drift, baseline, ratio = monitor.observe(0.0)
        assert drift is False
        assert baseline == 0.0
        assert ratio == 0.0
        # Window should still be empty — verify via subsequent
        # warm-up observations.
        for centroid in [2000.0, 2010.0]:
            drift, _, _ = monitor.observe(centroid)
            assert drift is False
        # Third real observation completes the warm-up but doesn't
        # itself trigger.
        drift, _, _ = monitor.observe(2005.0)
        assert drift is False

    def test_negative_centroid_skipped(self) -> None:
        """Defensive — negative centroid is impossible in normal flow
        but if a future caller passes one (eg. signed-int overflow
        in an external caller) the monitor must not crash."""
        monitor = SpeakerConsistencyMonitor(window_size=3, drift_ratio_threshold=0.05)
        drift, baseline, ratio = monitor.observe(-100.0)
        assert drift is False
        assert baseline == 0.0
        assert ratio == 0.0

    def test_reset_clears_window(self) -> None:
        """Reset on session boundary — prior session's baseline must
        not leak into the new session."""
        monitor = SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.05)
        # Fill the window with one voice's centroid.
        for _ in range(5):
            monitor.observe(2000.0)
        monitor.reset()
        # Post-reset, the next 4 observations are warm-up again.
        for centroid in [3000.0, 3000.0, 3000.0, 3000.0]:
            drift, _, _ = monitor.observe(centroid)
            assert drift is False
        # 5th observation at 3000 Hz lands AS the warm-up baseline
        # (not compared to anything). Doesn't trigger.
        drift, _, _ = monitor.observe(3000.0)
        assert drift is False

    def test_window_size_below_2_rejected(self) -> None:
        """Floor 2 — a single-sample baseline would fire on any
        drift, defeating the rolling-window design."""
        with pytest.raises(ValueError, match="window_size must be >= 2"):
            SpeakerConsistencyMonitor(window_size=1, drift_ratio_threshold=0.05)
        with pytest.raises(ValueError, match="window_size must be >= 2"):
            SpeakerConsistencyMonitor(window_size=0, drift_ratio_threshold=0.05)

    def test_threshold_zero_or_negative_rejected(self) -> None:
        """Threshold 0 would fire on any non-zero drift; negative is
        nonsensical."""
        with pytest.raises(ValueError, match="drift_ratio_threshold must be > 0"):
            SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=0.0)
        with pytest.raises(ValueError, match="drift_ratio_threshold must be > 0"):
            SpeakerConsistencyMonitor(window_size=5, drift_ratio_threshold=-0.1)

    def test_custom_threshold_respected(self) -> None:
        """A 1% threshold should fire on a 2% drift (which a 5%
        threshold would silence)."""
        monitor = SpeakerConsistencyMonitor(window_size=3, drift_ratio_threshold=0.01)
        for _ in range(3):
            monitor.observe(2000.0)
        # 2040 Hz = 2% drift — fires under 1% threshold.
        drift, _, ratio = monitor.observe(2040.0)
        assert drift is True
        assert abs(ratio - 0.02) < 0.001
