"""CI gate — assert observability hot-path latency stays within budget.

Stand-alone enforcement of §23 of IMPL-OBSERVABILITY-001 ("perf
budgets"). Runs ``benchmarks/bench_observability.py`` multiple times
to measure the per-emit latency of the structlog pipeline under three
configurations (minimal / redacted / async), then enforces two
complementary checks against the median p99 latencies across runs:

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

Why median-of-N
===============

Single-shot p99 on a shared CI runner is not noise-invariant — a
single GC pause or noisy-neighbour hiccup puts one sample at the
top of the 99.x percentile bucket, and with 20k samples a single
outlier at 700 µs pushes the p99 ratio past the gate. The gate's
previous docstring claimed "invariant under runner contention" but
that's only true for p50 / mean; p99 is explicitly tail-sensitive.

Canonical solution (used by ``cargo bench`` / Google Benchmark /
criterion.rs): run the benchmark N times and compute the **median**
of the resulting p99s. Noise almost always raises latency, so the
median across 3+ independent runs discards one outlier while still
catching real regressions (a 5× regression shows as 5× in all N
runs — median = 5× — gate still fires). One noisy run with real
code + two clean runs = median pulled from clean runs → gate passes
correctly.

We take **median**, not minimum. Minimum is maximally permissive
and would mask a genuinely flaky hot path. Median keeps the cost
asymmetry (one bad run costs nothing, three bad runs are a real
regression).

Wired into ``.github/workflows/ci.yml`` as the
``perf-regression-gate`` job after ``metrics-cardinality-gate``.
"""

from __future__ import annotations

import argparse
import json
import statistics
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

# Default benchmark sample count per run — large enough for a stable
# p99 but fast enough that the CI step finishes in a few seconds.
_DEFAULT_BENCH_ITERATIONS: int = 20_000

# Default independent-run count for median-of-N computation. Three is
# the canonical minimum (survives exactly one noisy run); five is
# overkill for a 60-second CI gate. Odd numbers avoid the ambiguity
# of the median between two central values.
_DEFAULT_REPEATS: int = 3


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


def _median_p99s(
    runs: list[list[dict[str, Any]]],
) -> dict[str, float]:
    """Return ``{benchmark_name: median_p99_us}`` across independent runs.

    Expects each entry of ``runs`` to be a full benchmark output list
    (i.e. one item per config: minimal / redacted / async). Panics
    when any run is missing one of the required benchmarks.
    """
    expected = {"logging.emit.minimal", "logging.emit.redacted", "logging.emit.async"}
    per_name_p99s: dict[str, list[float]] = {name: [] for name in expected}
    for results in runs:
        by_name = _index(results)
        for name in expected:
            if name not in by_name:
                msg = f"benchmark run missing entry: {name!r}"
                raise KeyError(msg)
            per_name_p99s[name].append(float(by_name[name]["p99_us"]))
    return {name: statistics.median(p99s) for name, p99s in per_name_p99s.items()}


def _check(runs: list[list[dict[str, Any]]]) -> list[str]:
    """Return human-readable violations; empty list means clean.

    Takes a list of benchmark runs and computes the median p99 per
    config before applying the absolute + ratio checks. Median is the
    noise-robust statistic for p99 on shared runners — see module
    docstring for the why.
    """
    violations: list[str] = []
    if not runs:
        violations.append("no benchmark runs were collected")
        return violations

    try:
        medians = _median_p99s(runs)
    except KeyError as exc:
        violations.append(str(exc))
        return violations

    minimal_p99 = medians["logging.emit.minimal"]
    redacted_p99 = medians["logging.emit.redacted"]
    async_p99 = medians["logging.emit.async"]

    # Absolute ceiling — order-of-magnitude regression fails hard.
    for label, value in (
        ("logging.emit.minimal", minimal_p99),
        ("logging.emit.redacted", redacted_p99),
        ("logging.emit.async", async_p99),
    ):
        if value > _ABSOLUTE_P99_CEILING_US:
            violations.append(
                f"{label}: median-of-{len(runs)} p99 = {value:.1f} µs exceeds "
                f"absolute ceiling {_ABSOLUTE_P99_CEILING_US:.0f} µs "
                "(catastrophic regression)"
            )

    # Self-relative ratios — derived from the medians so a single
    # noisy run can't bump a ratio through the budget.
    if minimal_p99 > 0:
        redacted_ratio = redacted_p99 / minimal_p99
        if redacted_ratio > _REDACTED_VS_MINIMAL_MAX:
            violations.append(
                f"redacted/minimal p99 ratio = {redacted_ratio:.2f}× "
                f"exceeds budget {_REDACTED_VS_MINIMAL_MAX:.1f}× "
                f"(median redacted={redacted_p99:.1f} µs, "
                f"median minimal={minimal_p99:.1f} µs across {len(runs)} runs). "
                "Likely cause: a new processor in the redactor chain that scales "
                "with payload size, or a regex added without a fast-reject path."
            )

        async_ratio = async_p99 / minimal_p99
        if async_ratio > _ASYNC_VS_MINIMAL_MAX:
            violations.append(
                f"async/minimal p99 ratio = {async_ratio:.2f}× "
                f"exceeds budget {_ASYNC_VS_MINIMAL_MAX:.1f}× "
                f"(median async={async_p99:.1f} µs, "
                f"median minimal={minimal_p99:.1f} µs across {len(runs)} runs). "
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
        "--repeats",
        type=int,
        default=_DEFAULT_REPEATS,
        help=(
            f"Independent benchmark runs for median-of-N (default: "
            f"{_DEFAULT_REPEATS}). Must be odd; use 3-5 for CI."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)

    if args.repeats < 1:
        print("error: --repeats must be >= 1", file=sys.stderr)
        return 2
    if not (args.root / "benchmarks").is_dir():
        print(
            f"error: {args.root} does not look like the sovyx repo (missing benchmarks/)",
            file=sys.stderr,
        )
        return 2

    runs: list[list[dict[str, Any]]] = []
    for attempt in range(1, args.repeats + 1):
        try:
            run_results = _run_benchmark(
                iterations=args.iterations,
                repo_root=args.root,
            )
        except (FileNotFoundError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
            print(
                f"FAIL: could not run perf benchmark (attempt {attempt}/{args.repeats}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        runs.append(run_results)

    violations = _check(runs)
    if violations:
        print(
            f"\nFAIL: {len(violations)} perf regression(s) (median of {len(runs)} runs):",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  - {line}", file=sys.stderr)
        print("\n  Per-run benchmark output:", file=sys.stderr)
        for idx, results in enumerate(runs, start=1):
            print(f"    run {idx}:", file=sys.stderr)
            for entry in results:
                print(f"      {entry}", file=sys.stderr)
        return 1

    medians = _median_p99s(runs)
    print(
        f"OK: observability hot-path latency within budget (median of {len(runs)} runs).",
    )
    for name, median in medians.items():
        per_run = [float(next(e for e in r if e["benchmark"] == name)["p99_us"]) for r in runs]
        print(
            f"  {name}: median p99={median:.1f} µs "
            f"(per-run p99: {', '.join(f'{v:.1f}' for v in per_run)})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
