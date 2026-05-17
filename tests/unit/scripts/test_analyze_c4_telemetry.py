"""Unit tests for the C4 telemetry analyser.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§9.1 row "analyze_c4_telemetry.py".

Exercises the F3 verdict logic against synthetic log fixtures. F1/F4/F5
return ``no_data`` / ``not_applicable`` until frontend telemetry +
Phase 3 ack-persistence ship; tested separately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dev.analyze_c4_telemetry import _percentile, analyze_log


def _make_log(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    """Write structured log fixtures (one JSON per line)."""
    log = tmp_path / "sovyx.log"
    log.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return log


class TestF3SoftRecoveryGate:
    def test_no_triggers_no_data(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, [])
        verdict = analyze_log(log)
        assert verdict.f3_status == "no_data"
        assert verdict.f3_triggers == 0

    def test_all_successes_pass(self, tmp_path: Path) -> None:
        events = [
            {
                "event": "voice.supervisor.soft_recovery_triggered",
                "voice.mind_id": "test",
            },
            {
                "event": "voice.supervisor.soft_recovery_complete",
                "voice.elapsed_ms": 42,
                "voice.reason": "auto_recovery_governor",
            },
        ]
        log = _make_log(tmp_path, events)
        verdict = analyze_log(log)
        assert verdict.f3_status == "pass"
        assert verdict.f3_success_rate == 1.0
        assert verdict.f3_p50_recovery_ms == 42

    def test_mixed_with_high_success_rate_pass(self, tmp_path: Path) -> None:
        # 19 successes + 1 failure = 95% success rate (just at threshold)
        events = []
        for i in range(20):
            events.append(
                {
                    "event": "voice.supervisor.soft_recovery_triggered",
                    "voice.mind_id": "test",
                    "voice.attempt_index": i,
                },
            )
            if i == 0:
                events.append(
                    {
                        "event": "voice.supervisor.soft_recovery_failed",
                        "voice.error_class": "RuntimeError",
                    },
                )
            else:
                events.append(
                    {
                        "event": "voice.supervisor.soft_recovery_complete",
                        "voice.elapsed_ms": 50 + i,
                    },
                )
        log = _make_log(tmp_path, events)
        verdict = analyze_log(log)
        assert verdict.f3_triggers == 20
        assert verdict.f3_successes == 19
        assert verdict.f3_status == "pass"

    def test_low_success_rate_fail(self, tmp_path: Path) -> None:
        events = [
            {"event": "voice.supervisor.soft_recovery_triggered"},
            {"event": "voice.supervisor.soft_recovery_triggered"},
            {"event": "voice.supervisor.soft_recovery_failed", "voice.error_class": "X"},
            {"event": "voice.supervisor.soft_recovery_failed", "voice.error_class": "Y"},
        ]
        log = _make_log(tmp_path, events)
        verdict = analyze_log(log)
        assert verdict.f3_triggers == 2
        assert verdict.f3_successes == 0
        assert verdict.f3_status == "fail"

    def test_escalation_counted(self, tmp_path: Path) -> None:
        events = [
            {"event": "voice.supervisor.soft_recovery_triggered"},
            {"event": "voice.supervisor.escalation_required"},
        ]
        log = _make_log(tmp_path, events)
        verdict = analyze_log(log)
        assert verdict.f3_escalations == 1

    def test_p99_percentile_computed(self, tmp_path: Path) -> None:
        events = []
        for ms in (10, 20, 30, 40, 50, 100, 200, 500, 1000, 5000):
            events.append({"event": "voice.supervisor.soft_recovery_triggered"})
            events.append(
                {
                    "event": "voice.supervisor.soft_recovery_complete",
                    "voice.elapsed_ms": ms,
                },
            )
        log = _make_log(tmp_path, events)
        verdict = analyze_log(log)
        # P50 should be around the middle (50ms range), P99 near 5000ms
        assert 30 <= verdict.f3_p50_recovery_ms <= 100
        assert verdict.f3_p99_recovery_ms == 5000


class TestPercentileHelper:
    def test_empty_returns_zero(self) -> None:
        assert _percentile([], 0.5) == 0

    def test_single_value(self) -> None:
        assert _percentile([42], 0.5) == 42

    def test_p50_of_ordered_list(self) -> None:
        assert _percentile([10, 20, 30, 40, 50], 0.5) == 30


class TestMalformedLog:
    def test_missing_log_file_returns_empty_verdict(self, tmp_path: Path) -> None:
        verdict = analyze_log(tmp_path / "does_not_exist.log")
        # No crash; verdict reports as no_data + notes
        assert verdict.f3_status == "no_data"
        assert any("log_read_failed" in n for n in verdict.notes)

    def test_non_json_lines_silently_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "mixed.log"
        log.write_text(
            "not json line\n"
            + json.dumps({"event": "voice.supervisor.soft_recovery_triggered"})
            + "\n"
            + "garbage\n"
            + json.dumps(
                {"event": "voice.supervisor.soft_recovery_complete", "voice.elapsed_ms": 100}
            )
            + "\n",
            encoding="utf-8",
        )
        verdict = analyze_log(log)
        assert verdict.f3_triggers == 1
        assert verdict.f3_successes == 1

    def test_f4_f5_not_applicable_phase_3(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, [])
        verdict = analyze_log(log)
        # Phase 3 ack persistence not yet shipped
        assert verdict.f4_status == "not_applicable"
        assert verdict.f5_status == "not_applicable"


@pytest.mark.parametrize(
    "events,expected_status",
    [
        ([], "no_data"),
        (
            [
                {"event": "voice.supervisor.soft_recovery_triggered"},
                {"event": "voice.supervisor.soft_recovery_complete", "voice.elapsed_ms": 50},
            ],
            "pass",
        ),
        (
            [
                {"event": "voice.supervisor.soft_recovery_triggered"},
                {"event": "voice.supervisor.soft_recovery_failed", "voice.error_class": "Test"},
            ],
            "fail",
        ),
    ],
)
def test_f3_status_table(
    tmp_path: Path,
    events: list[dict[str, object]],
    expected_status: str,
) -> None:
    log = _make_log(tmp_path, events)
    verdict = analyze_log(log)
    assert verdict.f3_status == expected_status
