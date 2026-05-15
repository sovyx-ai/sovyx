"""Tests for ``scripts/dev/analyze_c1_telemetry.py``.

The script is operator-facing infrastructure for Mission C1 Phase 3
telemetry calibration. Tests pin the parsing contract + gate
classification logic against synthetic log fixtures so future
emission-site renames or schema drifts surface as test failures
rather than silent telemetry blindness.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from types import ModuleType


_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "dev" / "analyze_c1_telemetry.py"


@pytest.fixture(scope="module")
def analyzer() -> ModuleType:
    """Import the script as a module (it lives under ``scripts/`` which
    isn't on ``PYTHONPATH``).

    The cast to ``ModuleType`` is for mypy strict — ``importlib.util``
    returns ``ModuleType | None`` semantically but the load path
    guarantees non-None when the spec is built from a known file.
    """
    spec = importlib.util.spec_from_file_location(
        "analyze_c1_telemetry",
        _SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        msg = f"cannot import script at {_SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_log(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """Materialise structlog-shaped JSON-per-line records to disk."""
    log_path = tmp_path / "sovyx.log"
    with log_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")
    return log_path


def _evt(event: str, /, **fields: Any) -> dict[str, object]:  # noqa: ANN401
    """Build a structlog-shaped record dict with ``event`` + extras."""
    record: dict[str, object] = {"event": event}
    record.update(fields)
    return record


class TestF1GateClassification:
    """F1 gate verdict bands."""

    def test_insufficient_data_when_no_vad_mute_records(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        log = _write_log(tmp_path, [_evt("voice.startup")])
        records = analyzer._iter_log_records(log)
        summary = analyzer._build_summary(records)
        f1 = cast("dict[str, object]", summary["f1_gate"])
        assert f1["verdict"] == "insufficient_data"
        assert f1["denominator_vad_mute_pre_bypass"] == 0

    def test_insufficient_events_when_below_threshold(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        records = [
            _evt(
                "capture_integrity_verdict",
                verdict="vad_mute",
                phase="pre_bypass",
            )
        ] * 5
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f1 = cast("dict[str, object]", summary["f1_gate"])
        assert "insufficient_events" in str(f1["verdict"])

    def test_pass_when_ratio_and_volume_meet_targets(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        # 25 vad_mute pre-bypass verdicts, 24 paired benign-skip events
        # (ratio = 0.96 ≥ 0.9, volume 25 ≥ 20 — both gates pass).
        records: list[dict[str, object]] = []
        for _ in range(25):
            records.append(
                _evt(
                    "capture_integrity_verdict",
                    verdict="vad_mute",
                    phase="pre_bypass",
                ),
            )
        for _ in range(24):
            records.append(
                _evt(
                    "capture_integrity_coordinator_benign_skip",
                    verdict="vad_mute",
                ),
            )
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f1 = cast("dict[str, object]", summary["f1_gate"])
        assert f1["verdict"] == "pass"
        assert f1["ratio"] == 0.96  # noqa: PLR2004

    def test_fail_when_ratio_below_target(self, analyzer: ModuleType, tmp_path: Path) -> None:
        # 20 pre-bypass verdicts, only 10 benign-skip (ratio=0.5, fail).
        records: list[dict[str, object]] = []
        for _ in range(20):
            records.append(
                _evt(
                    "capture_integrity_verdict",
                    verdict="vad_mute",
                    phase="pre_bypass",
                ),
            )
        for _ in range(10):
            records.append(
                _evt(
                    "capture_integrity_coordinator_benign_skip",
                    verdict="vad_mute",
                ),
            )
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f1 = cast("dict[str, object]", summary["f1_gate"])
        assert str(f1["verdict"]).startswith("fail")


class TestF3GateClassification:
    """F3 gate verdict bands."""

    def test_no_ladder_evidence_when_logs_silent(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        log = _write_log(tmp_path, [_evt("voice.startup")])
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f3 = cast("dict[str, object]", summary["f3_gate"])
        assert f3["verdict"] == "no_ladder_evidence"

    def test_pass_when_ladder_activates(self, analyzer: ModuleType, tmp_path: Path) -> None:
        records = [
            _evt(
                "voice.vad_frontend_reset.recovered",
                step="silero_reset",
                timestamp="2026-05-15T00:00:01Z",
            ),
            _evt(
                "voice_vad_frontend_reset_activated",
                step="silero_reset",
            ),
        ]
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f3 = cast("dict[str, object]", summary["f3_gate"])
        assert str(f3["verdict"]).startswith("pass")
        assert f3["ladder_activated_total"] == 1

    def test_ladder_attempted_no_recovery(self, analyzer: ModuleType, tmp_path: Path) -> None:
        records = [
            _evt(
                "voice.vad_frontend_reset.step_crashed",
                step="silero_reset",
                timestamp="2026-05-15T00:00:01Z",
            ),
            _evt(
                "voice.vad_frontend_reset.exhausted",
                timestamp="2026-05-15T00:00:02Z",
            ),
        ]
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        f3 = cast("dict[str, object]", summary["f3_gate"])
        assert "ladder_attempted_no_recovery" in str(f3["verdict"])


class TestDerivedReasonAttribution:
    """Quarantine + failover distributions split by derived_reason."""

    def test_quarantine_prefers_derived_reason(self, analyzer: ModuleType, tmp_path: Path) -> None:
        records = [
            _evt(
                "capture_integrity_coordinator_quarantined",
                reason="apo_degraded",
                derived_reason="vad_frontend_dead",
            ),
            _evt(
                "capture_integrity_coordinator_quarantined",
                reason="apo_degraded",
                derived_reason="apo_degraded",
            ),
        ]
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        dist = cast("dict[str, int]", summary["quarantine_by_reason"])
        assert dist["vad_frontend_dead"] == 1
        assert dist["apo_degraded"] == 1

    def test_failover_attribution_via_voice_derived_reason(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        records = [
            _evt(
                "voice.failover.attempted",
                **{
                    "voice.legacy_reason": "apo_degraded",
                    "voice.derived_reason": "format_mismatch",
                },
            ),
        ]
        log = _write_log(tmp_path, records)
        summary = analyzer._build_summary(analyzer._iter_log_records(log))
        dist = cast("dict[str, int]", summary["failover_by_derived_reason"])
        assert dist == {"format_mismatch": 1}


class TestLogPathHandling:
    """File-system robustness."""

    def test_missing_log_raises_system_exit(self, analyzer: ModuleType, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.log"
        with pytest.raises(SystemExit):
            list(analyzer._iter_log_records(missing))

    def test_skips_non_json_lines(self, analyzer: ModuleType, tmp_path: Path) -> None:
        log_path = tmp_path / "mixed.log"
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write("not-json\n")
            fh.write('{"event": "voice.startup"}\n')
            fh.write("\n")
            fh.write("[1, 2, 3]\n")  # JSON array — skipped (not dict)
            fh.write('{"event": "voice.shutdown"}\n')
        events = [r.get("event") for r in analyzer._iter_log_records(log_path)]
        assert events == ["voice.startup", "voice.shutdown"]


class TestSinceFilter:
    """Time-window filtering."""

    def test_drops_records_before_since(self, analyzer: ModuleType, tmp_path: Path) -> None:
        from datetime import datetime

        records = [
            _evt("voice.startup", timestamp="2026-05-14T12:00:00Z"),
            _evt("voice.shutdown", timestamp="2026-05-15T18:00:00Z"),
        ]
        log = _write_log(tmp_path, records)
        since = datetime(2026, 5, 15, tzinfo=UTC)
        filtered = list(
            analyzer._filter_by_since(analyzer._iter_log_records(log), since),
        )
        events = [r.get("event") for r in filtered]
        assert events == ["voice.shutdown"]

    def test_unparseable_timestamp_records_pass_through(
        self, analyzer: ModuleType, tmp_path: Path
    ) -> None:
        from datetime import datetime

        records = [
            _evt("voice.startup", timestamp="not-a-timestamp"),
        ]
        log = _write_log(tmp_path, records)
        since = datetime(2026, 5, 15, tzinfo=UTC)
        filtered = list(
            analyzer._filter_by_since(analyzer._iter_log_records(log), since),
        )
        assert len(filtered) == 1
