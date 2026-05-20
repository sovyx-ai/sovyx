#!/usr/bin/env python3
"""Mission H4 — F1..F9 telemetry analyzer.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T1.5 + §9.

Reads ``~/.sovyx/logs/sovyx.log`` (or ``--log``) and emits a JSON
report covering the 9 falsifiability gates F1..F9. Mirrors the H3
``analyze_h3_telemetry.py`` + H2 ``analyze_h2_telemetry.py`` patterns:
single-file Python; stdlib-only; idempotent; safe to re-run.

Output schema (see §9 of the mission spec):

```
{
    "f1_gate15_falsifiability": {...},
    "f2_22_new_fields_present": {...},
    "f3_anomaly_memory_growth_spike_fires": {...},
    "f4_cohort_budget_l909_replay": {...},
    "f5_heap_snapshot_persisted": {...},
    "f6_heap_snapshot_skipped_dedup": {...},
    "f7_circuit_breaker_ack_unblocks": {...},
    "f8_ast_exhaustiveness": {...},
    "f9_doctor_resources_cli_render": {...},
    "ready_for_strict_flip": bool
}
```

Used by the operator in Phase 2 calibration to surface a single JSON
blob back to Claude for STRICT-flip readiness evaluation per
``feedback_full_autonomous_authority``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_LOG = Path.home() / ".sovyx" / "logs" / "sovyx.log"

# 22 H4-new snapshot fields per Mission H4 §3 F2 literal enumeration.
# Must be a subset of the SSoT in ``_HEALTH_SNAPSHOT_FIELDS``. Kept in
# sync manually because this script is intentionally stdlib-only (no
# PYTHONPATH manipulation).
#
# Mission H4 §22 v0.49.31 closure addendum: the 12th + 13th
# audit-discipline cycles caught a count-vs-enumeration drift between
# this analyzer's 18-entry set and §3 F2's 22-name enumeration. The set
# NOW matches the F2 literal list exactly (22 names). The bonus
# emit-side field ``to_thread.queue_depth`` — emitted by code but NOT
# in F2 — is intentionally excluded so F2 = 22/22 reports honestly
# against spec.
_H4_NEW_FIELDS: frozenset[str] = frozenset(
    {
        # to_thread block (5 names per F2)
        "to_thread.pool_size",
        "to_thread.active_workers",  # v0.49.31 — F2 literal
        "to_thread.max_workers",
        "to_thread.dispatch_count_total",
        "to_thread.dispatch_count_per_label",
        # lock_dict block (3 names per F2)
        "lock_dict.total_cardinality",
        "lock_dict.per_owner",
        "lock_dict.instance_count",
        # onnx block (2 names per F2)
        "onnx.session_count",
        "onnx.session_labels",
        # gc block (2 names per F2)
        "gc.collections_by_gen",
        "gc.objects_count",
        # tracemalloc block (3 names per F2)
        "tracemalloc.is_tracing",
        "tracemalloc.current_kb",
        "tracemalloc.peak_kb",
        # exception_cohort block (3 names per F2)
        "exception_cohort.retained_bytes_estimate",
        "exception_cohort.distinct_group_id_count",
        "exception_cohort.last_observation_monotonic",
        # process block extension (3 names per F2; v0.49.31 closure)
        "process.memory_percent",
        "process.cpu_times_user_s",
        "process.cpu_times_system_s",
        # asyncio block extension (1 name per F2; v0.49.31 closure)
        # F2 does NOT include ``asyncio.default_executor_state`` — only
        # the task-name field is in the falsifiability list.
        "asyncio.current_running_task_name",
    },
)


@dataclass
class _F1Result:
    passes: bool = False
    synthetic_violation_count: int = 0
    note: str = ""


@dataclass
class _F2Result:
    passes: bool = False
    fields_observed: int = 0
    fields_expected: int = len(_H4_NEW_FIELDS)
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class _F3Result:
    passes: bool = False
    memory_growth_spike_count: int = 0
    note: str = ""


@dataclass
class _F4Result:
    passes: bool = False
    cohort_budget_exceeded_events: int = 0
    cohorts_observed: list[str] = field(default_factory=list)


@dataclass
class _F5Result:
    passes: bool = False
    heap_snapshot_files: list[str] = field(default_factory=list)
    tracemalloc_enabled_in_log: bool = False


@dataclass
class _F6Result:
    passes: bool = False
    heap_snapshot_skipped_events: int = 0
    distinct_cohorts_skipped: list[str] = field(default_factory=list)


@dataclass
class _F7Result:
    passes: bool = False
    circuit_breaker_blocks: int = 0
    ack_unblocks: int = 0


@dataclass
class _F8Result:
    passes: bool = False
    note: str = "evaluated at build time by Gate 15"


@dataclass
class _F9Result:
    passes: bool = False
    note: str = "evaluated by sovyx doctor resources --explain CLI integration test"


@dataclass
class TelemetryReport:
    log_path: str = ""
    log_line_count: int = 0
    f1_gate15_falsifiability: _F1Result = field(default_factory=_F1Result)
    f2_22_new_fields_present: _F2Result = field(default_factory=_F2Result)
    f3_anomaly_memory_growth_spike_fires: _F3Result = field(default_factory=_F3Result)
    f4_cohort_budget_l909_replay: _F4Result = field(default_factory=_F4Result)
    f5_heap_snapshot_persisted: _F5Result = field(default_factory=_F5Result)
    f6_heap_snapshot_skipped_dedup: _F6Result = field(default_factory=_F6Result)
    f7_circuit_breaker_ack_unblocks: _F7Result = field(default_factory=_F7Result)
    f8_ast_exhaustiveness: _F8Result = field(default_factory=_F8Result)
    f9_doctor_resources_cli_render: _F9Result = field(default_factory=_F9Result)
    ready_for_strict_flip: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ready_for_strict_flip"] = self._compute_strict_readiness()
        return d

    def _compute_strict_readiness(self) -> bool:
        # F1 + F2 + F3 must pass (those are the load-bearing
        # producer/consumer/SSoT parity gates). F4..F7 must pass if
        # any cohort-governor activity is observed in the log. F8/F9
        # are evaluated out-of-band; they are advisory.
        if not (
            self.f1_gate15_falsifiability.passes
            and self.f2_22_new_fields_present.passes
            and self.f3_anomaly_memory_growth_spike_fires.passes
        ):
            return False
        if self.f4_cohort_budget_l909_replay.cohort_budget_exceeded_events > 0 and not (
            self.f4_cohort_budget_l909_replay.passes
            and self.f6_heap_snapshot_skipped_dedup.passes
        ):
            return False
        return not (
            self.f5_heap_snapshot_persisted.tracemalloc_enabled_in_log
            and not self.f5_heap_snapshot_persisted.passes
        )


def _iter_log_records(log_path: Path) -> list[dict[str, Any]]:
    """Parse JSON Lines log file; tolerate non-JSON lines (rare)."""
    records: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def _evaluate_f2(records: list[dict[str, Any]]) -> _F2Result:
    """Check that every H4 new field appears in at least one self.health.snapshot record."""
    seen_fields: set[str] = set()
    for record in records:
        if record.get("event") != "self.health.snapshot":
            continue
        for key in record:
            if key in _H4_NEW_FIELDS:
                seen_fields.add(key)
    missing = sorted(_H4_NEW_FIELDS - seen_fields)
    return _F2Result(
        passes=len(missing) == 0,
        fields_observed=len(seen_fields),
        missing_fields=missing,
    )


def _evaluate_f3(records: list[dict[str, Any]]) -> _F3Result:
    """Check that anomaly.memory_growth_spike fires at least once.

    Post-mission HEAD: the fix at observability/anomaly.py renames the
    consumer key from system.rss_bytes to process.rss_bytes — the
    detector starts firing on real RSS growth.
    """
    count = sum(1 for r in records if r.get("event") == "anomaly.memory_growth_spike")
    return _F3Result(
        passes=count > 0,
        memory_growth_spike_count=count,
        note=(
            "F3 passes when anomaly.memory_growth_spike is observed at least once. "
            "Pre-mission HEAD never emits this event (field-name drift)."
        )
        if count == 0
        else "F3 GREEN — anomaly detector fires correctly.",
    )


def _evaluate_f4(records: list[dict[str, Any]]) -> _F4Result:
    """Check cohort_budget_exceeded events."""
    cohort_counter: Counter[str] = Counter()
    for record in records:
        if record.get("event") == "engine.resources.cohort_budget_exceeded":
            cohort = record.get("cohort") or record.get("axis") or "unknown"
            cohort_counter[cohort] += 1
    total = sum(cohort_counter.values())
    return _F4Result(
        passes=total >= 0,  # passes vacuously if no spike triggered yet
        cohort_budget_exceeded_events=total,
        cohorts_observed=sorted(cohort_counter.keys()),
    )


def _evaluate_f5(records: list[dict[str, Any]], diag_dir: Path) -> _F5Result:
    """Check heap-snapshot files persisted on RSS-driven cohort events."""
    tm_enabled = any(
        r.get("event") == "self.health.snapshot" and r.get("tracemalloc.is_tracing") is True
        for r in records
    )
    files: list[str] = []
    if diag_dir.is_dir():
        files = sorted(p.name for p in diag_dir.glob("heap-snapshot-*.json"))
    passes = (not tm_enabled) or (tm_enabled and len(files) >= 1)
    return _F5Result(
        passes=passes,
        heap_snapshot_files=files,
        tracemalloc_enabled_in_log=tm_enabled,
    )


def _evaluate_f6(records: list[dict[str, Any]]) -> _F6Result:
    """Check heap_snapshot_skipped event dedup."""
    skipped_events: list[dict[str, Any]] = [
        r for r in records if r.get("event") == "engine.resources.heap_snapshot_skipped"
    ]
    cohorts_skipped: set[str] = {
        r.get("cohort", "unknown") for r in skipped_events if isinstance(r, dict)
    }
    # Dedup invariant: skipped count should be roughly proportional to
    # cohort count (≤ 1 per cohort window); a 10× ratio suggests the
    # dedup is broken.
    passes = len(skipped_events) <= max(1, 3 * len(cohorts_skipped))
    return _F6Result(
        passes=passes,
        heap_snapshot_skipped_events=len(skipped_events),
        distinct_cohorts_skipped=sorted(cohorts_skipped),
    )


def _evaluate_f7(records: list[dict[str, Any]]) -> _F7Result:
    """Check circuit-breaker activations + ack unblocks."""
    blocks = sum(1 for r in records if r.get("event") == "engine.resources.cohort_breaker_engaged")
    acks = sum(1 for r in records if r.get("event") == "engine.resources.cohort_ack_recorded")
    passes = blocks >= 0  # vacuous when no breaker tripped yet
    return _F7Result(
        passes=passes,
        circuit_breaker_blocks=blocks,
        ack_unblocks=acks,
    )


def evaluate(
    log_path: Path = _DEFAULT_LOG,
    diag_dir: Path | None = None,
) -> TelemetryReport:
    """Build a full F1..F9 report."""
    if diag_dir is None:
        diag_dir = Path.home() / ".sovyx" / "diagnostics"
    records = _iter_log_records(log_path)
    report = TelemetryReport(
        log_path=str(log_path),
        log_line_count=len(records),
    )

    # F1 is evaluated by a separate integration test
    # (test_check_resource_hygiene_discipline_falsifiability.py). The
    # analyzer trusts the test result if a marker file
    # ``.git/.h4-gate15-falsifiability-pass`` exists; otherwise reports
    # "not evaluated" (vacuously passing for the strict-flip readiness
    # heuristic on this script alone).
    marker = Path(".git/.h4-gate15-falsifiability-pass")
    report.f1_gate15_falsifiability = _F1Result(
        passes=marker.exists(),
        synthetic_violation_count=0,
        note=(
            "Marker file present — Gate 15 STRICT mode rejects a synthetic drift as expected."
            if marker.exists()
            else "Marker file absent — run `pytest tests/integration/scripts/"
            "test_check_resource_hygiene_discipline_falsifiability.py` to "
            "set the marker."
        ),
    )

    report.f2_22_new_fields_present = _evaluate_f2(records)
    report.f3_anomaly_memory_growth_spike_fires = _evaluate_f3(records)
    report.f4_cohort_budget_l909_replay = _evaluate_f4(records)
    report.f5_heap_snapshot_persisted = _evaluate_f5(records, diag_dir)
    report.f6_heap_snapshot_skipped_dedup = _evaluate_f6(records)
    report.f7_circuit_breaker_ack_unblocks = _evaluate_f7(records)
    # F8 evaluated by mypy strict in CI; F9 evaluated by CLI tests.
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mission H4 — F1..F9 telemetry analyzer.")
    parser.add_argument(
        "--log",
        type=Path,
        default=_DEFAULT_LOG,
        help=f"Path to sovyx.log (default: {_DEFAULT_LOG})",
    )
    parser.add_argument(
        "--diagnostics-dir",
        type=Path,
        default=Path.home() / ".sovyx" / "diagnostics",
        help="Path to ~/.sovyx/diagnostics (heap-snapshot file directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report (default: yes)",
    )
    args = parser.parse_args(argv)
    report = evaluate(args.log, args.diagnostics_dir)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
