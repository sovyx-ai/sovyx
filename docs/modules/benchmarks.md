# Module: benchmarks

## What it does

`sovyx.benchmarks` defines performance budgets per hardware tier and tracks regressions against saved baselines. It answers "does this commit make anything measurably slower?" by comparing runtime measurements to per-tier limits and flagging regressions above a configurable tolerance (default 10%).

## Key classes

| Name | Responsibility |
|---|---|
| `HardwareTier` | StrEnum — `pi5`, `n100`, `gpu`. |
| `TierLimits` | Frozen dataclass — 5 threshold limits per tier. |
| `PerformanceBudget` | Validates `BenchmarkResult` against `TierLimits`. |
| `BaselineManager` | Save/load JSON baselines, regression detection. |
| `BenchmarkResult` | Single measurement (name, value, unit). |
| `BudgetCheck` | One check outcome (name, measured, limit, passed, higher_is_better). |
| `ComparisonReport` | Diff between two baselines (for CI). |

## Hardware tiers

| Tier | `startup_ms` | `rss_mb` | `brain_search_ms` | `context_assembly_ms` | `working_memory_ops/s` |
|---|---|---|---|---|---|
| **pi5** | ≤ 5 000 | ≤ 650 | ≤ 100 | ≤ 200 | ≥ 10 000 |
| **n100** | ≤ 3 000 | ≤ 1 024 | ≤ 50 | ≤ 100 | ≥ 50 000 |
| **gpu** | ≤ 2 000 | ≤ 2 048 | ≤ 20 | ≤ 50 | ≥ 100 000 |

## Higher-is-better awareness

Throughput metrics (ops/sec, tokens/sec) are inverted: a *decrease* is a regression. Latency and memory metrics use the standard direction: an *increase* is a regression.

## Baseline tracking

`BaselineManager` saves measurements to `baselines/` as timestamped JSON. Regression detection compares the current run against the latest baseline:

```python
manager = BaselineManager(baselines_dir)
report = manager.compare(current_results)
if not report.all_passed:
    raise RegressionDetected(report)
```

Default tolerance: 10% (e.g., a 95 ms brain search against a 85 ms baseline is a 12% regression and would fail).

## Configuration

No runtime config — tier limits are frozen constants. Tolerance is a parameter to `BaselineManager`.

## See also

- Source: `src/sovyx/benchmarks/`.
- Tests: `tests/unit/test_benchmarks.py`.
- Related: [`observability`](./observability.md) (SLOs track the same metrics at runtime), [`engine`](./engine.md) (hardware tier from `EngineConfig`).
