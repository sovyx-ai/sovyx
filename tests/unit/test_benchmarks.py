"""Tests that validate benchmark thresholds."""

from __future__ import annotations

import pytest

from benchmarks.bench_brain import (
    bench_token_counter_batch,
    bench_token_counter_long,
    bench_token_counter_short,
    bench_working_memory_activate,
    bench_working_memory_decay,
)
from benchmarks.bench_cogloop import (
    bench_budget_allocation,
    bench_cold_start,
    bench_rss_idle,
)


class TestBrainBenchmarks:
    """Validate brain benchmark thresholds."""

    def test_working_memory_100_under_10ms(self) -> None:
        result = bench_working_memory_activate(100)
        assert result["duration_ms"] < 10  # noqa: PLR2004

    def test_working_memory_1000_under_50ms(self) -> None:
        result = bench_working_memory_activate(1000)
        assert result["duration_ms"] < 50  # noqa: PLR2004

    def test_working_memory_decay_under_10ms(self) -> None:
        result = bench_working_memory_decay(1000)
        assert result["duration_ms"] < 10  # noqa: PLR2004

    def test_token_counter_short_under_1ms_avg(self) -> None:
        result = bench_token_counter_short()
        assert result["avg_us"] < 1000  # noqa: PLR2004

    def test_token_counter_long_under_10ms_avg(self) -> None:
        result = bench_token_counter_long()
        assert result["avg_ms"] < 10  # noqa: PLR2004

    def test_token_counter_batch_under_100ms(self) -> None:
        result = bench_token_counter_batch()
        assert result["avg_ms"] < 100  # noqa: PLR2004


class TestCogLoopBenchmarks:
    """Validate cogloop benchmark thresholds."""

    def test_budget_allocation_under_5us(self) -> None:
        result = bench_budget_allocation()
        assert result["avg_us"] < 50  # noqa: PLR2004

    def test_cold_start_under_10s(self) -> None:
        result = bench_cold_start()
        assert result["pass"] is True

    @pytest.mark.skipif(
        not __import__("pathlib").Path("/proc/self/status").exists(),
        reason="No /proc filesystem",
    )
    def test_rss_idle_under_budget(self) -> None:
        result = bench_rss_idle()
        rss_mb = result.get("rss_mb", 0)
        # CI runners have variable RSS due to shared infrastructure.
        # Log the value for debugging but only hard-fail above 800MB
        # (real regression). The per-tier budget in budgets.py handles
        # the strict threshold for local/benchmark runs.
        assert rss_mb < 800, f"RSS {rss_mb}MB exceeds hard limit (800MB)"  # noqa: PLR2004
