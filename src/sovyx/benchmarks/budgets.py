"""Performance budgets per hardware tier.

Defines target performance metrics for Pi5, N100, and GPU tiers.
Used by CI gates and benchmark validation.

Ref: SPE-031 §2, ADR-002 (hardware tiers).
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class HardwareTier(Enum):
    """Supported hardware tiers (ADR-002)."""

    PI5 = "pi5"
    N100 = "n100"
    GPU = "gpu"


@dataclasses.dataclass(frozen=True, slots=True)
class TierLimits:
    """Performance limits for a hardware tier.

    Attributes:
        startup_ms: Maximum cold start time in milliseconds.
        rss_mb: Maximum resident set size in megabytes.
        brain_search_ms: Maximum brain FTS5 search latency in ms.
        context_assembly_ms: Maximum context assembly time in ms.
        working_memory_ops_per_sec: Minimum working memory throughput.
    """

    startup_ms: int
    rss_mb: int
    brain_search_ms: int
    context_assembly_ms: int
    working_memory_ops_per_sec: int


# Budget definitions per tier
_TIER_LIMITS: dict[HardwareTier, TierLimits] = {
    HardwareTier.PI5: TierLimits(
        startup_ms=5000,
        rss_mb=512,
        brain_search_ms=100,
        context_assembly_ms=200,
        working_memory_ops_per_sec=10_000,
    ),
    HardwareTier.N100: TierLimits(
        startup_ms=3000,
        rss_mb=1024,
        brain_search_ms=50,
        context_assembly_ms=100,
        working_memory_ops_per_sec=50_000,
    ),
    HardwareTier.GPU: TierLimits(
        startup_ms=2000,
        rss_mb=2048,
        brain_search_ms=20,
        context_assembly_ms=50,
        working_memory_ops_per_sec=100_000,
    ),
}


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """A single benchmark measurement.

    Attributes:
        name: Benchmark name (e.g. ``"brain_search_latency"``).
        value: Measured value.
        unit: Unit of measurement (e.g. ``"ms"``, ``"MB"``, ``"ops/sec"``).
    """

    name: str
    value: float
    unit: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {"name": self.name, "value": self.value, "unit": self.unit}


@dataclasses.dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Result of checking a benchmark against a budget.

    Attributes:
        name: Metric name.
        measured: Actual measured value.
        limit: Budget limit.
        unit: Unit of measurement.
        passed: Whether the check passed.
        higher_is_better: If True, measured must be >= limit (throughput).
    """

    name: str
    measured: float
    limit: float
    unit: str
    passed: bool
    higher_is_better: bool = False


class PerformanceBudget:
    """Validate benchmark results against hardware tier budgets.

    Args:
        tier: Hardware tier to validate against.
    """

    def __init__(self, tier: HardwareTier) -> None:
        self._tier = tier
        self._limits = _TIER_LIMITS[tier]

    @property
    def tier(self) -> HardwareTier:
        """Hardware tier."""
        return self._tier

    @property
    def limits(self) -> TierLimits:
        """Limits for this tier."""
        return self._limits

    def check(self, result: BenchmarkResult) -> BudgetCheck | None:
        """Check a benchmark result against the budget.

        Args:
            result: Benchmark measurement.

        Returns:
            BudgetCheck if a matching budget exists, None otherwise.
        """
        mapping: dict[str, tuple[float, bool]] = {
            "startup_ms": (self._limits.startup_ms, False),
            "create_app_cold": (self._limits.startup_ms, False),
            "rss_mb": (self._limits.rss_mb, False),
            "rss_after_import": (self._limits.rss_mb, False),
            "rss_after_create_app": (self._limits.rss_mb, False),
            "brain_search_ms": (self._limits.brain_search_ms, False),
            "context_assembly_ms": (self._limits.context_assembly_ms, False),
            "budget_allocation_6_slots": (self._limits.context_assembly_ms, False),
            "working_memory_ops_per_sec": (
                self._limits.working_memory_ops_per_sec,
                True,
            ),
        }

        lookup = mapping.get(result.name)
        if lookup is None:
            return None

        limit_val, higher_is_better = lookup
        passed = result.value >= limit_val if higher_is_better else result.value <= limit_val

        return BudgetCheck(
            name=result.name,
            measured=result.value,
            limit=limit_val,
            unit=result.unit,
            passed=passed,
            higher_is_better=higher_is_better,
        )

    def check_all(self, results: list[BenchmarkResult]) -> list[BudgetCheck]:
        """Check all results against the budget.

        Args:
            results: List of benchmark measurements.

        Returns:
            List of budget checks (only for metrics with matching budgets).
        """
        checks: list[BudgetCheck] = []
        for result in results:
            budget_check = self.check(result)
            if budget_check is not None:
                checks.append(budget_check)
        return checks

    def all_passed(self, results: list[BenchmarkResult]) -> bool:
        """Check if all benchmark results are within budget.

        Args:
            results: Benchmark measurements.

        Returns:
            True if all checks pass (or no matching budgets).
        """
        checks = self.check_all(results)
        return all(c.passed for c in checks)

    @staticmethod
    def get_tier_limits(tier: HardwareTier) -> TierLimits:
        """Get limits for a specific tier.

        Raises:
            ValueError: If tier is unknown.
        """
        return _TIER_LIMITS[tier]
