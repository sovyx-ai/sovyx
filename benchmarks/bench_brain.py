"""Brain benchmarks: working memory, token counting, embedding."""

from __future__ import annotations

import json
import time

from sovyx.brain.working_memory import WorkingMemory
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import ConceptId


def bench_working_memory_activate(n: int = 1000) -> dict[str, object]:
    """Benchmark activating N items in working memory."""
    wm = WorkingMemory(capacity=n)
    start = time.perf_counter()
    for i in range(n):
        wm.activate(ConceptId(f"concept-{i}"), float(i) / n)
    elapsed = time.perf_counter() - start
    return {
        "benchmark": "working_memory_activate",
        "items": n,
        "duration_ms": round(elapsed * 1000, 2),
        "ops_per_sec": round(n / elapsed),
    }


def bench_working_memory_decay(n: int = 1000) -> dict[str, object]:
    """Benchmark decay on N items."""
    wm = WorkingMemory(capacity=n)
    for i in range(n):
        wm.activate(ConceptId(f"concept-{i}"), 1.0)

    start = time.perf_counter()
    wm.decay_all()
    elapsed = time.perf_counter() - start
    return {
        "benchmark": "working_memory_decay",
        "items": n,
        "duration_ms": round(elapsed * 1000, 2),
    }


def bench_token_counter_short() -> dict[str, object]:
    """Benchmark token counting on short text."""
    tc = TokenCounter()
    text = "Hello, how are you doing today?"
    iterations = 1000

    start = time.perf_counter()
    for _ in range(iterations):
        tc.count(text)
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "token_counter_short",
        "iterations": iterations,
        "duration_ms": round(elapsed * 1000, 2),
        "avg_us": round(elapsed / iterations * 1_000_000, 2),
    }


def bench_token_counter_long() -> dict[str, object]:
    """Benchmark token counting on long text (10K chars)."""
    tc = TokenCounter()
    text = "The quick brown fox jumps over the lazy dog. " * 250  # ~11K chars
    iterations = 100

    start = time.perf_counter()
    for _ in range(iterations):
        tc.count(text)
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "token_counter_long",
        "text_chars": len(text),
        "iterations": iterations,
        "duration_ms": round(elapsed * 1000, 2),
        "avg_ms": round(elapsed / iterations * 1000, 2),
    }


def bench_token_counter_batch() -> dict[str, object]:
    """Benchmark token counting on batch of 50 messages."""
    tc = TokenCounter()
    messages = [f"Message number {i}: some content here." for i in range(50)]

    start = time.perf_counter()
    for _ in range(100):
        for msg in messages:
            tc.count(msg)
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "token_counter_batch_50msgs",
        "iterations": 100,
        "duration_ms": round(elapsed * 1000, 2),
        "avg_ms": round(elapsed / 100 * 1000, 2),
    }


def run_all() -> list[dict[str, object]]:
    """Run all brain benchmarks."""
    results = [
        bench_working_memory_activate(100),
        bench_working_memory_activate(1000),
        bench_working_memory_activate(10000),
        bench_working_memory_decay(1000),
        bench_token_counter_short(),
        bench_token_counter_long(),
        bench_token_counter_batch(),
    ]
    return results


if __name__ == "__main__":
    results = run_all()
    for r in results:
        print(json.dumps(r))
