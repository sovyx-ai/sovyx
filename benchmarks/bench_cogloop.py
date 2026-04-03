"""CogLoop benchmarks: context assembly, budget allocation, cold start."""

from __future__ import annotations

import json
import time

from sovyx.context.budget import TokenBudgetManager


def bench_budget_allocation() -> dict[str, object]:
    """Benchmark budget allocation speed."""
    mgr = TokenBudgetManager()
    iterations = 10000

    start = time.perf_counter()
    for i in range(iterations):
        mgr.allocate(
            context_window=128000,
            conversation_length=i % 100,
            brain_result_count=i % 50,
            complexity=0.5,
        )
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "budget_allocation",
        "iterations": iterations,
        "duration_ms": round(elapsed * 1000, 2),
        "avg_us": round(elapsed / iterations * 1_000_000, 2),
    }


def bench_budget_various_windows() -> dict[str, object]:
    """Benchmark allocation across different context windows."""
    mgr = TokenBudgetManager()
    windows = [2048, 4096, 8192, 32768, 128000, 200000]
    timings: dict[int, float] = {}

    for w in windows:
        start = time.perf_counter()
        for _ in range(1000):
            mgr.allocate(
                context_window=w,
                conversation_length=20,
                brain_result_count=10,
            )
        elapsed = time.perf_counter() - start
        timings[w] = round(elapsed / 1000 * 1_000_000, 2)

    return {
        "benchmark": "budget_various_windows",
        "timings_us_per_call": timings,
    }


def bench_cold_start() -> dict[str, object]:
    """Benchmark import + instantiation time."""
    start = time.perf_counter()

    # Simulate cold start: import key modules
    from sovyx.brain.working_memory import WorkingMemory  # noqa: F811
    from sovyx.context.budget import TokenBudgetManager  # noqa: F811
    from sovyx.context.tokenizer import TokenCounter  # noqa: F811
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.events import EventBus
    from sovyx.engine.registry import ServiceRegistry

    # Instantiate core components
    _config = EngineConfig()
    _bus = EventBus()
    _registry = ServiceRegistry()
    _tc = TokenCounter()
    _wm = WorkingMemory()
    _budget = TokenBudgetManager()

    elapsed = time.perf_counter() - start

    return {
        "benchmark": "cold_start_imports",
        "duration_ms": round(elapsed * 1000, 2),
        "pass": elapsed < 10.0,
        "threshold_s": 10.0,
    }


def bench_rss_idle() -> dict[str, object]:
    """Measure RSS memory after basic import."""
    import os

    # Read RSS from /proc
    with open(f"/proc/{os.getpid()}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                rss_mb = rss_kb / 1024
                return {
                    "benchmark": "rss_idle",
                    "rss_mb": round(rss_mb, 1),
                    "pass": rss_mb < 512,
                    "threshold_mb": 512,
                }

    return {"benchmark": "rss_idle", "error": "Could not read RSS"}


def run_all() -> list[dict[str, object]]:
    """Run all cogloop benchmarks."""
    return [
        bench_budget_allocation(),
        bench_budget_various_windows(),
        bench_cold_start(),
        bench_rss_idle(),
    ]


if __name__ == "__main__":
    results = run_all()
    for r in results:
        print(json.dumps(r))
