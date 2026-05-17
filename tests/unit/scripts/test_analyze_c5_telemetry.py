"""Unit tests for scripts/dev/analyze_c5_telemetry.py (Mission C5 §T2.7).

These tests pin the F2 / F4 verdict computation against synthetic log
fixtures so the analyzer's parsing + percentile logic remains stable
across operator-side telemetry refreshes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZER_PATH = REPO_ROOT / "scripts" / "dev" / "analyze_c5_telemetry.py"


@pytest.fixture(scope="module")
def analyzer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analyze_c5_telemetry", ANALYZER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_log(tmp: Path, events: list[dict[str, object]]) -> Path:
    path = tmp / "sovyx.log"
    with path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt))
            fh.write("\n")
    return path


def test_missing_log_returns_no_data(analyzer: ModuleType, tmp_path: Path) -> None:
    verdict = analyzer.analyse(tmp_path / "absent.log")
    assert verdict.f2_status == "no_data"
    assert verdict.f4_status == "no_data"
    assert verdict.lines_inspected == 0
    assert any("log not found" in note for note in verdict.notes)


def test_f2_pass_when_scans_under_100ms(analyzer: ModuleType, tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            {
                "event": "dashboard.distribution.bundle_scanned",
                "verdict": "fully_present",
                "scan_duration_ms": 12.5,
            },
            {
                "event": "dashboard.distribution.bundle_scanned",
                "verdict": "fully_present",
                "scan_duration_ms": 8.1,
            },
            {
                "event": "dashboard.distribution.bundle_scanned",
                "verdict": "fully_present",
                "scan_duration_ms": 42.0,
            },
        ],
    )
    verdict = analyzer.analyse(log)
    assert verdict.f2_status == "pass"
    assert verdict.f2_observations == 3
    assert verdict.f2_max_scan_duration_ms == 42.0
    assert verdict.bundle_scanned_count == 3


def test_f2_fails_when_p99_exceeds_100ms(analyzer: ModuleType, tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            {
                "event": "dashboard.distribution.bundle_scanned",
                "verdict": "partial",
                "scan_duration_ms": 250.0,
            },
        ],
    )
    verdict = analyzer.analyse(log)
    assert verdict.f2_status == "fail"
    assert verdict.f2_max_scan_duration_ms == 250.0


def test_counters_track_partial_and_missing(analyzer: ModuleType, tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            {"event": "dashboard.distribution.bundle_scanned", "scan_duration_ms": 5.0},
            {"event": "dashboard.distribution.bundle_partial"},
            {"event": "dashboard.distribution.bundle_partial"},
            {"event": "dashboard.distribution.bundle_missing"},
            {"event": "dashboard.distribution.reactive_rescan_healthy"},
            {"event": "dashboard.distribution.reactive_rescan_degraded"},
        ],
    )
    verdict = analyzer.analyse(log)
    assert verdict.bundle_partial_count == 2
    assert verdict.bundle_missing_count == 1
    assert verdict.reactive_rescan_count == 2


def test_parse_errors_counted(analyzer: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "sovyx.log"
    path.write_text(
        '{"event": "ok"}\n'
        "not-json\n"
        '{"unclosed":\n'
        '{"event": "dashboard.distribution.bundle_scanned", "scan_duration_ms": 1.0}\n',
        encoding="utf-8",
    )
    verdict = analyzer.analyse(path)
    assert verdict.parse_errors >= 1
    assert verdict.bundle_scanned_count == 1


def test_main_json_output_parseable(
    analyzer: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = _write_log(
        tmp_path,
        [{"event": "dashboard.distribution.bundle_scanned", "scan_duration_ms": 4.0}],
    )
    rc = analyzer.main(["--log", str(log), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["f2"]["status"] == "pass"
    assert payload["counters"]["bundle_scanned_count"] == 1


def test_main_exit_code_nonzero_on_explicit_fail(
    analyzer: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = _write_log(
        tmp_path,
        [{"event": "dashboard.distribution.bundle_scanned", "scan_duration_ms": 250.0}],
    )
    rc = analyzer.main(["--log", str(log)])
    capsys.readouterr()  # drain
    assert rc != 0
