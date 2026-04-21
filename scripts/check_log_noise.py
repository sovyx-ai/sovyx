"""CI gate — refuse silent regressions in cataloged-event log volume.

Stand-alone enforcement of §23.2 of IMPL-OBSERVABILITY-001 ("log noise
regression CI gate"). The failure mode this defends against is the
quietest one in the observability stack: a developer adds
``logger.info(...)`` inside a hot loop, the test suite still passes, and
six weeks later disk usage doubles — or worse, a high-frequency event
silently buries a CRITICAL entry on the dashboard.

The gate runs the deterministic
``tests.regression.synthetic_workload`` (a fixed multiset of cataloged
events that mirrors one end-to-end Sovyx interaction), counts emits by
``(event, logger)``, and compares against
``benchmarks/log_noise_baseline.json``. It enforces three checks:

  1. **Total volume drift > 15%** without a ``schema_version`` minor
     bump → hard FAIL. The 15% slack absorbs legitimate background
     additions (a new bootstrap log) but flags doubling.
  2. **Per-(event, logger) drift > 50%** → hard FAIL. A specific event
     firing 1.5× more is almost always a hot-loop ``logger.info`` —
     the most common silent regression.
  3. **New event not in KNOWN_EVENTS** → hard FAIL. A workload emit
     that lands on the wire without a catalog entry means the schema
     gate (Task 11.2) didn't see it; force the developer to either
     register the event or remove the emit.

Soft (non-failing) signals:

  * Total drift in 5–15% range → warning printed to stderr; CI stays
    green but the operator gets a heads-up. The next legitimate
    feature commit can absorb it into a fresh baseline.
  * Per-(event, logger) drift in 25–50% range → warning printed but
    not failing. Fluctuations near the boundary often resolve
    themselves over a couple of commits.

When the gate fails the operator has two options:

  * Real regression → revert the offending commit.
  * Intentional bump → run ``scripts/update_log_noise_baseline.py
    --justify "<reason>"`` and commit the refreshed baseline along with
    the change. The justification is recorded in the baseline JSON's
    ``justification`` field so future readers know *why* volume grew.

Wired into ``.github/workflows/ci.yml`` as ``log-noise-gate`` after
``perf-regression-gate``.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 — controlled subprocess of our own workload.
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

# ── Drift budgets ─────────────────────────────────────────────────────
# Total-volume budget: 15% absorbs legitimate background-event
# additions (e.g., a new startup-cascade emit) without firing on every
# refactor; 50% per-event budget targets the most common regression
# (hot-loop ``logger.info``) which doubles or triples a single event.
_TOTAL_DRIFT_FAIL_PCT: float = 15.0
_PER_EVENT_DRIFT_FAIL_PCT: float = 50.0

# Soft-warning thresholds — below the fail line but worth surfacing so
# operators see drift trending before it crosses.
_TOTAL_DRIFT_WARN_PCT: float = 5.0
_PER_EVENT_DRIFT_WARN_PCT: float = 25.0


def _run_workload(*, repo_root: Path) -> list[dict[str, Any]]:
    """Spawn the workload module in a fresh interpreter, return JSONL entries.

    Subprocess isolation guarantees the workload sees a clean structlog
    pipeline (no leftover handlers from earlier in the gate's own
    process), and matches how production runs the daemon.
    """
    with tempfile.TemporaryDirectory(prefix="sovyx-noise-gate-") as tmp:
        tmp_path = Path(tmp)
        out_path = tmp_path / "workload.log"
        proc = subprocess.run(  # noqa: S603 — module path is repo-controlled.
            [
                sys.executable,
                "-m",
                "tests.regression.synthetic_workload",
                "--out",
                str(out_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            msg = (
                f"workload exited with status {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            )
            raise RuntimeError(msg)
        if not out_path.is_file():
            msg = f"workload did not produce {out_path}"
            raise FileNotFoundError(msg)
        entries: list[dict[str, Any]] = []
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
        return entries


def _count_by_event_logger(entries: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Return ``{(event, logger): count}`` for the given entries."""
    counter: Counter[tuple[str, str]] = Counter()
    for entry in entries:
        event = str(entry.get("event", ""))
        logger = str(entry.get("logger", ""))
        if not event:
            continue
        counter[(event, logger)] += 1
    return dict(counter)


def _drift_pct(observed: int, baseline: int) -> float:
    """Return the percent drift of *observed* vs *baseline*; 0 when both are 0."""
    if baseline == 0:
        return 100.0 if observed > 0 else 0.0
    return abs(observed - baseline) / baseline * 100.0


def _check(
    *,
    observed: dict[tuple[str, str], int],
    baseline: dict[str, int],
    baseline_total: int,
    schema_version_observed: str,
    schema_version_baseline: str,
    known_events: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Return ``(failures, warnings)`` lists for the gate."""
    failures: list[str] = []
    warnings: list[str] = []

    observed_total = sum(observed.values())
    schema_bumped = schema_version_observed != schema_version_baseline

    # Total volume drift
    total_drift = _drift_pct(observed_total, baseline_total)
    if total_drift > _TOTAL_DRIFT_FAIL_PCT and not schema_bumped:
        failures.append(
            f"total log volume drift = {total_drift:.1f}% "
            f"(observed={observed_total}, baseline={baseline_total}) "
            f"exceeds budget {_TOTAL_DRIFT_FAIL_PCT:.0f}% without "
            f"schema_version bump (still '{schema_version_baseline}'). "
            "Either revert the new emit site, or run "
            "scripts/update_log_noise_baseline.py with --justify."
        )
    elif total_drift > _TOTAL_DRIFT_WARN_PCT:
        warnings.append(
            f"total log volume drift = {total_drift:.1f}% "
            f"(observed={observed_total}, baseline={baseline_total}) "
            f"exceeds soft warning {_TOTAL_DRIFT_WARN_PCT:.0f}%; refresh "
            "baseline if intentional."
        )

    # Per-(event, logger) drift
    for (event, logger), observed_count in sorted(observed.items()):
        # Forbidden: workload emitted an event not in KNOWN_EVENTS.
        # ``audit.handler.installed`` is the single emit injected by
        # setup_logging() itself — exempt because it predates Phase 11
        # and lives outside the workload's payload table.
        if event not in known_events and event != "audit.handler.installed":
            failures.append(
                f"workload emitted uncataloged event '{event}' "
                f"(logger={logger}, n={observed_count}). Either register "
                "it in KNOWN_EVENTS (docs-internal/log_schema/<event>.json + "
                "regenerate _models.py) or remove the emit from the "
                "workload."
            )
            continue

        key = f"{event}|{logger}"
        baseline_count = baseline.get(key, 0)
        if baseline_count == 0:
            # New (event, logger) pair — accepted only with schema bump,
            # otherwise it's a silent telemetry addition.
            if not schema_bumped:
                failures.append(
                    f"new (event, logger) appeared without schema bump: "
                    f"'{event}|{logger}' = {observed_count}. Add to "
                    "baseline via scripts/update_log_noise_baseline.py "
                    "with --justify."
                )
            continue

        drift = _drift_pct(observed_count, baseline_count)
        if drift > _PER_EVENT_DRIFT_FAIL_PCT:
            failures.append(
                f"per-event drift > {_PER_EVENT_DRIFT_FAIL_PCT:.0f}% on "
                f"'{event}|{logger}': observed={observed_count}, "
                f"baseline={baseline_count} (Δ={drift:.1f}%). Likely "
                "cause: a new logger.info() inside a hot loop. Trace "
                "the emit site and either drop the call or move it "
                "behind a sampler."
            )
        elif drift > _PER_EVENT_DRIFT_WARN_PCT:
            warnings.append(
                f"per-event drift {drift:.1f}% on '{event}|{logger}': "
                f"observed={observed_count}, baseline={baseline_count}"
            )

    # Disappeared keys — also a regression worth surfacing.
    for key, baseline_count in sorted(baseline.items()):
        event, _, logger = key.partition("|")
        if (event, logger) not in observed and baseline_count > 0:
            failures.append(
                f"baseline key disappeared from workload output: "
                f"'{key}' (was {baseline_count}). Either the emit site "
                "was removed (refresh baseline with --justify) or the "
                "workload no longer reaches it (regression)."
            )

    return failures, warnings


def _load_known_events(repo_root: Path) -> frozenset[str]:
    """Return the canonical KNOWN_EVENTS set without importing sovyx."""
    sys.path.insert(0, str(repo_root / "src"))
    try:
        from sovyx.observability.schema import KNOWN_EVENTS
    finally:
        sys.path.pop(0)
    return frozenset(KNOWN_EVENTS.keys())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean run, 1 on any FAIL."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("benchmarks/log_noise_baseline.json"),
        help="Baseline file (default: benchmarks/log_noise_baseline.json)",
    )
    args = parser.parse_args(argv)

    baseline_path = args.baseline if args.baseline.is_absolute() else args.root / args.baseline
    if not baseline_path.is_file():
        print(
            f"error: baseline not found at {baseline_path}. Generate one "
            "with scripts/update_log_noise_baseline.py.",
            file=sys.stderr,
        )
        return 2

    baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_total = int(baseline_payload.get("total", 0))
    baseline_counts: dict[str, int] = baseline_payload.get("by_event_logger", {})
    schema_version_baseline = str(baseline_payload.get("schema_version", "0.0.0"))

    try:
        known_events = _load_known_events(args.root)
    except ImportError as exc:
        print(
            f"error: could not import KNOWN_EVENTS from sovyx.observability.schema: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        entries = _run_workload(repo_root=args.root)
    except (FileNotFoundError, RuntimeError) as exc:
        print(
            f"FAIL: could not run workload: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    observed = _count_by_event_logger(entries)

    # Schema version observed = whatever any entry declares (consistent
    # by construction of EnvelopeProcessor — pick first non-empty).
    schema_version_observed = ""
    for entry in entries:
        candidate = entry.get("schema_version")
        if candidate:
            schema_version_observed = str(candidate)
            break

    failures, warnings = _check(
        observed=observed,
        baseline=baseline_counts,
        baseline_total=baseline_total,
        schema_version_observed=schema_version_observed,
        schema_version_baseline=schema_version_baseline,
        known_events=known_events,
    )

    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)

    if failures:
        print(
            f"\nFAIL: {len(failures)} log-noise regression(s) "
            f"(observed_total={sum(observed.values())}, "
            f"baseline_total={baseline_total}):",
            file=sys.stderr,
        )
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(
        f"OK: log noise within budget. "
        f"observed_total={sum(observed.values())}, "
        f"baseline_total={baseline_total}, "
        f"unique_event_logger={len(observed)}, "
        f"schema_version={schema_version_observed!r}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
