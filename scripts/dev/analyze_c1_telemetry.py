#!/usr/bin/env python3
"""Mission C1 Phase 3 — telemetry calibration analyzer.

Parses the operator's structlog JSON log and computes the
falsifiability gates documented in
``docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md``
§3 + §7:

* **F1 gate** — ratio
  ``coordinator.benign_skip{vad_mute} / integrity.verdict{vad_mute,
  phase=pre_bypass}`` ≥ 0.9 across ≥ 20 deaf-signal events. Target:
  the new VAD_MUTE benign-skip dispatch (T1.3 §20.A) catches the
  same population that was previously misclassified as APO_DEGRADED
  pre-mission.
* **F2 gate** — already validated in CI synthetic tests at T1.4
  completion. This analyzer just notes the contract; no runtime
  data needed.
* **F3 gate** — operator's "fala e LLM não responde" event on
  hardware shows the expected verdict + outcome shape. Analyzer
  surfaces the `voice_vad_frontend_reset_*` event inventory + the
  `derived_reason` distribution on quarantine + failover events so
  the operator's narrative can be cross-checked against the
  structured surface.

Usage::

    uv run python scripts/dev/analyze_c1_telemetry.py
    uv run python scripts/dev/analyze_c1_telemetry.py --log /path/to/sovyx.log
    uv run python scripts/dev/analyze_c1_telemetry.py --since 2026-05-15T00:00:00Z
    uv run python scripts/dev/analyze_c1_telemetry.py --json

The script is committed (per ``feedback_no_inline_scripts_in_chat``)
so the operator can re-run it across the v0.44.x telemetry window
without inline heredoc / shell improvisation. Read-only — never
mutates the log or any sovyx state.

Designed for ``~/.sovyx/logs/sovyx.log`` (the default file handler
target per ``feedback_canonical_setup_paths``); pass ``--log`` for
log rotation or alternate ``$SOVYX_HOME`` setups.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


_DEFAULT_LOG = Path.home() / ".sovyx" / "logs" / "sovyx.log"

# Mission C1 event-name inventory. Adding events here MUST stay in
# sync with the source-of-truth emission sites — grep the codebase
# for each name before editing.
_EVENT_BENIGN_SKIP = "capture_integrity_coordinator_benign_skip"
_EVENT_INTEGRITY_VERDICT = "capture_integrity_verdict"
_EVENT_LADDER_PREFIX = "voice.vad_frontend_reset."
_EVENT_LADDER_ACTIVATED = "voice_vad_frontend_reset_activated"
_EVENT_QUARANTINED = "capture_integrity_coordinator_quarantined"
_EVENT_FAILOVER_ATTEMPTED = "voice.failover.attempted"
_EVENT_CASCADE_DISPATCH = "capture_integrity_coordinator_request_cascade_reevaluation"
_EVENT_NORMALIZER_DISPATCH = "capture_integrity_coordinator_request_normalizer_engagement"
_EVENT_APO_BYPASS_ACTIVATED = "voice_apo_bypass_activated"
_EVENT_APO_BYPASS_INEFFECTIVE = "voice_apo_bypass_ineffective"


# Mission §3 gate target. F1 passes iff ratio ≥ 0.9 across ≥ 20
# deaf-signal events. Either threshold failed → calibration window
# stays open (one more minor cycle).
_F1_RATIO_TARGET = 0.9
_F1_MIN_EVENTS = 20


def _iter_log_records(log_path: Path) -> Iterator[dict[str, object]]:
    """Stream every JSON-decodable line from ``log_path``.

    Non-JSON lines (dev-console / mixed-stream noise) are silently
    skipped; malformed JSON gets the same treatment. The structlog
    JSON file handler emits one object per line per
    ``feedback_canonical_setup_paths``, so the happy path is
    ``object_per_line``.
    """
    if not log_path.exists():
        msg = f"log file not found: {log_path}"
        raise SystemExit(msg)
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or not stripped.startswith("{"):
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _filter_by_since(
    records: Iterable[dict[str, object]],
    since: datetime | None,
) -> Iterator[dict[str, object]]:
    """Drop records whose ``timestamp`` predates ``since``.

    Records without a parseable timestamp are passed through (the
    user wanted to widen the window; better to over-include than
    silently drop). Sovyx structlog emits ISO-8601 ``"timestamp"``;
    the JSON file handler decorates that field consistently per the
    canonical setup paths.
    """
    if since is None:
        yield from records
        return
    for record in records:
        ts = record.get("timestamp")
        if not isinstance(ts, str):
            yield record
            continue
        try:
            record_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            yield record
            continue
        if record_dt >= since:
            yield record


def _build_summary(records: Iterable[dict[str, object]]) -> dict[str, object]:
    """Return a structured summary of the C1 telemetry surface.

    Single pass over the records — avoids re-iterating a long log.
    Counters are bounded by the structured event-name inventory so
    memory is O(events_found), not O(log_lines).
    """
    benign_skip_by_verdict: Counter[str] = Counter()
    vad_mute_verdict_count = 0
    ladder_events: list[tuple[str, str, str]] = []  # (timestamp, event, step)
    ladder_activated_count = 0
    quarantine_by_reason: Counter[str] = Counter()
    failover_by_derived_reason: Counter[str] = Counter()
    cascade_dispatch_count = 0
    normalizer_dispatch_count = 0
    apo_bypass_activated = 0
    apo_bypass_ineffective = 0

    for record in records:
        event = record.get("event")
        if not isinstance(event, str):
            continue
        if event == _EVENT_BENIGN_SKIP:
            verdict = record.get("verdict")
            if isinstance(verdict, str):
                benign_skip_by_verdict[verdict] += 1
        elif event == _EVENT_INTEGRITY_VERDICT:
            verdict = record.get("verdict")
            phase = record.get("phase")
            if verdict == "vad_mute" and phase == "pre_bypass":
                vad_mute_verdict_count += 1
        elif event.startswith(_EVENT_LADDER_PREFIX):
            ts = record.get("timestamp", "")
            step = record.get("step", "")
            ladder_events.append(
                (str(ts), event, str(step) if step else ""),
            )
        elif event == _EVENT_LADDER_ACTIVATED:
            ladder_activated_count += 1
        elif event == _EVENT_QUARANTINED:
            reason = record.get("derived_reason") or record.get("reason")
            if isinstance(reason, str):
                quarantine_by_reason[reason] += 1
            else:
                quarantine_by_reason["unknown"] += 1
        elif event == _EVENT_FAILOVER_ATTEMPTED:
            derived = record.get("voice.derived_reason")
            legacy = record.get("voice.legacy_reason")
            tag = derived or legacy or "unknown"
            if isinstance(tag, str):
                failover_by_derived_reason[tag] += 1
        elif event == _EVENT_CASCADE_DISPATCH:
            cascade_dispatch_count += 1
        elif event == _EVENT_NORMALIZER_DISPATCH:
            normalizer_dispatch_count += 1
        elif event == _EVENT_APO_BYPASS_ACTIVATED:
            apo_bypass_activated += 1
        elif event == _EVENT_APO_BYPASS_INEFFECTIVE:
            apo_bypass_ineffective += 1

    # Mission §3 F1 gate.
    f1_ratio = (
        benign_skip_by_verdict.get("vad_mute", 0) / vad_mute_verdict_count
        if vad_mute_verdict_count > 0
        else 0.0
    )
    f1_total = vad_mute_verdict_count
    f1_passes = (f1_ratio >= _F1_RATIO_TARGET) and (f1_total >= _F1_MIN_EVENTS)
    if f1_total == 0:
        f1_verdict = "insufficient_data"
    elif f1_total < _F1_MIN_EVENTS:
        f1_verdict = f"insufficient_events ({f1_total}/{_F1_MIN_EVENTS})"
    elif f1_passes:
        f1_verdict = "pass"
    else:
        f1_verdict = f"fail (ratio={f1_ratio:.3f} < {_F1_RATIO_TARGET})"

    # Mission §3 F3 gate.
    if ladder_activated_count == 0 and not ladder_events:
        f3_verdict = "no_ladder_evidence"
    elif ladder_activated_count > 0:
        f3_verdict = f"pass ({ladder_activated_count} ladder recoveries)"
    else:
        f3_verdict = f"ladder_attempted_no_recovery ({len(ladder_events)} step events)"

    return {
        "f1_gate": {
            "verdict": f1_verdict,
            "numerator_benign_skip_vad_mute": benign_skip_by_verdict.get("vad_mute", 0),
            "denominator_vad_mute_pre_bypass": f1_total,
            "ratio": round(f1_ratio, 3),
            "target_ratio": _F1_RATIO_TARGET,
            "target_min_events": _F1_MIN_EVENTS,
        },
        "f2_gate": {
            "verdict": "CI-validated",
            "note": (
                "F2 (history-window classifier) is validated by the "
                "unit tests at tests/unit/voice/health/test_c1_dispatch.py; "
                "no runtime data needed."
            ),
        },
        "f3_gate": {
            "verdict": f3_verdict,
            "ladder_activated_total": ladder_activated_count,
            "ladder_step_events_total": len(ladder_events),
            "ladder_events_first_5": ladder_events[:5],
        },
        "benign_skip_distribution": dict(benign_skip_by_verdict),
        "quarantine_by_reason": dict(quarantine_by_reason),
        "failover_by_derived_reason": dict(failover_by_derived_reason),
        "coordinator_dispatch": {
            "cascade_reevaluation_requested": cascade_dispatch_count,
            "normalizer_engagement_requested": normalizer_dispatch_count,
        },
        "legacy_apo_bypass_outcomes": {
            "activated": apo_bypass_activated,
            "ineffective": apo_bypass_ineffective,
        },
    }


def _render_text(summary: dict[str, object]) -> str:
    """Format the summary as a terse human-readable report."""
    lines = ["Mission C1 Phase 3 -- telemetry analyzer", "=" * 50, ""]

    f1 = summary["f1_gate"]
    if isinstance(f1, dict):
        lines.extend(
            [
                f"F1 gate: {f1['verdict']}",
                f"  benign_skip{{vad_mute}} = {f1['numerator_benign_skip_vad_mute']}",
                f"  integrity.verdict{{vad_mute, pre_bypass}} = "
                f"{f1['denominator_vad_mute_pre_bypass']}",
                f"  ratio = {f1['ratio']} (target >= {f1['target_ratio']}, "
                f"need >= {f1['target_min_events']} events)",
                "",
            ],
        )

    f2 = summary["f2_gate"]
    if isinstance(f2, dict):
        lines.extend([f"F2 gate: {f2['verdict']} -- {f2['note']}", ""])

    f3 = summary["f3_gate"]
    if isinstance(f3, dict):
        lines.extend(
            [
                f"F3 gate: {f3['verdict']}",
                f"  ladder activations = {f3['ladder_activated_total']}",
                f"  ladder step events = {f3['ladder_step_events_total']}",
            ],
        )
        events = f3.get("ladder_events_first_5")
        if isinstance(events, list) and events:
            lines.append("  first 5 step events:")
            for ts, event, step in events:
                lines.append(f"    [{ts}] {event} step={step or '-'}")
        lines.append("")

    lines.append("Quarantine entries by derived_reason:")
    quarantine = summary["quarantine_by_reason"]
    if isinstance(quarantine, dict) and quarantine:
        for reason, count in sorted(quarantine.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason}: {count}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Failover attempts by derived_reason:")
    failover = summary["failover_by_derived_reason"]
    if isinstance(failover, dict) and failover:
        for reason, count in sorted(failover.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason}: {count}")
    else:
        lines.append("  (none)")
    lines.append("")

    dispatch = summary["coordinator_dispatch"]
    if isinstance(dispatch, dict):
        lines.extend(
            [
                "Coordinator dispatch (non-strategy outcomes):",
                f"  cascade_reevaluation_requested = {dispatch['cascade_reevaluation_requested']}",
                f"  normalizer_engagement_requested = "
                f"{dispatch['normalizer_engagement_requested']}",
                "",
            ],
        )

    legacy = summary["legacy_apo_bypass_outcomes"]
    if isinstance(legacy, dict):
        lines.extend(
            [
                "Legacy APO bypass (pre-mission baseline):",
                f"  voice_apo_bypass_activated   = {legacy['activated']}",
                f"  voice_apo_bypass_ineffective = {legacy['ineffective']}",
            ],
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission C1 Phase 3 telemetry calibration analyzer",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=_DEFAULT_LOG,
        help=f"Path to sovyx.log (default: {_DEFAULT_LOG}).",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "ISO-8601 timestamp lower bound; records before this point are "
            "ignored. Example: 2026-05-15T00:00:00Z."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured summary as JSON (for programmatic use).",
    )
    args = parser.parse_args(argv)

    since: datetime | None = None
    if args.since is not None:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError as exc:
            print(f"error: --since is not valid ISO-8601: {exc}", file=sys.stderr)
            return 2

    records = _iter_log_records(args.log)
    records = _filter_by_since(records, since)
    summary = _build_summary(records)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_render_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
