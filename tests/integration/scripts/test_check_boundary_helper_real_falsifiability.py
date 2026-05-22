"""Falsifiability tests — Mission C Gate 20 boundary helper realism.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.0 +
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 20.

These tests EXIST to be the operational proof that:

1. A synthetic ``assert_boundary_accepts(..., helper_factory=lambda)``
   call is flagged as a violation in STRICT mode.
2. Named callables (``helper_factory=_real``) pass.
3. The allowlist marker (``# c-allowlist: boundary_helper_lambda
   reason=<...>``) silences a single violation.
4. The current real-world baseline matches the Mission C audit
   estimate (51 lambda boundary helpers across 5 files).

If any test here regresses, Gate 20 is broken — DO NOT silence it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNER = _REPO_ROOT / "scripts" / "dev" / "check_boundary_helper_real.py"


def _run_scanner(
    scan_root: Path,
    *,
    strict: bool = True,
) -> tuple[int, dict[str, object]]:
    args = [sys.executable, str(_SCANNER), "--scan-root", str(scan_root), "--json"]
    if strict:
        args.append("--strict")
    result = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result.returncode, payload


class TestGate20Falsifiability:
    def test_synthetic_lambda_helper_strict_fails(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_synth():
    assert_boundary_accepts(
        SomeModel,
        helper_factory=lambda: {"a": 1},
    )
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc != 0
        assert payload["violation_count"] == 1

    def test_named_helper_passes(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def _real():
    return {"a": 1}

def test_compliant():
    assert_boundary_accepts(
        SomeModel,
        helper_factory=_real,
    )
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0
        assert payload["call_sites_with_named_helper"] == 1

    def test_attribute_helper_passes(self, tmp_path: Path) -> None:
        """`helper_factory=builder.payload` is a named attribute call,
        not a lambda; legitimate."""
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_compliant():
    assert_boundary_accepts(
        SomeModel,
        helper_factory=builder.payload,
    )
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0

    def test_allowlist_silences_violation(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_intentional_lambda():
    assert_boundary_accepts(  # c-allowlist: boundary_helper_lambda reason=Test-only synthetic shape
        SomeModel,
        helper_factory=lambda: {"a": 1},
    )
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0
        assert payload["call_sites_allowlisted"] == 1

    def test_non_boundary_helper_call_ignored(self, tmp_path: Path) -> None:
        """A `helper_factory=lambda:` passed to a function OTHER than
        `assert_boundary_accepts` is out of scope."""
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_x():
    some_other_helper(helper_factory=lambda: {"x": 1})
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0
        assert payload["call_sites_inspected"] == 0

    def test_positional_helper_factory_lambda_flagged(self, tmp_path: Path) -> None:
        """Even if helper_factory is passed positionally (rare),
        an inline lambda is still flagged."""
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_positional():
    assert_boundary_accepts(SomeModel, lambda: {"a": 1})
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc != 0
        assert payload["violation_count"] == 1

    def test_lenient_mode_exits_zero_with_violations(
        self,
        tmp_path: Path,
    ) -> None:
        scan_root = tmp_path / "tests"
        scan_root.mkdir()
        (scan_root / "test_x_boundary.py").write_text(
            """\
def test_synth():
    assert_boundary_accepts(SomeModel, helper_factory=lambda: {})
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=False)
        assert rc == 0
        assert payload["passed"] is False
        assert payload["violation_count"] == 1


class TestGate20CurrentBaseline:
    """Anchor the current real-world baseline."""

    def test_dashboard_baseline_matches_audit(self) -> None:
        """Mission C audit estimated 51 lambda boundary helpers across
        5 files. The exact number is the LENIENT baseline; assert it
        does NOT exceed 60 (a regression-budget headroom). Phase C.6
        body migration progressively closes lambdas; this assertion
        tightens as the work lands."""
        from scripts.dev.check_boundary_helper_real import scan_dir

        report = scan_dir()
        assert report.files_scanned > 0
        # Pre-mission baseline is 51; allow up to 60 as regression
        # budget. If a body-migration commit drops violations, the
        # gate STILL passes — this assertion only fires when the
        # backlog GROWS, not shrinks.
        assert len(report.violations) <= 60, (
            f"Regression budget exceeded: {len(report.violations)} lambda "
            "boundary helpers (pre-C.0 baseline was 51). Either migrate "
            "the new lambdas to named callables or add a "
            "`# c-allowlist: boundary_helper_lambda reason=<...>` marker."
        )
