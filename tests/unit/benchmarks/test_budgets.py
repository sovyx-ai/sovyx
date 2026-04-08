"""Tests for PerformanceBudget — tier-based performance validation (V05-40)."""

from __future__ import annotations

from sovyx.benchmarks.budgets import (
    BenchmarkResult,
    HardwareTier,
    PerformanceBudget,
    TierLimits,
)


class TestHardwareTier:
    """Tests for HardwareTier enum."""

    def test_all_tiers(self) -> None:
        assert len(HardwareTier) == 3  # noqa: PLR2004
        assert HardwareTier.PI5.value == "pi5"
        assert HardwareTier.N100.value == "n100"
        assert HardwareTier.GPU.value == "gpu"


class TestBenchmarkResult:
    """Tests for BenchmarkResult dataclass."""

    def test_to_dict(self) -> None:
        r = BenchmarkResult(name="test", value=42.0, unit="ms")
        d = r.to_dict()
        assert d["name"] == "test"
        assert d["value"] == 42.0
        assert d["unit"] == "ms"


class TestPerformanceBudget:
    """Tests for PerformanceBudget class."""

    def test_pi5_tier(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        assert budget.tier == HardwareTier.PI5
        assert budget.limits.startup_ms == 5000  # noqa: PLR2004
        assert budget.limits.rss_mb == 600  # noqa: PLR2004

    def test_n100_tier(self) -> None:
        budget = PerformanceBudget(HardwareTier.N100)
        assert budget.limits.startup_ms == 3000  # noqa: PLR2004
        assert budget.limits.brain_search_ms == 50  # noqa: PLR2004

    def test_gpu_tier(self) -> None:
        budget = PerformanceBudget(HardwareTier.GPU)
        assert budget.limits.startup_ms == 2000  # noqa: PLR2004

    def test_check_within_budget(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="startup_ms", value=3000.0, unit="ms")
        check = budget.check(result)
        assert check is not None
        assert check.passed is True

    def test_check_exceeds_budget(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="startup_ms", value=6000.0, unit="ms")
        check = budget.check(result)
        assert check is not None
        assert check.passed is False

    def test_check_throughput_above(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="working_memory_ops_per_sec", value=20000.0, unit="ops/sec")
        check = budget.check(result)
        assert check is not None
        assert check.passed is True
        assert check.higher_is_better is True

    def test_check_throughput_below(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="working_memory_ops_per_sec", value=5000.0, unit="ops/sec")
        check = budget.check(result)
        assert check is not None
        assert check.passed is False

    def test_check_unknown_metric(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="unknown_metric", value=1.0, unit="x")
        check = budget.check(result)
        assert check is None

    def test_check_all(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        results = [
            BenchmarkResult(name="startup_ms", value=3000.0, unit="ms"),
            BenchmarkResult(name="rss_mb", value=400.0, unit="MB"),
            BenchmarkResult(name="unknown", value=1.0, unit="x"),
        ]
        checks = budget.check_all(results)
        assert len(checks) == 2  # noqa: PLR2004
        assert all(c.passed for c in checks)

    def test_all_passed_true(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        results = [
            BenchmarkResult(name="startup_ms", value=3000.0, unit="ms"),
            BenchmarkResult(name="rss_mb", value=400.0, unit="MB"),
        ]
        assert budget.all_passed(results) is True

    def test_all_passed_false(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        results = [
            BenchmarkResult(name="startup_ms", value=3000.0, unit="ms"),
            BenchmarkResult(name="rss_mb", value=650.0, unit="MB"),  # over 600
        ]
        assert budget.all_passed(results) is False

    def test_all_passed_empty(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        assert budget.all_passed([]) is True

    def test_get_tier_limits(self) -> None:
        limits = PerformanceBudget.get_tier_limits(HardwareTier.GPU)
        assert isinstance(limits, TierLimits)
        assert limits.startup_ms == 2000  # noqa: PLR2004

    def test_create_app_cold_maps_to_startup(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="create_app_cold", value=4000.0, unit="ms")
        check = budget.check(result)
        assert check is not None
        assert check.passed is True

    def test_rss_after_import_maps_to_rss(self) -> None:
        budget = PerformanceBudget(HardwareTier.PI5)
        result = BenchmarkResult(name="rss_after_import", value=300.0, unit="MB")
        check = budget.check(result)
        assert check is not None
        assert check.passed is True
