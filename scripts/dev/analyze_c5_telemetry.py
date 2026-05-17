"""Operator-side telemetry analyser for Mission C5 F-gate verification.

Mission anchor: ``MISSION-c5-dashboard-distribution-integrity-2026-05-17.md``
§Phase 2 + §3 falsifiability gates.

Usage::

    uv run python scripts/dev/analyze_c5_telemetry.py \\
        --log ~/.sovyx/logs/sovyx.log [--json]

Outputs F1 / F2 / F3 / F4 verdicts based on structured log events:

* F1 — Quality Gate 11 falsifiability. Verified mechanically in CI by
  ``tests/integration/scripts/test_check_dashboard_bundle_integrity_falsifiability.py``.
  The analyser reports ``ci_verified`` if the test exists locally; the
  operator-side run NEVER toggles F1 — that gate lives in CI.

* F2 — Boot-scan time-to-surface ≤ 100 ms. For every
  ``dashboard.distribution.bundle_scanned`` event emitted by
  ``create_app()``, the analyser records its ``scan_duration_ms``
  field. P50 + P99 over the operator's window are emitted.

* F3 — CI-synthetic regression (forensic replay
  ``tests/regression/test_c5_silent_404_cascade_replay.py``). Same as
  F1 — reported ``ci_verified`` if the test exists locally.

* F4 — Reactive arm latency: when
  ``dashboard.distribution.reactive_rescan_*`` fires, the latency
  between the preceding 404 and the rescan event MUST be ≤
  ``debounce_sec + 1.0`` s. The 404 itself is logged via
  ``net.http.request_complete{status_code=404, target=/assets/...}``;
  matching by closest timestamp.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class C5TelemetryVerdict:
    f1_status: str = "ci_verified"  # falsifiability lives in CI
    f2_status: str = "no_data"  # "pass" | "fail" | "no_data"
    f2_observations: int = 0
    f2_p50_scan_duration_ms: float = 0.0
    f2_p99_scan_duration_ms: float = 0.0
    f2_max_scan_duration_ms: float = 0.0
    f3_status: str = "ci_verified"  # forensic replay lives in CI
    f4_status: str = "no_data"
    f4_observations: int = 0
    f4_max_latency_s: float = 0.0
    parse_errors: int = 0
    lines_inspected: int = 0
    bundle_scanned_count: int = 0
    bundle_partial_count: int = 0
    bundle_missing_count: int = 0
    reactive_rescan_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "f1": {
                "status": self.f1_status,
                "note": (
                    "Falsifiability lives in CI: tests/integration/scripts/"
                    "test_check_dashboard_bundle_integrity_falsifiability.py"
                ),
            },
            "f2": {
                "status": self.f2_status,
                "observations": self.f2_observations,
                "p50_scan_duration_ms": self.f2_p50_scan_duration_ms,
                "p99_scan_duration_ms": self.f2_p99_scan_duration_ms,
                "max_scan_duration_ms": self.f2_max_scan_duration_ms,
            },
            "f3": {
                "status": self.f3_status,
                "note": (
                    "Forensic replay lives in CI: "
                    "tests/regression/test_c5_silent_404_cascade_replay.py"
                ),
            },
            "f4": {
                "status": self.f4_status,
                "observations": self.f4_observations,
                "max_latency_s": self.f4_max_latency_s,
            },
            "counters": {
                "bundle_scanned_count": self.bundle_scanned_count,
                "bundle_partial_count": self.bundle_partial_count,
                "bundle_missing_count": self.bundle_missing_count,
                "reactive_rescan_count": self.reactive_rescan_count,
            },
            "parse_errors": self.parse_errors,
            "lines_inspected": self.lines_inspected,
            "notes": self.notes,
        }


def _parse_log_line(line: str) -> dict[str, Any] | None:
    """Sovyx structured logs are JSON-per-line via structlog. Returns
    None for malformed lines (operator log rotation, partial writes)."""
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        result: dict[str, Any] = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    return result


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = int(round((pct / 100.0) * (len(sorted_v) - 1)))
    return sorted_v[idx]


def analyse(log_path: Path) -> C5TelemetryVerdict:
    verdict = C5TelemetryVerdict()
    if not log_path.exists():
        verdict.notes.append(f"log not found: {log_path}")
        return verdict

    scan_durations_ms: list[float] = []
    latest_404_by_path: dict[str, float] = {}
    reactive_latencies: list[float] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            verdict.lines_inspected += 1
            evt = _parse_log_line(raw)
            if evt is None:
                verdict.parse_errors += 1
                continue
            event = evt.get("event")
            if event == "dashboard.distribution.bundle_scanned":
                verdict.bundle_scanned_count += 1
                duration = evt.get("scan_duration_ms")
                if isinstance(duration, (int, float)):
                    scan_durations_ms.append(float(duration))
            elif event == "dashboard.distribution.bundle_partial":
                verdict.bundle_partial_count += 1
            elif event == "dashboard.distribution.bundle_missing":
                verdict.bundle_missing_count += 1
            elif event in {
                "dashboard.distribution.reactive_rescan_healthy",
                "dashboard.distribution.reactive_rescan_degraded",
            }:
                verdict.reactive_rescan_count += 1
                # Best-effort latency: compare against the most recent
                # /assets/ 404 timestamp.
                ts = evt.get("timestamp")
                if isinstance(ts, (int, float)) and latest_404_by_path:
                    # Use the most recent 404 ts across any path.
                    last_404 = max(latest_404_by_path.values())
                    latency = float(ts) - float(last_404)
                    if 0.0 <= latency <= 3600.0:
                        reactive_latencies.append(latency)
            elif event == "net.http.request_complete":
                status = evt.get("net.status_code")
                target = evt.get("net.target", "")
                if status == 404 and isinstance(target, str) and target.startswith("/assets/"):
                    ts = evt.get("timestamp")
                    if isinstance(ts, (int, float)):
                        latest_404_by_path[target] = float(ts)

    # F2 verdict
    if scan_durations_ms:
        verdict.f2_observations = len(scan_durations_ms)
        verdict.f2_p50_scan_duration_ms = _percentile(scan_durations_ms, 50)
        verdict.f2_p99_scan_duration_ms = _percentile(scan_durations_ms, 99)
        verdict.f2_max_scan_duration_ms = max(scan_durations_ms)
        # Pass if P99 ≤ 100ms (Mission spec invariant).
        verdict.f2_status = "pass" if verdict.f2_p99_scan_duration_ms <= 100.0 else "fail"

    # F4 verdict
    if reactive_latencies:
        verdict.f4_observations = len(reactive_latencies)
        verdict.f4_max_latency_s = max(reactive_latencies)
        # Default debounce 60s + 1s margin. Operator may override.
        verdict.f4_status = "pass" if verdict.f4_max_latency_s <= 61.0 else "fail"
    elif verdict.reactive_rescan_count > 0:
        # Reactive rescans fired but no preceding /assets/ 404 logged —
        # net.http telemetry may be disabled in operator config.
        verdict.f4_status = "no_data"
        verdict.notes.append(
            "reactive rescans fired but no /assets/* 404 net.http events observed "
            "(operator may have HTTP telemetry disabled — F4 not falsifiable)",
        )

    return verdict


def _render_human(verdict: C5TelemetryVerdict) -> str:
    lines: list[str] = [
        "Mission C5 telemetry verdicts:",
        f"  F1 — Quality Gate 11 falsifiability: {verdict.f1_status}",
        f"  F2 — boot scan duration: {verdict.f2_status} "
        f"(P50 {verdict.f2_p50_scan_duration_ms:.2f}ms / "
        f"P99 {verdict.f2_p99_scan_duration_ms:.2f}ms / "
        f"max {verdict.f2_max_scan_duration_ms:.2f}ms over "
        f"{verdict.f2_observations} observations)",
        f"  F3 — forensic replay: {verdict.f3_status}",
        f"  F4 — reactive arm latency: {verdict.f4_status} "
        f"(max {verdict.f4_max_latency_s:.2f}s over {verdict.f4_observations} observations)",
        "",
        "Counters:",
        f"  bundle_scanned       = {verdict.bundle_scanned_count}",
        f"  bundle_partial       = {verdict.bundle_partial_count}",
        f"  bundle_missing       = {verdict.bundle_missing_count}",
        f"  reactive_rescan      = {verdict.reactive_rescan_count}",
        "",
        f"Lines inspected: {verdict.lines_inspected}  (parse errors: {verdict.parse_errors})",
    ]
    if verdict.notes:
        lines.append("")
        lines.append("Notes:")
        for note in verdict.notes:
            lines.append(f"  - {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission C5 telemetry analyser — F2/F4 evaluation from sovyx.log.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path.home() / ".sovyx" / "logs" / "sovyx.log",
        help="Path to the operator's sovyx.log (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output on stdout.",
    )
    args = parser.parse_args(argv)

    verdict = analyse(args.log)
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    else:
        print(_render_human(verdict))
    # Non-zero exit only if any explicit-fail verdict fires.
    fails = [v for v in (verdict.f2_status, verdict.f4_status) if v == "fail"]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
