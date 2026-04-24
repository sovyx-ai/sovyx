"""Unit tests for ``scripts/check_perf_regression.py``.

The production path of this script is "run the observability benchmark
N times, compute median p99 per config, compare ratios against budget".
The benchmark subprocess itself is slow (seconds per run) so these
tests inject already-synthesised benchmark outputs and exercise the
pure logic:

* ``_median_p99s`` — correct median across N runs, raises on missing
  benchmark entries.
* ``_check`` — no-violation on clean inputs, reports every individual
  budget breach, picks the right wording for the median-of-N framing.

No subprocess is invoked; the tests are millisecond-fast and run on
every platform (the production script is Linux-only in CI, but its
internals are pure Python).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_perf_regression.py"


def _load_script_module() -> ModuleType:
    """Load ``scripts/check_perf_regression.py`` as an importable module.

    The scripts/ directory is not a package, so we load by file path.
    Caches on ``sys.modules`` so pytest collection doesn't re-import
    on every test.
    """
    name = "_sovyx_check_perf_regression_testshim"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"failed to build spec for {_SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _entry(benchmark: str, p99_us: float, p50_us: float = 100.0) -> dict[str, Any]:
    return {
        "benchmark": benchmark,
        "p50_us": p50_us,
        "p95_us": p99_us * 0.9,
        "p99_us": p99_us,
        "mean_us": p50_us,
        "samples": 20000.0,
    }


def _run(minimal: float, redacted: float, async_: float) -> list[dict[str, Any]]:
    return [
        _entry("logging.emit.minimal", minimal),
        _entry("logging.emit.redacted", redacted),
        _entry("logging.emit.async", async_),
    ]


# ---------------------------------------------------------------------------
# _median_p99s
# ---------------------------------------------------------------------------


class TestMedianP99s:
    def test_single_run_returns_that_runs_values(self) -> None:
        script = _load_script_module()
        runs = [_run(100.0, 200.0, 150.0)]
        medians = script._median_p99s(runs)  # noqa: SLF001
        assert medians == {
            "logging.emit.minimal": 100.0,
            "logging.emit.redacted": 200.0,
            "logging.emit.async": 150.0,
        }

    def test_three_runs_take_statistical_median(self) -> None:
        script = _load_script_module()
        runs = [
            _run(100.0, 200.0, 150.0),
            _run(110.0, 210.0, 160.0),
            _run(90.0, 190.0, 140.0),
        ]
        medians = script._median_p99s(runs)  # noqa: SLF001
        # Median across three — the middle value per benchmark.
        assert medians["logging.emit.minimal"] == 100.0
        assert medians["logging.emit.redacted"] == 200.0
        assert medians["logging.emit.async"] == 150.0

    def test_single_noisy_run_discarded_by_median(self) -> None:
        script = _load_script_module()
        # The CI failure pattern: two clean runs + one outlier for async.
        runs = [
            _run(200.0, 250.0, 190.0),
            _run(190.0, 245.0, 185.0),
            _run(189.3, 227.2, 698.0),  # <-- the real CI failure
        ]
        medians = script._median_p99s(runs)  # noqa: SLF001
        # Median for async is 190.0 (the middle value), NOT 698.0.
        # The previous single-run gate would have taken 698.0 and fired.
        assert medians["logging.emit.async"] == 190.0

    def test_missing_entry_raises(self) -> None:
        script = _load_script_module()
        # Delete the async entry from the single run.
        runs = [_run(100.0, 200.0, 150.0)]
        runs[0].pop()  # drop async
        with pytest.raises(KeyError, match="logging.emit.async"):
            script._median_p99s(runs)  # noqa: SLF001


# ---------------------------------------------------------------------------
# _check
# ---------------------------------------------------------------------------


class TestCheckClean:
    def test_empty_runs_list_reports_violation(self) -> None:
        script = _load_script_module()
        assert script._check([]) == [  # noqa: SLF001
            "no benchmark runs were collected",
        ]

    def test_clean_single_run_passes(self) -> None:
        script = _load_script_module()
        runs = [_run(200.0, 300.0, 220.0)]
        assert script._check(runs) == []  # noqa: SLF001

    def test_clean_three_runs_passes(self) -> None:
        script = _load_script_module()
        runs = [
            _run(200.0, 300.0, 220.0),
            _run(210.0, 320.0, 230.0),
            _run(195.0, 290.0, 215.0),
        ]
        assert script._check(runs) == []  # noqa: SLF001

    def test_ci_failure_pattern_passes_with_median(self) -> None:
        """The exact p99s from the failing CI run, plus two clean runs.

        With the original single-run gate this was a FAIL
        (async p99 = 698.0, async/minimal = 3.69×). With median-of-3
        the outlier is discarded — we want this test to PASS so the
        gate stops firing on noise.
        """
        script = _load_script_module()
        runs = [
            _run(200.0, 230.0, 195.0),
            _run(205.0, 235.0, 190.0),
            _run(189.3, 227.2, 698.0),  # <-- the CI flake
        ]
        assert script._check(runs) == []  # noqa: SLF001


class TestCheckRatioViolations:
    def test_async_ratio_exceeded_in_all_runs_fails(self) -> None:
        script = _load_script_module()
        # Every run shows async at 3× minimal — median is 3×, gate fires.
        runs = [
            _run(100.0, 200.0, 300.0),
            _run(110.0, 210.0, 330.0),
            _run(90.0, 190.0, 270.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 1
        assert "async/minimal" in violations[0]
        assert "3.00×" in violations[0]

    def test_redacted_ratio_exceeded_in_all_runs_fails(self) -> None:
        script = _load_script_module()
        # Redacted runs at 4× minimal; budget is 3×.
        runs = [
            _run(100.0, 400.0, 150.0),
            _run(110.0, 440.0, 160.0),
            _run(90.0, 360.0, 140.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 1
        assert "redacted/minimal" in violations[0]
        assert "4.00×" in violations[0]

    def test_both_ratios_exceeded_fails_with_two_violations(self) -> None:
        script = _load_script_module()
        runs = [
            _run(100.0, 400.0, 300.0),
            _run(110.0, 440.0, 330.0),
            _run(90.0, 360.0, 270.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 2

    def test_absolute_ceiling_breached(self) -> None:
        script = _load_script_module()
        # All three runs have minimal p99 above 10 ms — catastrophic.
        runs = [
            _run(11_000.0, 12_000.0, 10_500.0),
            _run(11_100.0, 12_100.0, 10_600.0),
            _run(10_900.0, 11_900.0, 10_400.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        # All three configs tripped the absolute ceiling.
        assert len(violations) == 3
        for line in violations:
            assert "absolute ceiling" in line


class TestCheckMessageFraming:
    def test_violation_message_mentions_run_count(self) -> None:
        """The median-of-N wording is part of the gate's user
        experience — a contributor reading the failure should see
        whether this is a single-run or multi-run median.
        """
        script = _load_script_module()
        runs = [
            _run(100.0, 200.0, 300.0),  # async ratio 3×
            _run(100.0, 200.0, 300.0),
            _run(100.0, 200.0, 300.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert violations
        assert "across 3 runs" in violations[0]
        assert "median" in violations[0].lower()
