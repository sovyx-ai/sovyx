"""Context assembly benchmarks: budget allocation, slot assembly throughput."""

from __future__ import annotations

import json
import time

from sovyx.context.budget import BudgetAllocator
from sovyx.context.tokenizer import TokenCounter


def bench_budget_allocation_6_slots() -> dict[str, object]:
    """Benchmark budget allocation across 6 context slots."""
    allocator = BudgetAllocator(total_budget=4096)
    iterations = 1000

    start = time.perf_counter()
    for _ in range(iterations):
        allocator.allocate()
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "budget_allocation_6_slots",
        "iterations": iterations,
        "duration_ms": round(elapsed * 1000, 2),
        "avg_us": round(elapsed / iterations * 1_000_000, 2),
    }


def bench_token_counting_throughput() -> dict[str, object]:
    """Benchmark tokens assembled per second."""
    tc = TokenCounter()
    texts = [f"Context slot {i}: " + "word " * 100 for i in range(6)]
    iterations = 500

    start = time.perf_counter()
    total_tokens = 0
    for _ in range(iterations):
        for text in texts:
            total_tokens += tc.count(text)
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "token_counting_throughput",
        "total_tokens": total_tokens,
        "iterations": iterations,
        "duration_ms": round(elapsed * 1000, 2),
        "tokens_per_sec": round(total_tokens / elapsed),
    }


def run_all() -> list[dict[str, object]]:
    """Run all context benchmarks."""
    return [
        bench_budget_allocation_6_slots(),
        bench_token_counting_throughput(),
    ]


if __name__ == "__main__":
    for r in run_all():
        print(json.dumps(r))
