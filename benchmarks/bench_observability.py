"""Logging hot-path benchmarks for §23 perf budgets.

Measures the per-emit latency of the production structlog pipeline
under three configurations that cover the realistic operating modes
of the daemon:

  * **minimal**  — envelope + JSON renderer only. Establishes the
    floor; anything slower than this is processor overhead.
  * **redacted** — envelope + clamp + PII redactor + JSON renderer.
    The default production wiring; this is the budget that matters.
  * **async**    — same as redacted but with the BackgroundLogWriter
    enqueue measured separately so we can verify the async hand-off
    really is sub-100µs as ``async_handler.py`` claims.

Each benchmark emits ``ITERATIONS`` records (default 20 000), records
``time.perf_counter_ns()`` deltas, and reports p50 / p95 / p99 in
microseconds. Output JSON is suitable for both human reading and the
``scripts/check_perf_regression.py`` gate.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §23 (perf
budgets) and consumed by the ``perf-regression-gate`` CI job.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Reasonable default — enough samples to make p99 meaningful but not so
# many that the gate becomes the slowest CI step.
DEFAULT_ITERATIONS: int = 20_000


def _drain_handlers() -> None:
    """Close every file-backed handler so Windows can release file locks.

    Walks the root logger AND ``sovyx.audit`` (a propagate=False child
    that the root walk skips), flushes, closes, and removes every
    handler. ``shutdown_logging`` already drains the BackgroundLogWriter
    but doesn't touch the audit handler — without this the next
    ``TemporaryDirectory.cleanup()`` raises PermissionError.
    """
    for parent in (
        logging.getLogger(),
        logging.getLogger("sovyx.audit"),
    ):
        for handler in list(parent.handlers):
            with contextlib.suppress(Exception):
                handler.flush()
            with contextlib.suppress(Exception):
                handler.close()
            with contextlib.suppress(Exception):
                parent.removeHandler(handler)


def _silence_console() -> None:
    """Detach every StreamHandler so emit() no longer prints to stdout/stderr.

    The benchmark exercises real ``logger.info(...)`` calls 20 000+
    times — without this, the console-formatted JSON pollutes the
    benchmark output AND adds non-trivial IO time to every sample
    (skewing the percentile by orders of magnitude).
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler | logging.handlers.RotatingFileHandler
        ):
            root.removeHandler(handler)


def _measure_emit(
    *,
    iterations: int,
    pii_redaction: bool,
    async_queue: bool,
    log_file: Path,
) -> dict[str, float]:
    """Return p50/p95/p99 emit latency in microseconds for one config.

    The structlog pipeline is reset between configs so each measurement
    sees the same cold cache state. We measure ``logger.info(...)``
    end-to-end — the structlog processor chain through to the queue
    enqueue (async) or file write (sync).
    """
    from sovyx.engine.config import (
        LoggingConfig,
        ObservabilityConfig,
        ObservabilityFeaturesConfig,
        ObservabilityPIIConfig,
    )
    from sovyx.observability.logging import get_logger, setup_logging, shutdown_logging

    shutdown_logging(timeout=2.0)
    _drain_handlers()

    logging_cfg = LoggingConfig(
        level="INFO",
        console_format="json",
        log_file=log_file,
    )
    obs_cfg = ObservabilityConfig(
        features=ObservabilityFeaturesConfig(
            async_queue=async_queue,
            pii_redaction=pii_redaction,
            schema_validation=False,
        ),
        pii=ObservabilityPIIConfig(),
    )
    setup_logging(logging_cfg, obs_cfg, data_dir=log_file.parent)
    _silence_console()

    logger = get_logger("sovyx.bench.observability")

    # Warm-up — first call lazy-binds the structlog processor chain
    # for this logger; we don't want that one-shot setup cost in the
    # percentile.
    for _ in range(50):
        logger.info("bench.warmup", k=1, label="x")

    samples: list[int] = []
    samples_append = samples.append
    perf = time.perf_counter_ns
    # Hot loop kept tight on purpose — every line that runs inside the
    # measurement window adds bias to the percentile.
    for i in range(iterations):
        t0 = perf()
        logger.info("bench.emit", iteration=i, label="emit", payload="hello")
        samples_append(perf() - t0)

    shutdown_logging(timeout=5.0)
    _drain_handlers()

    samples.sort()
    return {
        "p50_us": round(samples[len(samples) // 2] / 1000, 3),
        "p95_us": round(samples[int(len(samples) * 0.95)] / 1000, 3),
        "p99_us": round(samples[int(len(samples) * 0.99)] / 1000, 3),
        "mean_us": round(statistics.fmean(samples) / 1000, 3),
        "samples": float(len(samples)),
    }


def run_all(*, iterations: int = DEFAULT_ITERATIONS) -> list[dict[str, Any]]:
    """Execute every observability benchmark and return their results."""
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sovyx-bench-obs-") as tmp:
        tmp_path = Path(tmp)
        for label, kwargs in (
            (
                "logging.emit.minimal",
                {"pii_redaction": False, "async_queue": False},
            ),
            (
                "logging.emit.redacted",
                {"pii_redaction": True, "async_queue": False},
            ),
            (
                "logging.emit.async",
                {"pii_redaction": True, "async_queue": True},
            ),
        ):
            log_dir = tmp_path / label
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "bench.log"
            stats = _measure_emit(
                iterations=iterations,
                log_file=log_file,
                **kwargs,
            )
            results.append({"benchmark": label, **stats})
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — write JSON to stdout or to ``--out`` if provided."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Samples per benchmark (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON results to this path (default: stdout)",
    )
    args = parser.parse_args(argv)

    out = run_all(iterations=args.iterations)
    payload = json.dumps(out, indent=2)
    if args.out is not None:
        args.out.write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
