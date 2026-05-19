"""Unit tests for Mission H3 §T1.3 AST scanner —
:mod:`scripts.dev.check_quarantine_reason_discipline`.

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T1.6.

Covers:

* Compliant shapes (QuarantineReason member / resolver call / field
  passthrough / lifecycle-tag literal with allowlist).
* Violation detection on terminal-classification literals + unknown
  literals + non-SSoT expressions.
* Inline allowlist comment recognition (same line / previous line).
* File-level allowlist (the SSoT module itself).
* JSON output shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNER_PATH = _REPO_ROOT / "scripts" / "dev" / "check_quarantine_reason_discipline.py"


def _load_scanner_module() -> object:
    """Load the scanner via importlib because ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "check_quarantine_reason_discipline", _SCANNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scanner_module() -> object:
    return _load_scanner_module()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Write a synthetic source file under ``tmp_path/src/sovyx/``."""
    root = tmp_path / "src" / "sovyx"
    root.mkdir(parents=True, exist_ok=True)
    file = root / name
    file.write_text(body, encoding="utf-8")
    return file


class TestCompliantShapes:
    def test_enum_member_is_compliant(self, scanner_module, tmp_path: Path) -> None:
        source = """\
from sovyx.voice.health._quarantine_reasons import QuarantineReason

def fire(quarantine):
    quarantine.add(
        endpoint_guid="x",
        reason=QuarantineReason.APO_DEGRADED,
    )
"""
        _write(tmp_path, "compliant_enum.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed
        assert report.calls_inspected == 1

    def test_enum_member_value_is_compliant(self, scanner_module, tmp_path: Path) -> None:
        source = """\
from sovyx.voice.health._quarantine_reasons import QuarantineReason

def fire(quarantine):
    quarantine.add(
        endpoint_guid="x",
        reason=QuarantineReason.CAPTURE_DEAD.value,
    )
"""
        _write(tmp_path, "compliant_value.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed

    def test_resolver_call_is_compliant(self, scanner_module, tmp_path: Path) -> None:
        source = """\
from sovyx.voice.health._quarantine_reasons import resolve_reason_from_verdict

def fire(quarantine, verdict):
    quarantine.add(
        endpoint_guid="x",
        reason=resolve_reason_from_verdict(verdict),
    )
"""
        _write(tmp_path, "compliant_resolver.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed

    def test_resolver_call_value_is_compliant(self, scanner_module, tmp_path: Path) -> None:
        source = """\
from sovyx.voice.health._quarantine_reasons import resolve_reason_from_diagnosis

def fire(quarantine, diagnosis):
    quarantine.add(
        endpoint_guid="x",
        reason=resolve_reason_from_diagnosis(diagnosis).value,
    )
"""
        _write(tmp_path, "compliant_resolver_value.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed

    def test_field_passthrough_is_compliant(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def reAdd(quarantine, entry):
    quarantine.add(
        endpoint_guid=entry.endpoint_guid,
        reason=entry.resolved_reason,
    )
"""
        _write(tmp_path, "compliant_passthrough.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed

    def test_lifecycle_tag_with_allowlist_is_compliant(
        self, scanner_module, tmp_path: Path
    ) -> None:
        source = """\
def reAdd(quarantine):
    # h3-allowlist: lifecycle-tag — watchdog recheck re-add
    quarantine.add(endpoint_guid="x", reason="watchdog_recheck")
"""
        _write(tmp_path, "compliant_lifecycle.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed

    def test_lifecycle_tag_with_same_line_allowlist(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def reAdd(quarantine):
    quarantine.add(endpoint_guid="x", reason="factory_integration")  # h3-allowlist: boot cascade
"""
        _write(tmp_path, "compliant_lifecycle_sameline.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed


class TestViolations:
    def test_terminal_literal_is_violation(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def fire(quarantine):
    quarantine.add(endpoint_guid="x", reason="apo_degraded")
"""
        _write(tmp_path, "violation_terminal.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert not report.passed
        assert len(report.violations) == 1
        assert report.violations[0].kind == "literal_terminal"

    def test_unknown_literal_is_violation(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def fire(quarantine):
    quarantine.add(endpoint_guid="x", reason="totally_made_up")
"""
        _write(tmp_path, "violation_unknown.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert not report.passed
        assert report.violations[0].kind == "literal_unknown"

    def test_lifecycle_literal_without_allowlist_is_violation(
        self, scanner_module, tmp_path: Path
    ) -> None:
        source = """\
def reAdd(quarantine):
    quarantine.add(endpoint_guid="x", reason="watchdog_recheck")
"""
        _write(tmp_path, "violation_lifecycle_no_allowlist.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert not report.passed
        assert report.violations[0].kind == "literal_lifecycle_without_allowlist"

    def test_non_ssot_expression_is_violation(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def fire(quarantine, reason_var):
    quarantine.add(endpoint_guid="x", reason=reason_var)
"""
        _write(tmp_path, "violation_var.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert not report.passed
        assert report.violations[0].kind == "non_ssot_expr"

    def test_non_quarantine_receiver_ignored(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def fire(other):
    other.add(endpoint_guid="x", reason="anything")
"""
        _write(tmp_path, "non_quarantine.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        assert report.passed
        assert report.calls_inspected == 0


class TestFileAllowlist:
    def test_ssot_module_is_allowlisted(self, scanner_module, tmp_path: Path) -> None:
        # Replicate the SSoT module path under the synthetic repo root.
        root = tmp_path / "src" / "sovyx" / "voice" / "health"
        root.mkdir(parents=True)
        (root / "_quarantine_reasons.py").write_text(
            'def f(quarantine):\n    quarantine.add(endpoint_guid="x", reason="anything")\n',
            encoding="utf-8",
        )
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        # The allowlist short-circuits the scan for this file.
        assert report.passed


class TestJsonOutput:
    def test_json_round_trip(self, scanner_module, tmp_path: Path) -> None:
        source = """\
def fire(quarantine):
    quarantine.add(endpoint_guid="x", reason="apo_degraded")
"""
        _write(tmp_path, "for_json.py", source)
        report = scanner_module.run_check(tmp_path / "src" / "sovyx", repo_root=tmp_path)
        as_dict = report.to_dict()
        assert as_dict["passed"] is False
        assert as_dict["violation_count"] == 1
        v0 = as_dict["violations"][0]
        assert v0["kind"] == "literal_terminal"
        assert v0["receiver"] == "quarantine"
        assert v0["method"] == "add"


class TestCli:
    def test_strict_exits_nonzero_on_violation(
        self, scanner_module, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(
            tmp_path,
            "cli_violation.py",
            'def f(quarantine):\n    quarantine.add(endpoint_guid="x", reason="apo_degraded")\n',
        )
        exit_code = scanner_module.main(
            [
                "--scan-root",
                str(tmp_path / "src" / "sovyx"),
                "--strict",
                "--repo-root",
                str(tmp_path),
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "violation" in captured.out.lower()

    def test_lenient_exits_zero_on_violation(
        self, scanner_module, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(
            tmp_path,
            "cli_violation_lenient.py",
            'def f(quarantine):\n    quarantine.add(endpoint_guid="x", reason="apo_degraded")\n',
        )
        exit_code = scanner_module.main(
            ["--scan-root", str(tmp_path / "src" / "sovyx"), "--repo-root", str(tmp_path)]
        )
        assert exit_code == 0  # LENIENT report-only

    def test_json_flag(
        self, scanner_module, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(
            tmp_path,
            "cli_for_json.py",
            'def f(quarantine):\n    quarantine.add(endpoint_guid="x", reason="apo_degraded")\n',
        )
        exit_code = scanner_module.main(
            [
                "--scan-root",
                str(tmp_path / "src" / "sovyx"),
                "--json",
                "--repo-root",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()
        import json

        payload = json.loads(captured.out)
        assert payload["passed"] is False
        assert payload["violation_count"] == 1
        assert exit_code == 0  # LENIENT default
