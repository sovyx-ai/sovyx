"""Baseline management — store, load, and compare benchmark baselines.

Enables CI regression detection: if any metric degrades by more than
the configured tolerance (default 10%), the comparison fails.

Ref: SPE-031 §5 (baseline tracking).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import Any

from sovyx.benchmarks.budgets import BenchmarkResult
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

_REGRESSION_TOLERANCE = 0.10  # 10% regression threshold


class RegressionDetected(Exception):  # noqa: N818
    """Raised when benchmark regression exceeds tolerance."""


@dataclasses.dataclass(frozen=True, slots=True)
class MetricComparison:
    """Comparison of a single metric against baseline.

    Attributes:
        name: Metric name.
        current: Current measured value.
        baseline: Previous baseline value.
        unit: Unit of measurement.
        change_pct: Percentage change from baseline (positive = worse for latency).
        regressed: Whether the change exceeds tolerance.
        higher_is_better: If True, decrease is regression.
    """

    name: str
    current: float
    baseline: float
    unit: str
    change_pct: float
    regressed: bool
    higher_is_better: bool = False


@dataclasses.dataclass
class ComparisonReport:
    """Aggregated comparison report.

    Attributes:
        comparisons: Individual metric comparisons.
        has_regressions: Whether any metric regressed beyond tolerance.
        timestamp: When the comparison was performed.
    """

    comparisons: list[MetricComparison] = dataclasses.field(default_factory=list)
    has_regressions: bool = False
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "has_regressions": self.has_regressions,
            "timestamp": self.timestamp,
            "comparisons": [
                {
                    "name": c.name,
                    "current": c.current,
                    "baseline": c.baseline,
                    "unit": c.unit,
                    "change_pct": round(c.change_pct, 2),
                    "regressed": c.regressed,
                }
                for c in self.comparisons
            ],
        }


# Metrics where higher values are better (throughput, ops/sec)
_HIGHER_IS_BETTER: set[str] = {
    "working_memory_ops_per_sec",
    "tokens_per_sec",
    "ops_per_sec",
}


class BaselineManager:
    """Manage benchmark baselines for regression detection.

    Baselines are stored as JSON files in a directory. Each baseline
    contains timestamped metric values.

    Args:
        baselines_dir: Directory for baseline JSON files.
        tolerance: Regression tolerance (default 10%).
    """

    def __init__(
        self,
        baselines_dir: Path,
        *,
        tolerance: float = _REGRESSION_TOLERANCE,
    ) -> None:
        self._dir = baselines_dir
        self._tolerance = tolerance

    @property
    def baselines_dir(self) -> Path:
        """Directory where baselines are stored."""
        return self._dir

    @property
    def tolerance(self) -> float:
        """Regression tolerance (fraction, e.g. 0.10 = 10%)."""
        return self._tolerance

    def save_baseline(self, results: list[BenchmarkResult]) -> Path:
        """Save benchmark results as a new baseline.

        Args:
            results: Benchmark measurements to save.

        Returns:
            Path to the saved baseline file.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(UTC)
        filename = f"baseline-{now.strftime('%Y%m%d-%H%M%S')}.json"
        filepath = self._dir / filename

        data = {
            "timestamp": now.isoformat(),
            "metrics": [r.to_dict() for r in results],
        }

        filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("baseline_saved", path=str(filepath), metrics=len(results))

        # Update latest symlink / file
        latest = self._dir / "latest.json"
        latest.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return filepath

    def load_baseline(self, path: Path | None = None) -> list[BenchmarkResult] | None:
        """Load a baseline from file.

        Args:
            path: Specific baseline file. If None, loads ``latest.json``.

        Returns:
            List of BenchmarkResult, or None if no baseline exists.
        """
        target = path or (self._dir / "latest.json")

        if not target.exists():
            return None

        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("baseline_load_failed", path=str(target), error=str(exc))
            return None

        metrics = data.get("metrics", [])
        results: list[BenchmarkResult] = []
        for m in metrics:
            if isinstance(m, dict) and "name" in m and "value" in m:
                results.append(
                    BenchmarkResult(
                        name=m["name"],
                        value=float(m["value"]),
                        unit=m.get("unit", ""),
                    )
                )

        return results if results else None

    def compare(
        self,
        current: list[BenchmarkResult],
        baseline: list[BenchmarkResult] | None = None,
    ) -> ComparisonReport:
        """Compare current results against a baseline.

        Args:
            current: Current benchmark measurements.
            baseline: Previous baseline. If None, loads from latest.json.

        Returns:
            ComparisonReport with per-metric comparisons.

        Raises:
            RegressionDetected: If any metric regresses beyond tolerance.
        """
        if baseline is None:
            baseline = self.load_baseline()

        if baseline is None:
            # No baseline to compare against — save current as first baseline
            self.save_baseline(current)
            return ComparisonReport(
                timestamp=datetime.now(UTC).isoformat(),
            )

        baseline_map = {r.name: r for r in baseline}
        comparisons: list[MetricComparison] = []

        for result in current:
            base = baseline_map.get(result.name)
            if base is None:
                continue

            higher_better = result.name in _HIGHER_IS_BETTER

            if base.value == 0:
                change_pct = 0.0
            elif higher_better:
                # For throughput: decrease is bad
                change_pct = (base.value - result.value) / base.value
            else:
                # For latency: increase is bad
                change_pct = (result.value - base.value) / base.value

            regressed = change_pct > self._tolerance

            comparisons.append(
                MetricComparison(
                    name=result.name,
                    current=result.value,
                    baseline=base.value,
                    unit=result.unit,
                    change_pct=change_pct * 100,
                    regressed=regressed,
                    higher_is_better=higher_better,
                )
            )

        has_regressions = any(c.regressed for c in comparisons)
        report = ComparisonReport(
            comparisons=comparisons,
            has_regressions=has_regressions,
            timestamp=datetime.now(UTC).isoformat(),
        )

        if has_regressions:
            regressed_names = [c.name for c in comparisons if c.regressed]
            msg = f"Performance regression detected: {', '.join(regressed_names)}"
            logger.warning("regression_detected", metrics=regressed_names)
            raise RegressionDetected(msg)

        return report
