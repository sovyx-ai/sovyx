"""Tests for the recent-SNR rolling aggregator [Phase 4 T4.36].

Coverage:

* :func:`record_sample` + :func:`window_summary` percentile
  computation across representative distributions.
* Empty buffer returns count=0 with zero placeholder p50 (the
  orchestrator gates the confidence factor on count>0).
* :func:`window_summary` is read-only — two consecutive calls
  with no new samples return identical results.
* Bounded buffer drops oldest samples on overflow (FIFO).
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._recent_snr import (
    _WINDOW_SAMPLES,
    record_sample,
    reset_for_tests,
    window_summary,
)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestEmpty:
    def test_no_samples_returns_zero_count(self) -> None:
        s = window_summary()
        assert s.count == 0
        assert s.p50_db == 0.0


class TestPercentiles:
    def test_single_sample_p50_equals_value(self) -> None:
        record_sample(snr_db=12.5)
        s = window_summary()
        assert s.count == 1
        assert s.p50_db == 12.5

    def test_uniform_distribution_p50_picks_middle(self) -> None:
        # 1, 2, …, 11. Median idx 11//2 = 5 → value 6.
        for k in range(1, 12):
            record_sample(snr_db=float(k))
        s = window_summary()
        assert s.count == 11  # noqa: PLR2004
        assert s.p50_db == 6.0  # noqa: PLR2004

    def test_unsorted_input_sorted_internally(self) -> None:
        for v in [50.0, 10.0, 80.0, 30.0, 60.0]:
            record_sample(snr_db=v)
        s = window_summary()
        # Sorted [10, 30, 50, 60, 80]; idx 2 → 50.
        assert s.p50_db == 50.0


class TestReadOnly:
    def test_two_consecutive_summaries_identical(self) -> None:
        # window_summary is READ-ONLY (unlike the heartbeat drain).
        for v in (5.0, 12.0, 18.0, 25.0):
            record_sample(snr_db=v)
        a = window_summary()
        b = window_summary()
        assert a.p50_db == b.p50_db
        assert a.count == b.count


class TestBufferOverflow:
    def test_overflow_drops_oldest_via_fifo(self) -> None:
        # Push 1.5x the window cap with monotonically increasing
        # values; the surviving samples are the most recent N.
        total = _WINDOW_SAMPLES + (_WINDOW_SAMPLES // 2)
        for k in range(total):
            record_sample(snr_db=float(k))
        s = window_summary()
        assert s.count == _WINDOW_SAMPLES
        # Survived: [total-N, …, total-1]. Median idx N//2 →
        # value (total - N) + N//2.
        expected_p50 = float(total - (_WINDOW_SAMPLES - _WINDOW_SAMPLES // 2))
        assert s.p50_db == expected_p50
