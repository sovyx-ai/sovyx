"""Tests for the SNR heartbeat aggregator [Phase 4 T4.34].

Coverage:

* :func:`record_snr_sample` + :func:`drain_window_stats`
  percentile correctness for representative sample distributions.
* Empty drain returns count=0 with zero placeholders (orchestrator
  uses count==0 to OMIT the heartbeat fields, not to log zeros).
* Drain clears the buffer atomically — two consecutive drains
  with no intermediate samples produce identical zero results.
* Bounded buffer drops oldest samples on overflow (FIFO via
  ``deque(maxlen=...)``); the percentile computation reflects
  only the surviving samples.
* Thread safety: concurrent record + drain produces no
  exceptions (smoke test, not a stress test).
"""

from __future__ import annotations

import threading

import pytest

from sovyx.voice.health._snr_heartbeat import (
    _MAX_BUFFER_SAMPLES,
    drain_window_stats,
    record_snr_sample,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    """Each test starts with an empty buffer."""
    reset_for_tests()
    yield
    reset_for_tests()


class TestDrainEmpty:
    def test_no_samples_returns_zero_count(self) -> None:
        stats = drain_window_stats()
        assert stats.count == 0
        assert stats.p50_db == 0.0
        assert stats.p95_db == 0.0

    def test_two_consecutive_drains_both_empty(self) -> None:
        first = drain_window_stats()
        second = drain_window_stats()
        assert first.count == 0
        assert second.count == 0


class TestPercentileComputation:
    def test_single_sample_p50_equals_p95(self) -> None:
        record_snr_sample(snr_db=12.5)
        stats = drain_window_stats()
        assert stats.count == 1
        assert stats.p50_db == 12.5
        assert stats.p95_db == 12.5

    def test_two_samples_p50_picks_upper_p95_picks_max(self) -> None:
        # count=2, count//2=1 → p50 = samples[1] (sorted upper).
        # count*0.95=1.9 → int=1 → p95 = samples[1] (the max).
        record_snr_sample(snr_db=5.0)
        record_snr_sample(snr_db=20.0)
        stats = drain_window_stats()
        assert stats.count == 2
        assert stats.p50_db == 20.0
        assert stats.p95_db == 20.0

    def test_uniform_distribution_p95_picks_top_5pct(self) -> None:
        # 100 samples 1, 2, …, 100. Median index = 100//2 = 50 →
        # value 51. p95 index = int(100*0.95) = 95 → value 96.
        for k in range(1, 101):
            record_snr_sample(snr_db=float(k))
        stats = drain_window_stats()
        assert stats.count == 100  # noqa: PLR2004
        assert stats.p50_db == 51.0  # noqa: PLR2004
        assert stats.p95_db == 96.0  # noqa: PLR2004

    def test_unsorted_input_still_sorted_internally(self) -> None:
        # The aggregator must sort before computing percentiles —
        # FrameNormalizer feeds samples in arrival (not sorted)
        # order.
        for v in [50.0, 10.0, 80.0, 30.0, 60.0]:
            record_snr_sample(snr_db=v)
        stats = drain_window_stats()
        # Sorted: [10, 30, 50, 60, 80]. Median idx=2 → 50.
        # p95 idx = min(4, int(5*0.95)) = min(4, 4) = 4 → 80.
        assert stats.p50_db == 50.0
        assert stats.p95_db == 80.0

    def test_negative_and_high_values_pass_through(self) -> None:
        # Below 0 dB SNR (degraded mic) and > 60 dB (very clean
        # signal at the SNR floor) are both valid; the aggregator
        # does not clamp.
        record_snr_sample(snr_db=-5.0)
        record_snr_sample(snr_db=70.0)
        stats = drain_window_stats()
        assert stats.count == 2  # noqa: PLR2004
        # Sorted: [-5, 70]. p50 idx=1 → 70. p95 idx=1 → 70.
        assert stats.p50_db == 70.0
        assert stats.p95_db == 70.0


class TestDrainSemantics:
    def test_drain_clears_buffer(self) -> None:
        for v in [10.0, 20.0, 30.0]:
            record_snr_sample(snr_db=v)
        first = drain_window_stats()
        assert first.count == 3  # noqa: PLR2004

        # Without new samples, the next drain is empty.
        second = drain_window_stats()
        assert second.count == 0

    def test_consecutive_windows_isolated(self) -> None:
        # Window 1: 5 samples around 15 dB.
        for v in [12.0, 14.0, 15.0, 16.0, 18.0]:
            record_snr_sample(snr_db=v)
        first = drain_window_stats()
        assert first.count == 5  # noqa: PLR2004
        assert first.p50_db == 15.0  # noqa: PLR2004

        # Window 2: 3 samples around 5 dB. The first window's
        # median (15) MUST NOT contaminate the second window.
        for v in [3.0, 5.0, 7.0]:
            record_snr_sample(snr_db=v)
        second = drain_window_stats()
        assert second.count == 3  # noqa: PLR2004
        assert second.p50_db == 5.0  # noqa: PLR2004


class TestBufferOverflow:
    def test_overflow_drops_oldest_via_fifo(self) -> None:
        # Push twice the buffer cap with monotonically increasing
        # values. The deque(maxlen=N) semantics drop the OLDEST
        # so the surviving window is the most recent N samples.
        total = _MAX_BUFFER_SAMPLES * 2
        for k in range(total):
            record_snr_sample(snr_db=float(k))
        stats = drain_window_stats()
        # Survived samples: [total-N, total-N+1, …, total-1].
        assert stats.count == _MAX_BUFFER_SAMPLES
        # Median = surviving sample at idx N//2 = (total-N) +
        # (N//2) = total - N + N//2 = total - (N - N//2).
        expected_p50 = float(total - (_MAX_BUFFER_SAMPLES - _MAX_BUFFER_SAMPLES // 2))
        assert stats.p50_db == expected_p50
        # Max of surviving = total - 1; p95 idx = int(N * 0.95).
        expected_p95 = float((total - _MAX_BUFFER_SAMPLES) + int(_MAX_BUFFER_SAMPLES * 0.95))
        assert stats.p95_db == expected_p95


class TestThreadSafety:
    def test_concurrent_record_and_drain_no_exception(self) -> None:
        # Smoke test: 4 producer threads append samples while one
        # drainer drains repeatedly. The lock contract is "no
        # exceptions, no lost samples beyond the FIFO cap"; we
        # assert only the no-exception part because exact sample
        # accounting requires deterministic timing.
        stop = threading.Event()
        errors: list[BaseException] = []

        def _producer(start: float) -> None:
            try:
                k = 0
                while not stop.is_set():
                    record_snr_sample(snr_db=start + k * 0.01)
                    k += 1
            except BaseException as exc:  # noqa: BLE001 — smoke test surfaces all
                errors.append(exc)

        def _drainer() -> None:
            try:
                for _ in range(50):
                    drain_window_stats()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        producers = [threading.Thread(target=_producer, args=(float(i * 100),)) for i in range(4)]
        drainer = threading.Thread(target=_drainer)
        for p in producers:
            p.start()
        drainer.start()

        drainer.join(timeout=5.0)
        stop.set()
        for p in producers:
            p.join(timeout=5.0)

        assert errors == []
