"""Operator-side telemetry analyser for Mission C4 F-gate verification.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 4 + §3 falsifiability gates.

Usage::

    uv run python scripts/dev/analyze_c4_telemetry.py \\
        --log ~/.sovyx/logs/sovyx.log [--json]

Outputs F1 / F3 verdicts based on structured log events. F4 + F5 are
Phase 3 (ack persistence) and skipped here; the script reports them as
``not_applicable`` when Phase 3 hasn't shipped yet.

* F1 — banner surfaced within 5 s of any degraded axis recording.
  Detection: for each axis-record event, the matching banner-surface
  event MUST appear within 5 s. Phase 1.B emits
  ``voice.supervisor.soft_recovery_triggered`` only on auto-recovery
  trigger, not on banner-surface — so F1 is observed via the
  store-record cadence + the dashboard's HTTP poll (5 s baseline).
* F3 — auto-recovery success rate ≥ 95% over sessions where the
  governor triggers AND the operator's underlying issue resolves
  within the retry budget. Measured via
  ``voice.supervisor.soft_recovery_complete{success}`` event ratio.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class C4TelemetryVerdict:
    f1_status: str = "no_data"  # "pass" | "fail" | "no_data"
    f1_observations: int = 0
    f1_max_time_to_surface_s: float = 0.0
    f3_status: str = "no_data"
    f3_triggers: int = 0
    f3_successes: int = 0
    f3_success_rate: float = 0.0
    f3_p50_recovery_ms: int = 0
    f3_p99_recovery_ms: int = 0
    f3_escalations: int = 0
    f4_status: str = "not_applicable"  # Phase 3
    f5_status: str = "not_applicable"  # Phase 3
    parse_errors: int = 0
    lines_inspected: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "f1": {
                "status": self.f1_status,
                "observations": self.f1_observations,
                "max_time_to_surface_s": self.f1_max_time_to_surface_s,
            },
            "f3": {
                "status": self.f3_status,
                "triggers": self.f3_triggers,
                "successes": self.f3_successes,
                "success_rate": self.f3_success_rate,
                "p50_recovery_ms": self.f3_p50_recovery_ms,
                "p99_recovery_ms": self.f3_p99_recovery_ms,
                "escalations": self.f3_escalations,
            },
            "f4": {"status": self.f4_status, "note": "Phase 3 — not yet shipped"},
            "f5": {"status": self.f5_status, "note": "Phase 3 — not yet shipped"},
            "parse_errors": self.parse_errors,
            "lines_inspected": self.lines_inspected,
            "notes": self.notes,
        }


def _parse_log_line(line: str) -> dict[str, Any] | None:
    """Sovyx structured logs are JSON-per-line via structlog. Returns
    None on non-JSON or malformed lines (silently skipped)."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _percentile(samples: list[int], pct: float) -> int:
    """Simple percentile — sufficient for the analyser's ≤ 100-sample
    typical session."""
    if not samples:
        return 0
    sorted_samples = sorted(samples)
    k = int(len(sorted_samples) * pct)
    k = max(0, min(k, len(sorted_samples) - 1))
    return sorted_samples[k]


def analyze_log(log_path: Path) -> C4TelemetryVerdict:
    """Parse a Sovyx log file and emit a verdict bundle."""
    verdict = C4TelemetryVerdict()

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError) as exc:
        verdict.notes.append(f"log_read_failed: {exc}")
        return verdict

    f3_recovery_durations_ms: list[int] = []
    f3_recovery_outcomes: list[bool] = []

    for line in text.splitlines():
        verdict.lines_inspected += 1
        parsed = _parse_log_line(line)
        if parsed is None:
            continue
        event = parsed.get("event", "")

        # F3 — soft-recovery telemetry
        if event == "voice.supervisor.soft_recovery_triggered":
            verdict.f3_triggers += 1
        elif event == "voice.supervisor.soft_recovery_complete":
            elapsed = parsed.get("voice.elapsed_ms")
            if isinstance(elapsed, int):
                f3_recovery_durations_ms.append(elapsed)
            f3_recovery_outcomes.append(True)
        elif event == "voice.supervisor.soft_recovery_failed":
            f3_recovery_outcomes.append(False)
        elif event == "voice.supervisor.escalation_required":
            verdict.f3_escalations += 1

    # F1 — composite-banner time-to-surface is best measured at the
    # frontend (the React poller has 5 s baseline). Without a frontend
    # event log we cannot derive max_time_to_surface_s from the
    # backend-only log file. Mark "no_data" with a structured note
    # so operators know to capture frontend telemetry next session.
    verdict.notes.append(
        "F1 (banner surface latency) requires frontend telemetry; the "
        "backend log alone bounds the worst case at the 5 s poll baseline.",
    )

    # F3 — compute success rate + percentiles
    verdict.f3_successes = sum(1 for ok in f3_recovery_outcomes if ok)
    total_outcomes = len(f3_recovery_outcomes)
    if total_outcomes > 0:
        verdict.f3_success_rate = round(verdict.f3_successes / total_outcomes, 4)
    if f3_recovery_durations_ms:
        verdict.f3_p50_recovery_ms = _percentile(f3_recovery_durations_ms, 0.5)
        verdict.f3_p99_recovery_ms = _percentile(f3_recovery_durations_ms, 0.99)

    # F3 verdict
    if verdict.f3_triggers == 0:
        verdict.f3_status = "no_data"
    elif verdict.f3_success_rate >= 0.95:
        verdict.f3_status = "pass"
    else:
        verdict.f3_status = "fail"

    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission C4 telemetry analyser (F1 / F3 gates).",
    )
    parser.add_argument(
        "--log",
        type=Path,
        required=True,
        help="Path to sovyx.log (typically ~/.sovyx/logs/sovyx.log).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON verdict bundle to stdout.",
    )
    args = parser.parse_args(argv)

    verdict = analyze_log(args.log)

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(f"Mission C4 telemetry verdict for {args.log}:")
        print(f"  F1 — banner surface: {verdict.f1_status}")
        print(
            f"  F3 — soft-recovery: {verdict.f3_status} "
            f"({verdict.f3_successes}/{verdict.f3_triggers} successes, "
            f"P50={verdict.f3_p50_recovery_ms}ms, "
            f"P99={verdict.f3_p99_recovery_ms}ms, "
            f"{verdict.f3_escalations} escalations)",
        )
        print(f"  F4 — ack persistence: {verdict.f4_status}")
        print(f"  F5 — TTL re-surface: {verdict.f5_status}")
        print(f"  ({verdict.lines_inspected} lines inspected)")
        for note in verdict.notes:
            print(f"  NOTE: {note}")

    # Exit non-zero if any F-gate failed (CI-friendly).
    if verdict.f3_status == "fail":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
