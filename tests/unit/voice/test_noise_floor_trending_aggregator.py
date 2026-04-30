"""Tests for the noise-floor trend aggregator [Phase 4 T4.38].

Coverage:

* :func:`record_noise_floor_sample` + :func:`compute_drift`
  short-vs-long mean computation for representative trends.
* Empty buffer returns ``ready=False`` with zero placeholders.
* ``ready`` gate: long window must be fully populated AND short
  window has ≥25% of capacity.
* Sustained upward drift produces positive ``drift_db``;
  downward drift produces negative.
* :func:`compute_drift` does NOT clear the buffer (read-only —
  unlike :func:`._snr_heartbeat.drain_window_stats`).
* Bounded buffer drops oldest samples on overflow (FIFO).
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._noise_floor_trending import (
    _LONG_WINDOW_SAMPLES,
    _SHORT_WINDOW_SAMPLES,
    compute_drift,
    record_noise_floor_sample,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestEmptyBuffer:
    def test_no_samples_not_ready(self) -> None:
        drift = compute_drift()
        assert drift.short_count == 0
        assert drift.long_count == 0
        assert drift.ready is False
        assert drift.drift_db == 0.0


class TestReadyGate:
    def test_short_window_only_not_ready(self) -> None:
        # Push samples up to a fraction of the short window —
        # baseline (long window) is still empty so ready=False.
        for _ in range(_SHORT_WINDOW_SAMPLES // 4):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.short_count > 0
        assert drift.ready is False

    def test_long_window_underfilled_not_ready(self) -> None:
        # Long window not yet populated: ready=False.
        for _ in range(_LONG_WINDOW_SAMPLES // 2):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.long_count == _LONG_WINDOW_SAMPLES // 2
        assert drift.ready is False

    def test_full_long_window_ready(self) -> None:
        # Fully populated long window → ready=True.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.long_count == _LONG_WINDOW_SAMPLES
        assert drift.short_count == _SHORT_WINDOW_SAMPLES
        assert drift.ready is True


class TestDriftComputation:
    def test_steady_floor_zero_drift(self) -> None:
        # All samples at the same level → drift = 0.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-55.0)
        drift = compute_drift()
        assert drift.short_avg_db == pytest.approx(-55.0)
        assert drift.long_avg_db == pytest.approx(-55.0)
        assert drift.drift_db == pytest.approx(0.0)
        assert drift.ready is True

    def test_upward_drift_positive_delta(self) -> None:
        # First fill the long window with -55 dB.
        baseline_count = _LONG_WINDOW_SAMPLES - _SHORT_WINDOW_SAMPLES
        for _ in range(baseline_count):
            record_noise_floor_sample(noise_floor_db=-55.0)
        # Then top off with -40 dB samples filling the short window.
        for _ in range(_SHORT_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-40.0)
        drift = compute_drift()
        # Short window = last _SHORT_WINDOW_SAMPLES samples = all -40.
        assert drift.short_avg_db == pytest.approx(-40.0)
        # Long window = full buffer; mean ≈ weighted avg of the
        # two regions.
        weighted_avg = (
            baseline_count * -55.0 + _SHORT_WINDOW_SAMPLES * -40.0
        ) / _LONG_WINDOW_SAMPLES
        assert drift.long_avg_db == pytest.approx(weighted_avg)
        assert drift.drift_db > 0  # short > long means upward drift
        assert drift.drift_db == pytest.approx(drift.short_avg_db - drift.long_avg_db)
        assert drift.ready is True

    def test_downward_drift_negative_delta(self) -> None:
        # Inverse: baseline -40 dB then short window of -55 dB.
        baseline_count = _LONG_WINDOW_SAMPLES - _SHORT_WINDOW_SAMPLES
        for _ in range(baseline_count):
            record_noise_floor_sample(noise_floor_db=-40.0)
        for _ in range(_SHORT_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-55.0)
        drift = compute_drift()
        assert drift.drift_db < 0  # short < long means downward
        assert drift.short_avg_db == pytest.approx(-55.0)


class TestReadOnlyContract:
    def test_compute_drift_does_not_clear_buffer(self) -> None:
        # Unlike the SNR drain, compute_drift MUST NOT clear.
        # Two consecutive calls must return identical results.
        for k in range(_LONG_WINDOW_SAMPLES):
            # Fill with a slight ramp so any clear would be
            # detectable (different averages).
            record_noise_floor_sample(noise_floor_db=-50.0 + k * 0.001)
        first = compute_drift()
        second = compute_drift()
        assert first.short_avg_db == pytest.approx(second.short_avg_db)
        assert first.long_avg_db == pytest.approx(second.long_avg_db)
        assert first.drift_db == pytest.approx(second.drift_db)


class TestBufferOverflow:
    def test_overflow_drops_oldest_via_fifo(self) -> None:
        # Push 1.5x the long-window cap — the oldest 0.5x should
        # drop out, leaving only the newest _LONG_WINDOW_SAMPLES.
        # If the cap weren't enforced, drift computation would
        # see all 1.5x samples and produce wrong percentile.
        first_block_count = _LONG_WINDOW_SAMPLES // 2
        for _ in range(first_block_count):
            record_noise_floor_sample(noise_floor_db=-70.0)
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        # The buffer should ONLY hold -50.0 samples (the -70.0
        # block scrolled out via FIFO drop).
        assert drift.long_avg_db == pytest.approx(-50.0)
        assert drift.short_avg_db == pytest.approx(-50.0)
        assert drift.drift_db == pytest.approx(0.0)
