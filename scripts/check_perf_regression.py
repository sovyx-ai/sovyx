"""CI gate — assert observability hot-path latency stays within budget.

Stand-alone enforcement of §23 of IMPL-OBSERVABILITY-001 ("perf
budgets"). Runs ``benchmarks/bench_observability.py`` to measure the
per-emit latency of the structlog pipeline under three configurations
(minimal / redacted / async), then enforces two complementary checks
against the resulting p99 latencies:

  1. **Absolute ceiling** — a generous upper bound (default 10 ms p99)
     that catches catastrophic regressions where a refactor turned the
     pipeline 100× slower. The exact wall-clock floor varies wildly
     between developer laptops and shared CI runners, so the absolute
     check is intentionally loose; it exists only to fail on
     order-of-magnitude regressions.

  2. **Self-relative ratio** — the ratio of redacted-pipeline latency
     to minimal-pipeline latency (and async-pipeline to minimal). The
     PII redactor and clamp processors should add bounded overhead;
     if redacted/minimal grows past 3.0× the chain has either
     introduced a quadratic processor or accidentally widened the
     critical section. Likewise async/minimal > 2.0× means the queue
     enqueue path lost its ``put_nowait`` fast path.

The self-relative ratio is the *real* gate — it's invariant under CPU
speed, runner contention, and noisy neighbours. The absolute ceiling
is a cheap defence against the case where ALL three configs got
catastrophically slower together (so the ratio still looks fine).

Wired into ``.github/workflows/ci.yml`` as the
``perf-regression-gate`` job after ``metrics-cardinality-gate``.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 — controlled subprocess of our own benchmark.
import sys
from pathlib import Path
from typing import Any

# ── Budgets ───────────────────────────────────────────────────────────
# The redactor adds ~50 µs of work per record on a developer laptop;
# 3× headroom gives CI runners ample slack while still catching a
# regression where the chain accidentally grew quadratic. Async should
# be FASTER than minimal (queue enqueue beats direct file write), so
# 2× the minimal floor is already a generous regression ceiling.
_REDACTED_VS_MINIMAL_MAX: float = 3.0
_ASYNC_VS_MINIMAL_MAX: float = 2.0

# Order-of-magnitude sanity floor in microseconds. Any modern x86 CPU
# emits a JSON log line in well under 1 ms; 10 ms p99 means something
# has gone catastrophically wrong (sync IO on every record, infinite
# retry, lock contention) and warrants a hard fail regardless of the
# self-relative ratio.
_ABSOLUTE_P99_CEILING_US: float = 10_000.0

# Default benchmark sample count — large enough for a stable p99 but
# fast enough that the CI step finishes in a few seconds. The CI job
# may override via ``--iterations``.
_DEFAULT_BENCH_ITERATIONS: int = 20_000


def _run_benchmark(*, iterations: int, repo_root: Path) -> list[dict[str, Any]]:
    """Spawn the benchmark script and return its parsed JSON output.

    We invoke the benchmark via ``subprocess`` (instead of importing
    it) so the measurement runs in a fresh interpreter with no warm
    caches from the gate's own setup. That keeps the percentiles
    representative of a cold daemon process.
    """
    bench = repo_root / "benchmarks" / "bench_observability.py"
    if not bench.is_file():
        msg = f"benchmark script not found: {bench}"
        raise FileNotFoundError(msg)

    out_path = repo_root / "benchmarks" / "_perf_run.json"
    proc = subprocess.run(  # noqa: S603 — ``bench`` is repo-controlled.
        [
            sys.executable,
            str(bench),
            "--iterations",
            str(iterations),
            "--out",
            str(out_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (
            f"benchmark exited with status {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
        raise RuntimeError(msg)

    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    if not isinstance(payload, list):
        msg = f"benchmark output is not a JSON list: {type(payload).__name__}"
        raise TypeError(msg)
    return payload


def _index(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``results`` indexed by the ``benchmark`` field."""
    return {entry["benchmark"]: entry for entry in results}


def _check(results: list[dict[str, Any]]) -> list[str]:
    """Return human-readable violations; empty list means clean."""
    violations: list[str] = []
    by_name = _index(results)

    expected = {"logging.emit.minimal", "logging.emit.redacted", "logging.emit.async"}
    missing = expected - by_name.keys()
    if missing:
        violations.append(f"benchmark output missing required entries: {sorted(missing)}")
        return violations

    minimal_p99 = float(by_name["logging.emit.minimal"]["p99_us"])
    redacted_p99 = float(by_name["logging.emit.redacted"]["p99_us"])
    async_p99 = float(by_name["logging.emit.async"]["p99_us"])

    # Absolute ceiling — order-of-magnitude regression fails hard.
    for label, value in (
        ("logging.emit.minimal", minimal_p99),
        ("logging.emit.redacted", redacted_p99),
        ("logging.emit.async", async_p99),
    ):
        if value > _ABSOLUTE_P99_CEILING_US:
            violations.append(
                f"{label}: p99 = {value:.1f} µs exceeds absolute ceiling "
                f"{_ABSOLUTE_P99_CEILING_US:.0f} µs (catastrophic regression)"
            )

    # Self-relative ratios — CI-invariant regression detection.
    if minimal_p99 > 0:
        redacted_ratio = redacted_p99 / minimal_p99
        if redacted_ratio > _REDACTED_VS_MINIMAL_MAX:
            violations.append(
                f"redacted/minimal p99 ratio = {redacted_ratio:.2f}× "
                f"exceeds budget {_REDACTED_VS_MINIMAL_MAX:.1f}× "
                f"(redacted={redacted_p99:.1f} µs, minimal={minimal_p99:.1f} µs). "
                "Likely cause: a new processor in the redactor chain that scales "
                "with payload size, or a regex added without a fast-reject path."
            )

        async_ratio = async_p99 / minimal_p99
        if async_ratio > _ASYNC_VS_MINIMAL_MAX:
            violations.append(
                f"async/minimal p99 ratio = {async_ratio:.2f}× "
                f"exceeds budget {_ASYNC_VS_MINIMAL_MAX:.1f}× "
                f"(async={async_p99:.1f} µs, minimal={minimal_p99:.1f} µs). "
                "Likely cause: AsyncQueueHandler.enqueue lost its put_nowait "
                "fast path, or BackgroundLogWriter started doing work on the "
                "producer thread."
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean run, 1 on any violation."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--iterations",
        type=int,
        default=_DEFAULT_BENCH_ITERATIONS,
        help=f"Samples per benchmark (default: {_DEFAULT_BENCH_ITERATIONS})",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)

    if not (args.root / "benchmarks").is_dir():
        print(
            f"error: {args.root} does not look like the sovyx repo (missing benchmarks/)",
            file=sys.stderr,
        )
        return 2

    try:
        results = _run_benchmark(iterations=args.iterations, repo_root=args.root)
    except (FileNotFoundError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
        print(
            f"FAIL: could not run perf benchmark: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    violations = _check(results)
    if violations:
        print(
            f"\nFAIL: {len(violations)} perf regression(s):",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  - {line}", file=sys.stderr)
        print("\n  Benchmark output:", file=sys.stderr)
        for entry in results:
            print(f"    {entry}", file=sys.stderr)
        return 1

    print("OK: observability hot-path latency within budget. Measurements:")
    for entry in results:
        print(
            f"  {entry['benchmark']}: "
            f"p50={entry['p50_us']:.1f} µs, "
            f"p95={entry['p95_us']:.1f} µs, "
            f"p99={entry['p99_us']:.1f} µs, "
            f"mean={entry['mean_us']:.1f} µs"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
