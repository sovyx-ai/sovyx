#!/usr/bin/env python3
"""Quality Gate 18 — boundary helper must be a real producer callable,
not an inline lambda (Mission C §C.0).

Static AST scanner that walks every ``tests/**/test_*boundary*.py``
file and reports every ``assert_boundary_accepts(..., helper_factory=
lambda: {...})`` call site. The helper_factory parameter should be a
NAMED callable that mirrors the actual producer's runtime-bound dict
shape — anti-pattern #40 is built on the assumption that the boundary
test exercises the SAME shape the producer emits. An inline lambda
encodes the test-author's MEMORY of the producer shape; a producer
rename leaves the boundary test passing against a stale lambda
(the F-FAL-2 / F-RPL-4 / proposed anti-pattern #60 surface).

The Mission C2 ``_boundary_helpers.py:19`` docstring explicitly calls
this out: the helper "MUST mirror the real producer's runtime-bound
output, not the constructor-time defaults."

Compliant shapes:

* ``helper_factory=_named_function`` — passes a real callable by name.
* ``helper_factory=<obj>.method`` — bound method or attribute call
  that yields the dict.

Violating shapes:

* ``helper_factory=lambda: {...}`` — inline anonymous lambda.
* ``helper_factory=lambda *, k=v: {...}`` — any lambda.

LENIENT semantics:

* The gate ALWAYS exits 0 unless ``--strict`` (or
  ``SOVYX_C_GATE_STRICT=1``) is passed.
* Pre-mission baseline: 51 violations across 5 boundary test files
  (Mission C audit Gate 20 §17). The body migration is Phase C.6+
  work; LENIENT prevents NEW lambda factories from sliding in.

Allowlist:

* Inline marker ``# c-allowlist: boundary_helper_lambda reason=<text>``
  on the call site or the immediately-preceding line. Use sparingly —
  legitimate reasons are essentially nil; the gate exists precisely
  because lambda helpers ARE the anti-pattern.

Anti-pattern compliance:

* #20 — scanner lives under ``scripts/dev/``.
* #40 (sibling — boundary tests assume real-producer round-trip).
* #60 (proposed; closure via this gate's STRICT-flip).

Mission anchor:
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 20 +
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.0.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SCAN_ROOT = _REPO_ROOT / "tests"


# Function names whose ``helper_factory=`` kwarg the gate inspects.
# Currently only the dashboard boundary helper; future helpers can be
# added here.
_BOUNDARY_HELPER_FUNC_NAMES: Final[frozenset[str]] = frozenset(
    {"assert_boundary_accepts"},
)


# Inline allowlist marker. Either on the same line as the call site OR
# the immediately-preceding line.
_ALLOWLIST_RE: Final[re.Pattern[str]] = re.compile(
    r"#\s*c-allowlist:\s*boundary_helper_lambda\s+reason=\S",
)


@dataclass(frozen=True, slots=True)
class _LambdaHelper:
    """A call site passing ``helper_factory=lambda: ...``."""

    file_path: str
    line: int
    column: int
    helper_func: str


@dataclass
class GateReport:
    files_scanned: int = 0
    call_sites_inspected: int = 0
    call_sites_with_named_helper: int = 0
    call_sites_allowlisted: int = 0
    violations: list[_LambdaHelper] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "call_sites_inspected": self.call_sites_inspected,
            "call_sites_with_named_helper": self.call_sites_with_named_helper,
            "call_sites_allowlisted": self.call_sites_allowlisted,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "column": v.column,
                    "helper_func": v.helper_func,
                }
                for v in self.violations
            ],
            "skipped_files": self.skipped_files,
        }


def _call_func_name(call: ast.Call) -> str | None:
    """Return the rightmost name segment of the called function, or None
    if the callee is not a Name/Attribute shape."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _has_allowlist_marker(source_lines: list[str], call_lineno: int) -> bool:
    if call_lineno < 1 or call_lineno > len(source_lines):
        return False
    if _ALLOWLIST_RE.search(source_lines[call_lineno - 1]):
        return True
    return call_lineno >= 2 and bool(
        _ALLOWLIST_RE.search(source_lines[call_lineno - 2]),
    )


def _scan_file(path: Path, report: GateReport) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.skipped_files.append(f"{path}: read failed: {exc!r}")
        return
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        report.skipped_files.append(f"{path}: parse failed: {exc!r}")
        return
    report.files_scanned += 1
    source_lines = source.splitlines()
    if path.is_absolute():
        try:
            rel: Path = path.relative_to(_REPO_ROOT)
        except ValueError:
            rel = path
    else:
        rel = path
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _call_func_name(node)
        if func_name not in _BOUNDARY_HELPER_FUNC_NAMES:
            continue
        # Find the helper_factory kwarg.
        helper_kw = next(
            (kw for kw in node.keywords if kw.arg == "helper_factory"),
            None,
        )
        if helper_kw is None:
            # The helper may be positional — check positional arg #1
            # (signature is (model_cls, helper_factory, *, ...)). When
            # both are positional, the second arg is helper_factory.
            if len(node.args) < 2:
                continue
            value: ast.expr = node.args[1]
        else:
            value = helper_kw.value
        report.call_sites_inspected += 1
        if isinstance(value, ast.Lambda):
            if _has_allowlist_marker(source_lines, node.lineno):
                report.call_sites_allowlisted += 1
                continue
            report.violations.append(
                _LambdaHelper(
                    file_path=str(rel).replace("\\", "/"),
                    line=node.lineno,
                    column=node.col_offset,
                    helper_func=func_name or "?",
                ),
            )
        else:
            # Named callable (Name, Attribute, Call, etc.) — counts as
            # compliant. The named target may itself be a lambda
            # assigned to a variable, but that's a separate code-style
            # nudge — the gate's job is the inline-lambda anti-pattern.
            report.call_sites_with_named_helper += 1


def scan_dir(scan_root: Path = _DEFAULT_SCAN_ROOT) -> GateReport:
    report = GateReport()
    if not scan_root.exists():
        report.skipped_files.append(f"scan root missing: {scan_root}")
        return report
    for path in sorted(scan_root.rglob("test_*boundary*.py")):
        _scan_file(path, report)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mission C Gate 20 — boundary helper must be a real callable. "
            "LENIENT by default; STRICT via --strict or "
            "SOVYX_C_GATE_STRICT=1."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any violation.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report on stdout.",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=_DEFAULT_SCAN_ROOT,
        help="Override the tests scan root.",
    )
    return parser


def _render_human(report: GateReport) -> str:
    lines: list[str] = []
    lines.append(
        f"Gate 20 — boundary helper realism: "
        f"{report.files_scanned} file(s), "
        f"{report.call_sites_inspected} call site(s) inspected, "
        f"{report.call_sites_with_named_helper} compliant, "
        f"{report.call_sites_allowlisted} allowlisted.",
    )
    if report.skipped_files:
        lines.append("Skipped files:")
        for entry in report.skipped_files:
            lines.append(f"  - {entry}")
    if report.violations:
        lines.append(
            f"{len(report.violations)} violation(s) — inline "
            "`helper_factory=lambda:` (anti-pattern #60 surface):",
        )
        by_file: dict[str, list[_LambdaHelper]] = {}
        for v in report.violations:
            by_file.setdefault(v.file_path, []).append(v)
        for fp, vs in by_file.items():
            lines.append(
                f"  {fp}: {len(vs)} lambda(s) at line(s) {', '.join(str(v.line) for v in vs)}"
            )
        lines.append("boundary helper discipline: VIOLATIONS")
    else:
        lines.append("boundary helper discipline: PASS")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    report = scan_dir(scan_root=args.scan_root)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_render_human(report))
    strict = args.strict or os.environ.get("SOVYX_C_GATE_STRICT") == "1"
    if strict and not report.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
