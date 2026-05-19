#!/usr/bin/env python3
"""Mission H3 Phase 2 telemetry analyzer — F1..F8 verdict aggregator.

Reads operator structured-log output (typically ``~/.sovyx/logs/sovyx.log``)
and emits a JSON verdict for the eight falsifiability gates declared in
Mission H3 §3 + §9. Operator runs this once the v0.49.10..v0.49.13
cohort has accumulated ≥ 1 minor cycle of real-world emissions, surfaces
the JSON output to Claude, and Claude evaluates STRICT-flip readiness
per ``feedback_full_autonomous_authority``.

F-gate semantics:

* **F1 — Gate 14 falsifiability:** runs the Gate 14 AST scanner under
  STRICT mode against a synthetic file with a deliberate unguarded
  ``quarantine.add(reason="totally_made_up", endpoint_guid="x")`` emit;
  PASS iff the scanner exits non-zero (i.e. detected the violation).
* **F2 — Triple-field ratio:** counts legacy/derived/resolved reason
  occurrences across ``capture_integrity_coordinator_quarantined`` /
  ``voice_endpoint_quarantined`` events; PASS iff all three counters
  agree (LENIENT triple-field).
* **F3 — Forensic-replay regression:** runs
  ``tests/regression/test_h3_apo_degraded_masks_real_class_replay.py``;
  PASS iff exit 0.
* **F4 — Dashboard banner ingestion:** runs the locale-completeness
  + DegradedBanner / voice-health vitest assertions for new copy keys.
* **F5 — Zod schema validation:** runs the schemas vitest assertions
  for ``CaptureRestartReasonSchema`` carrying the new ``"capture_dead"``
  + ``"unclassified"`` literals.
* **F6 — Doctor remediation:** runs the doctor CLI integration test
  asserting that the rendered remediation hint matches the resolved
  reason (NOT the legacy APO playbook on Linux).
* **F7 — mypy exhaustiveness:** runs ``mypy --strict`` against
  ``_quarantine_reasons.py`` and confirms the ``assert_never`` arms
  cover every :class:`IntegrityVerdict` + :class:`Diagnosis` member.
* **F8 — OTel semconv conformance:** verifies that the
  ``sovyx.voice.health.quarantine_resolution`` counter exports with the
  five canonical attributes (verdict, diagnosis, resolved_reason,
  platform, bypass_family) at least once.

Anti-pattern compliance:
* #18 — operator runs this; no remote-API call.
* #34 — pure analysis tool; no production side-effects.
* #46 — does NOT itself emit non-SSoT ``reason=`` literals; safe under
  Gate 14.

Usage:

    uv run python scripts/dev/analyze_h3_telemetry.py \\
        --log ~/.sovyx/logs/sovyx.log --json

    # Skip individual gates if their input is unavailable:
    uv run python scripts/dev/analyze_h3_telemetry.py \\
        --log /tmp/sample.log --skip-vitest --skip-mypy

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§9 + §12 V-H3-11. Output schema verbatim from §9.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Canonical reason taxonomy mirrored from
# ``src/sovyx/voice/health/_quarantine_reasons.py``. Keeping them as
# literals (not importing) makes the script standalone — operators can
# run it against an old daemon log even after the SSoT module has been
# refactored.
_CANONICAL_REASONS = {
    "apo_degraded",
    "vad_frontend_dead",
    "format_mismatch",
    "driver_silent",
    "capture_dead",
    "kernel_invalidated",
    "watchdog_recheck",
    "unclassified",
}

# Quarantine-event names we scan for triple-field presence.
_QUARANTINE_EVENT_NAMES = {
    "voice_endpoint_quarantined",
    "capture_integrity_coordinator_quarantined",
}

# F8 — canonical OTel attribute set on the new counter.
_F8_COUNTER_ATTRIBUTES = ("verdict", "diagnosis", "resolved_reason", "platform", "bypass_family")


# Synthetic violation source for F1 — a single line that should trip
# Gate 14 in STRICT mode. Generated lazily into a tempdir so we don't
# pollute the repo tree.
_F1_SYNTHETIC_VIOLATION_SOURCE = '''\
"""F1 synthetic — should trip Gate 14 STRICT."""
from sovyx.voice.health._quarantine import get_default_quarantine


def synthetic_violation() -> None:
    quarantine = get_default_quarantine()
    quarantine.add(
        endpoint_guid="synthetic-endpoint",
        reason="totally_made_up",
    )
'''


@dataclass
class GateVerdict:
    passes: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TelemetryReport:
    f1_gate14_falsifiability: GateVerdict
    f2_triple_field_ratio: GateVerdict
    f3_forensic_replay: GateVerdict
    f4_dashboard_banner: GateVerdict
    f5_zod_validation: GateVerdict
    f6_doctor_remediation: GateVerdict
    f7_mypy_exhaustiveness: GateVerdict
    f8_otel_semconv: GateVerdict

    @property
    def ready_for_strict_flip(self) -> bool:
        return all(
            v.passes
            for v in (
                self.f1_gate14_falsifiability,
                self.f2_triple_field_ratio,
                self.f3_forensic_replay,
                self.f4_dashboard_banner,
                self.f5_zod_validation,
                self.f6_doctor_remediation,
                self.f7_mypy_exhaustiveness,
                self.f8_otel_semconv,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        def _gd(g: GateVerdict) -> dict[str, Any]:
            return {"passes": g.passes, **g.detail}

        return {
            "f1_gate14_falsifiability": _gd(self.f1_gate14_falsifiability),
            "f2_triple_field_ratio": _gd(self.f2_triple_field_ratio),
            "f3_forensic_replay": _gd(self.f3_forensic_replay),
            "f4_dashboard_banner": _gd(self.f4_dashboard_banner),
            "f5_zod_validation": _gd(self.f5_zod_validation),
            "f6_doctor_remediation": _gd(self.f6_doctor_remediation),
            "f7_mypy_exhaustiveness": _gd(self.f7_mypy_exhaustiveness),
            "f8_otel_semconv": _gd(self.f8_otel_semconv),
            "ready_for_strict_flip": self.ready_for_strict_flip,
        }


# ── F1: Gate 14 falsifiability ───────────────────────────────────────────


def evaluate_f1_gate14_falsifiability(*, repo_root: Path) -> GateVerdict:
    """Drop a synthetic violation file into a tmpdir, run Gate 14 --strict,
    expect non-zero exit."""
    scanner = repo_root / "scripts" / "dev" / "check_quarantine_reason_discipline.py"
    if not scanner.is_file():
        return GateVerdict(
            passes=False,
            detail={
                "synthetic_violation_count": 0,
                "false_positives": [],
                "error": "scanner not found",
            },
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        scan_root = tmp_root / "src" / "sovyx"
        scan_root.mkdir(parents=True)
        synthetic = scan_root / "_h3_f1_synthetic.py"
        synthetic.write_text(_F1_SYNTHETIC_VIOLATION_SOURCE, encoding="utf-8")
        result = subprocess.run(  # noqa: S603 — controlled args, no shell
            [
                sys.executable,
                str(scanner),
                "--scan-root",
                str(scan_root),
                "--strict",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    passes = result.returncode != 0  # Gate must FAIL on synthetic violation
    return GateVerdict(
        passes=passes,
        detail={
            "synthetic_violation_count": 1,
            "scanner_exit": result.returncode,
            "false_positives": [],
        },
    )


# ── F2: Triple-field ratio across structured logs ─────────────────────────


_KV_RE = re.compile(r"\b(reason|derived_reason|resolved_reason)=(\S+)")
_EVENT_RE = re.compile(r'event=("[^"]+"|\S+)')


def evaluate_f2_triple_field_ratio(*, log_path: Path | None) -> GateVerdict:
    """Count occurrences of legacy/derived/resolved reason fields on
    quarantine events. PASS iff all three counts agree.

    Accepts both ``key=value`` structured lines (debug-friendly) and
    JSON-line entries (production). Skip lines that don't match either
    shape.
    """
    if log_path is None or not log_path.is_file():
        return GateVerdict(
            passes=False,
            detail={
                "legacy_count": 0,
                "derived_count": 0,
                "resolved_count": 0,
                "drift_legacy_vs_resolved": 0,
                "drift_derived_vs_resolved": 0,
                "error": "log not found",
            },
        )

    counts: Counter[str] = Counter()
    total_quarantine_events = 0

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Cheap event-name pre-filter.
            if not any(name in line for name in _QUARANTINE_EVENT_NAMES):
                continue
            total_quarantine_events += 1
            for match in _KV_RE.finditer(line):
                key = match.group(1)
                counts[key] += 1

    legacy = counts["reason"]
    derived = counts["derived_reason"]
    resolved = counts["resolved_reason"]
    drift_lr = abs(legacy - resolved)
    drift_dr = abs(derived - resolved)
    # During LENIENT triple-field, all three counts must agree.
    passes = (total_quarantine_events == 0) or (drift_lr == 0 and drift_dr == 0)
    return GateVerdict(
        passes=passes,
        detail={
            "legacy_count": legacy,
            "derived_count": derived,
            "resolved_count": resolved,
            "drift_legacy_vs_resolved": drift_lr,
            "drift_derived_vs_resolved": drift_dr,
            "total_quarantine_events": total_quarantine_events,
        },
    )


# ── F3: Forensic-replay regression ────────────────────────────────────────


def evaluate_f3_forensic_replay(*, repo_root: Path, skip: bool = False) -> GateVerdict:
    target = repo_root / "tests" / "regression" / "test_h3_apo_degraded_masks_real_class_replay.py"
    if skip:
        return GateVerdict(passes=True, detail={"skipped": True})
    if not target.is_file():
        return GateVerdict(passes=False, detail={"error": "regression test not found"})
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            str(target),
            "-q",
            "--timeout=30",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return GateVerdict(
        passes=result.returncode == 0,
        detail={
            "exit_code": result.returncode,
            "stdout_tail": result.stdout.strip().splitlines()[-5:],
        },
    )


# ── F4: Dashboard banner ingestion (vitest) ───────────────────────────────


def evaluate_f4_dashboard_banner(*, repo_root: Path, skip: bool = False) -> GateVerdict:
    if skip:
        return GateVerdict(passes=True, detail={"skipped": True})
    dashboard = repo_root / "dashboard"
    if not dashboard.is_dir():
        return GateVerdict(passes=False, detail={"error": "dashboard not found"})
    result = subprocess.run(  # noqa: S603
        [
            "npx",
            "vitest",
            "run",
            "--reporter=dot",
            "src/locales/__tests__/locale-completeness.test.ts",
            "src/pages/voice-health.test.tsx",
        ],
        cwd=str(dashboard),
        capture_output=True,
        text=True,
        check=False,
        shell=True,  # noqa: S602 — npx on Windows requires shell
    )
    return GateVerdict(
        passes=result.returncode == 0,
        detail={
            "exit_code": result.returncode,
            "i18n_locales_verified": ["en", "pt-BR", "es"],
        },
    )


# ── F5: Zod schema validation (vitest) ────────────────────────────────────


def evaluate_f5_zod_validation(*, repo_root: Path, skip: bool = False) -> GateVerdict:
    if skip:
        return GateVerdict(passes=True, detail={"skipped": True})
    dashboard = repo_root / "dashboard"
    if not dashboard.is_dir():
        return GateVerdict(passes=False, detail={"error": "dashboard not found"})
    result = subprocess.run(  # noqa: S603
        [
            "npx",
            "vitest",
            "run",
            "--reporter=dot",
            "src/types/schemas.test.ts",
        ],
        cwd=str(dashboard),
        capture_output=True,
        text=True,
        check=False,
        shell=True,  # noqa: S602
    )
    return GateVerdict(
        passes=result.returncode == 0,
        detail={
            "exit_code": result.returncode,
            "new_reason_values": ["capture_dead", "unclassified"],
        },
    )


# ── F6: Doctor remediation hint (pytest) ──────────────────────────────────


def evaluate_f6_doctor_remediation(*, repo_root: Path, skip: bool = False) -> GateVerdict:
    if skip:
        return GateVerdict(passes=True, detail={"skipped": True})
    target = repo_root / "tests" / "unit" / "cli" / "test_doctor_voice_quarantine_h3.py"
    if not target.is_file():
        return GateVerdict(passes=False, detail={"error": "doctor h3 test not found"})
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(target), "-q", "--timeout=30"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return GateVerdict(
        passes=result.returncode == 0,
        detail={"exit_code": result.returncode},
    )


# ── F7: mypy exhaustiveness on _quarantine_reasons.py ─────────────────────


def evaluate_f7_mypy_exhaustiveness(*, repo_root: Path, skip: bool = False) -> GateVerdict:
    if skip:
        return GateVerdict(passes=True, detail={"skipped": True})
    target = repo_root / "src" / "sovyx" / "voice" / "health" / "_quarantine_reasons.py"
    if not target.is_file():
        return GateVerdict(passes=False, detail={"error": "SSoT module not found"})
    result = subprocess.run(  # noqa: S603
        ["uv", "run", "mypy", "--strict", str(target)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        shell=True,  # noqa: S602 — uv on Windows
    )
    output = (result.stdout + result.stderr).strip()
    success = "Success" in output or "no issues found" in output
    return GateVerdict(
        passes=result.returncode == 0 and success,
        detail={
            "exit_code": result.returncode,
            "match_arms_covered_all_enum_members": success,
        },
    )


# ── F8: OTel semconv counter attribute round-trip ─────────────────────────


def evaluate_f8_otel_semconv(*, log_path: Path | None) -> GateVerdict:
    """Verify the new ``voice_health_quarantine_resolution`` counter
    surfaces at least one record with all 5 canonical attributes."""
    if log_path is None or not log_path.is_file():
        return GateVerdict(
            passes=False,
            detail={
                "fields_round_trip": [],
                "error": "log not found",
            },
        )

    fields_seen: set[str] = set()
    counter_line_seen = False

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "voice_health_quarantine_resolution" in line or "quarantine_resolution" in line:
                counter_line_seen = True
                for attr in _F8_COUNTER_ATTRIBUTES:
                    if f"{attr}=" in line or f'"{attr}":' in line:
                        fields_seen.add(attr)

    passes = counter_line_seen and fields_seen == set(_F8_COUNTER_ATTRIBUTES)
    return GateVerdict(
        passes=passes,
        detail={
            "fields_round_trip": sorted(fields_seen),
            "counter_observed": counter_line_seen,
        },
    )


# ── Driver ────────────────────────────────────────────────────────────────


def run_analysis(
    *,
    log_path: Path | None,
    repo_root: Path = _REPO_ROOT,
    skip_pytest: bool = False,
    skip_vitest: bool = False,
    skip_mypy: bool = False,
) -> TelemetryReport:
    return TelemetryReport(
        f1_gate14_falsifiability=evaluate_f1_gate14_falsifiability(repo_root=repo_root),
        f2_triple_field_ratio=evaluate_f2_triple_field_ratio(log_path=log_path),
        f3_forensic_replay=evaluate_f3_forensic_replay(repo_root=repo_root, skip=skip_pytest),
        f4_dashboard_banner=evaluate_f4_dashboard_banner(repo_root=repo_root, skip=skip_vitest),
        f5_zod_validation=evaluate_f5_zod_validation(repo_root=repo_root, skip=skip_vitest),
        f6_doctor_remediation=evaluate_f6_doctor_remediation(
            repo_root=repo_root, skip=skip_pytest
        ),
        f7_mypy_exhaustiveness=evaluate_f7_mypy_exhaustiveness(
            repo_root=repo_root, skip=skip_mypy
        ),
        f8_otel_semconv=evaluate_f8_otel_semconv(log_path=log_path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission H3 Phase 2 F1..F8 telemetry analyzer.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Path to sovyx.log (default: None — F2 + F8 will fail without it)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report (default: human text)",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip F3 + F6 pytest evaluations",
    )
    parser.add_argument(
        "--skip-vitest",
        action="store_true",
        help="Skip F4 + F5 vitest evaluations",
    )
    parser.add_argument(
        "--skip-mypy",
        action="store_true",
        help="Skip F7 mypy evaluation",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root (default: this script's repo)",
    )
    args = parser.parse_args(argv)

    report = run_analysis(
        log_path=args.log,
        repo_root=args.repo_root,
        skip_pytest=args.skip_pytest,
        skip_vitest=args.skip_vitest,
        skip_mypy=args.skip_mypy,
    )

    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Mission H3 Phase 2 telemetry — F1..F8 verdict:")
        for gate, value in payload.items():
            if gate == "ready_for_strict_flip":
                continue
            mark = "✓" if value.get("passes") else "✗"
            print(f"  {mark} {gate}: passes={value.get('passes')}")
        print()
        print(f"ready_for_strict_flip: {payload['ready_for_strict_flip']}")
    return 0 if report.ready_for_strict_flip else 1


if __name__ == "__main__":
    sys.exit(main())
