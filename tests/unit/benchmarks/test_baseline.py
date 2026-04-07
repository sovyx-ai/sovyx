"""Tests for BaselineManager — save, load, compare benchmarks (V05-41)."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003

import pytest

from sovyx.benchmarks.baseline import (
    BaselineManager,
    ComparisonReport,
    MetricComparison,
    RegressionDetected,
)
from sovyx.benchmarks.budgets import BenchmarkResult


class TestSaveLoad:
    """Tests for saving and loading baselines."""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        results = [BenchmarkResult("test", 42.0, "ms")]
        path = mgr.save_baseline(results)
        assert path.exists()
        assert "baseline-" in path.name

    def test_save_creates_latest(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        results = [BenchmarkResult("test", 42.0, "ms")]
        mgr.save_baseline(results)
        latest = tmp_path / "baselines" / "latest.json"
        assert latest.exists()

    def test_load_latest(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        results = [BenchmarkResult("test", 42.0, "ms")]
        mgr.save_baseline(results)
        loaded = mgr.load_baseline()
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].name == "test"
        assert loaded[0].value == 42.0

    def test_load_specific_path(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        results = [BenchmarkResult("test", 42.0, "ms")]
        path = mgr.save_baseline(results)
        loaded = mgr.load_baseline(path)
        assert loaded is not None
        assert loaded[0].value == 42.0

    def test_load_no_baseline(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        assert mgr.load_baseline() is None

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        baselines_dir = tmp_path / "baselines"
        baselines_dir.mkdir()
        latest = baselines_dir / "latest.json"
        latest.write_text("not valid json{{{")
        mgr = BaselineManager(baselines_dir)
        assert mgr.load_baseline() is None

    def test_load_empty_metrics(self, tmp_path: Path) -> None:
        baselines_dir = tmp_path / "baselines"
        baselines_dir.mkdir()
        latest = baselines_dir / "latest.json"
        latest.write_text(json.dumps({"metrics": []}))
        mgr = BaselineManager(baselines_dir)
        assert mgr.load_baseline() is None

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        results = [
            BenchmarkResult("latency", 50.0, "ms"),
            BenchmarkResult("throughput", 10000.0, "ops/sec"),
        ]
        mgr.save_baseline(results)
        loaded = mgr.load_baseline()
        assert loaded is not None
        assert len(loaded) == 2  # noqa: PLR2004
        assert loaded[0].name == "latency"
        assert loaded[1].name == "throughput"


class TestCompare:
    """Tests for baseline comparison."""

    def test_no_regression(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 50.0, "ms")]
        current = [BenchmarkResult("latency", 52.0, "ms")]  # 4% slower — OK
        report = mgr.compare(current, baseline)
        assert report.has_regressions is False
        assert len(report.comparisons) == 1

    def test_regression_detected(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 50.0, "ms")]
        current = [BenchmarkResult("latency", 60.0, "ms")]  # 20% slower
        with pytest.raises(RegressionDetected, match="latency"):
            mgr.compare(current, baseline)

    def test_improvement_no_regression(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 50.0, "ms")]
        current = [BenchmarkResult("latency", 40.0, "ms")]  # 20% faster
        report = mgr.compare(current, baseline)
        assert report.has_regressions is False

    def test_throughput_regression(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("ops_per_sec", 10000.0, "ops/sec")]
        current = [BenchmarkResult("ops_per_sec", 8000.0, "ops/sec")]  # 20% less
        with pytest.raises(RegressionDetected, match="ops_per_sec"):
            mgr.compare(current, baseline)

    def test_throughput_improvement(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("ops_per_sec", 10000.0, "ops/sec")]
        current = [BenchmarkResult("ops_per_sec", 12000.0, "ops/sec")]  # 20% more
        report = mgr.compare(current, baseline)
        assert report.has_regressions is False

    def test_equal_values(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 50.0, "ms")]
        current = [BenchmarkResult("latency", 50.0, "ms")]
        report = mgr.compare(current, baseline)
        assert report.has_regressions is False

    def test_zero_baseline(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 0.0, "ms")]
        current = [BenchmarkResult("latency", 5.0, "ms")]
        report = mgr.compare(current, baseline)
        assert report.has_regressions is False  # 0 baseline = no change

    def test_no_baseline_saves_current(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        current = [BenchmarkResult("latency", 50.0, "ms")]
        report = mgr.compare(current)
        assert report.has_regressions is False
        # Should have saved as first baseline
        assert mgr.load_baseline() is not None

    def test_new_metric_ignored(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines")
        baseline = [BenchmarkResult("latency", 50.0, "ms")]
        current = [
            BenchmarkResult("latency", 50.0, "ms"),
            BenchmarkResult("new_metric", 100.0, "x"),
        ]
        report = mgr.compare(current, baseline)
        assert len(report.comparisons) == 1  # only latency compared

    def test_custom_tolerance(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "baselines", tolerance=0.05)
        assert mgr.tolerance == 0.05  # noqa: PLR2004
        baseline = [BenchmarkResult("latency", 100.0, "ms")]
        current = [BenchmarkResult("latency", 108.0, "ms")]  # 8% — over 5%
        with pytest.raises(RegressionDetected):
            mgr.compare(current, baseline)


class TestComparisonReport:
    """Tests for ComparisonReport."""

    def test_to_dict(self) -> None:
        report = ComparisonReport(
            comparisons=[
                MetricComparison(
                    name="latency",
                    current=55.0,
                    baseline=50.0,
                    unit="ms",
                    change_pct=10.0,
                    regressed=True,
                ),
            ],
            has_regressions=True,
            timestamp="2026-04-07T19:00:00Z",
        )
        d = report.to_dict()
        assert d["has_regressions"] is True
        assert len(d["comparisons"]) == 1
        assert d["comparisons"][0]["change_pct"] == 10.0

    def test_to_dict_json_serializable(self) -> None:
        report = ComparisonReport(timestamp="2026-04-07T19:00:00Z")
        j = json.dumps(report.to_dict())
        assert "has_regressions" in j


class TestProperties:
    """Tests for BaselineManager properties."""

    def test_baselines_dir(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path / "bl")
        assert mgr.baselines_dir == tmp_path / "bl"

    def test_default_tolerance(self, tmp_path: Path) -> None:
        mgr = BaselineManager(tmp_path)
        assert mgr.tolerance == 0.10  # noqa: PLR2004
