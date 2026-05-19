#!/usr/bin/env python3
"""Quality Gate 14 — quarantine reason discipline (Mission H3 §T1.3).

Static AST scanner that walks every ``*.py`` under ``src/sovyx/`` and
reports every ``*.add(..., reason=..., ...)`` call site where the
receiver name contains ``quarantine`` AND the ``reason=`` keyword
argument's value is NOT one of the four permitted shapes:

1. **Enum member or attribute access** — ``QuarantineReason.<MEMBER>``
   (StrEnum, implicit ``str()`` coercion) OR
   ``QuarantineReason.<MEMBER>.value`` (explicit string).
2. **SSoT resolver call** — ``resolve_reason_from_verdict(...)`` /
   ``resolve_reason_from_diagnosis(...)`` returning a
   :class:`QuarantineReason` member OR ``.value`` access on its result.
3. **Field passthrough** — ``entry.resolved_reason`` /
   ``.derived_reason`` / ``.reason`` attribute access on an existing
   :class:`QuarantineEntry` (legitimate during re-add / lifecycle re-tag).
4. **Allowlisted literal** — a string literal recognised as a lifecycle
   tag (``"watchdog_recheck"`` / ``"factory_integration"`` /
   ``"probe_pinned"`` / ``"probe_store"`` / ``"probe_cascade"`` /
   ``"kernel_invalidated_recheck"`` / ``"probe"``) accompanied by an
   inline ``# h3-allowlist: <rationale>`` comment on the same line OR
   the immediately-preceding line.

Exits non-zero on any violation in STRICT mode (``--strict`` flag or
``SOVYX_H3_GATE_STRICT=1``); LENIENT mode (default) prints the report
but exits 0 — used by ``scripts/verify_gates.sh`` during v0.49.10..v0.52.x
staged adoption. ``.github/workflows/publish.yml`` runs ``--strict``
post-build. Phase 3 v0.53.0 promotes the local gate to STRICT.

Anti-pattern compliance:

* #20 — checker lives under ``scripts/dev/`` (operational tooling), not
  inside production code; consumers patch via ``patch.object(module,
  "_scan_file")`` against this module path.
* #46 — the canonical instance the gate enforces (acceptance-gate output
  field discipline via SSoT verdict→reason resolver).

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T1.3. Counterfactual F1 in §3 is verified by the integration test
``tests/integration/scripts/test_check_quarantine_reason_discipline_falsifiability.py``.
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
_DEFAULT_SCAN_ROOT = _REPO_ROOT / "src" / "sovyx"


# Lifecycle-tag literals — NOT terminal verdict classifications. Mission
# H3 §0 #5: these are the only string literals permitted as ``reason=``
# values, and only when accompanied by an explicit inline allowlist
# comment.
_LIFECYCLE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "watchdog_recheck",
        "factory_integration",
        "probe_pinned",
        "probe_store",
        "probe_cascade",
        "kernel_invalidated_recheck",
        "probe",
    },
)


# Canonical reason values (mirrored from
# :class:`sovyx.voice.health._quarantine_reasons.QuarantineReason`). A
# string literal that matches one of these is a terminal verdict whose
# call site MUST migrate to the SSoT resolver; "literal_terminal"
# violations indicate a known taxonomy member written as a raw string.
_KNOWN_TERMINAL_REASONS: Final[frozenset[str]] = frozenset(
    {
        "apo_degraded",
        "vad_frontend_dead",
        "format_mismatch",
        "driver_silent",
        "capture_dead",
        "kernel_invalidated",
        "unclassified",
    },
)


# Inline allowlist token. Either on the same line as the violating call
# OR the immediately-preceding line.
_ALLOWLIST_RE: Final[re.Pattern[str]] = re.compile(r"#\s*h3-allowlist:\s*\S")


# Receiver name fragments that identify quarantine call targets. Matches
# any attribute call whose receiver name contains one of these tokens
# (case-insensitive). Configurable via ``--quarantine-receiver-tokens``
# for tests / future extensions.
_DEFAULT_QUARANTINE_RECEIVER_TOKENS: Final[frozenset[str]] = frozenset(
    {"quarantine", "_quarantine"},
)


# Method names we scan on quarantine receivers. The acceptance-gate
# write path is ``.add(...)``; ``.clear`` / ``.purge`` / ``.snapshot``
# don't accept a ``reason=`` kwarg of interest.
_DEFAULT_QUARANTINE_METHODS: Final[frozenset[str]] = frozenset({"add"})


# SSoT resolver function names — calls to these in the ``reason=``
# expression are recognised as compliant. Includes both the canonical
# names and `.value` chained-access form (handled by AST inspection,
# not name list).
_DEFAULT_RESOLVER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "resolve_reason_from_verdict",
        "resolve_reason_from_diagnosis",
    },
)


# Field names on QuarantineEntry that are legitimate ``reason=``
# expressions during re-add / lifecycle-tag passthrough.
_DEFAULT_ENTRY_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {"reason", "derived_reason", "resolved_reason"},
)


# Files explicitly allowlisted. The SSoT module + the resolver tests
# legitimately use literals to verify the enum's value strings.
_FILE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "src/sovyx/voice/health/_quarantine_reasons.py",
    },
)


@dataclass(frozen=True, slots=True)
class Violation:
    """A non-compliant ``reason=`` value at a quarantine ``.add(...)`` call."""

    file_path: str
    line: int
    column: int
    receiver: str
    method: str
    reason_repr: str
    kind: str  # "literal_terminal" | "literal_unknown" | "non_ssot_expr"


@dataclass
class GateReport:
    """Aggregate scanner report — mirrors the C5 Gate 11 / C6 Gate 12 / H2 Gate 13 shape."""

    files_scanned: int = 0
    calls_inspected: int = 0
    violations: list[Violation] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "calls_inspected": self.calls_inspected,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "column": v.column,
                    "receiver": v.receiver,
                    "method": v.method,
                    "reason_repr": v.reason_repr,
                    "kind": v.kind,
                }
                for v in self.violations
            ],
            "skipped_files": self.skipped_files,
        }


def _receiver_token_match(name: str, tokens: frozenset[str]) -> bool:
    lower = name.lower()
    return any(tok in lower for tok in tokens)


def _classify_reason_expr(
    expr: ast.expr,
    *,
    resolver_names: frozenset[str],
    entry_field_names: frozenset[str],
) -> tuple[str, str]:
    """Return ``(kind, repr)`` for an AST expression passed as ``reason=``.

    ``kind`` is one of:
    - ``"enum_member"`` — ``QuarantineReason.X`` or ``QuarantineReason.X.value``.
    - ``"resolver_call"`` — call to ``resolve_reason_from_verdict`` /
      ``resolve_reason_from_diagnosis`` (optionally chained ``.value``).
    - ``"field_passthrough"`` — attribute access on an entry's ``reason``
      / ``derived_reason`` / ``resolved_reason`` field.
    - ``"literal_lifecycle"`` — string literal matching a lifecycle tag.
    - ``"literal_terminal"`` — string literal NOT matching a lifecycle tag
      (e.g. a terminal verdict written as raw string — VIOLATION).
    - ``"literal_unknown"`` — string literal that's neither lifecycle nor
      a recognisable terminal verdict (treated as VIOLATION; new
      lifecycle tags MUST be added to ``_LIFECYCLE_TAGS``).
    - ``"non_ssot_expr"`` — any other expression shape (variable, dict
      lookup, conditional, …) — VIOLATION, must go through the SSoT.

    ``repr`` is a short human-readable rendering of the expression.
    """
    try:
        rendered = ast.unparse(expr)
    except Exception:  # noqa: BLE001 — defensive on weird AST shapes
        rendered = "<unrenderable>"

    # 1. Plain string literal.
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        if expr.value in _LIFECYCLE_TAGS:
            return "literal_lifecycle", repr(expr.value)
        if expr.value in _KNOWN_TERMINAL_REASONS:
            return "literal_terminal", repr(expr.value)
        return "literal_unknown", repr(expr.value)

    # 2. QuarantineReason.X / QuarantineReason.X.value / resolver(...).value.
    if isinstance(expr, ast.Attribute):
        # ``.value`` chain — unwrap one level for both nested Attribute
        # (``QuarantineReason.X.value``) AND nested Call (``resolver(...).value``).
        if expr.attr == "value" and isinstance(expr.value, (ast.Attribute, ast.Call)):
            return _classify_reason_expr(
                expr.value,
                resolver_names=resolver_names,
                entry_field_names=entry_field_names,
            )
        # Top-level ``QuarantineReason.<MEMBER>``.
        if isinstance(expr.value, ast.Name) and expr.value.id == "QuarantineReason":
            return "enum_member", rendered
        # ``something.reason`` / ``.derived_reason`` / ``.resolved_reason`` —
        # treat as field passthrough regardless of the receiver name.
        if expr.attr in entry_field_names:
            return "field_passthrough", rendered

    # 3. resolve_reason_from_verdict(...) / resolve_reason_from_diagnosis(...).
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name) and func.id in resolver_names:
            return "resolver_call", rendered
        if isinstance(func, ast.Attribute) and func.attr in resolver_names:
            return "resolver_call", rendered

    return "non_ssot_expr", rendered


def _line_has_allowlist(source_lines: list[str], line_no: int) -> bool:
    """Return True iff the current OR previous source line carries the allowlist token."""
    if line_no <= 0 or line_no > len(source_lines):
        return False
    if _ALLOWLIST_RE.search(source_lines[line_no - 1]):
        return True
    return bool(line_no >= 2 and _ALLOWLIST_RE.search(source_lines[line_no - 2]))


def _is_quarantine_add_call(
    call: ast.Call,
    *,
    receiver_tokens: frozenset[str],
    method_names: frozenset[str],
) -> tuple[str, str] | None:
    """Return ``(receiver_repr, method_name)`` if this call targets ``*.<method>(...)``.

    Recognises:
    - ``obj.add(...)`` where ``obj`` is a :class:`Name` whose id contains
      a quarantine token.
    - ``self._quarantine.add(...)`` / ``self.quarantine.add(...)`` etc.
    - ``get_default_quarantine().add(...)`` — receiver token in the func
      name.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in method_names:
        return None
    receiver = func.value
    # Compute a stable receiver string for matching token presence.
    try:
        receiver_repr = ast.unparse(receiver)
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not _receiver_token_match(receiver_repr, receiver_tokens):
        return None
    return receiver_repr, func.attr


def _extract_reason_kwarg(call: ast.Call) -> ast.expr | None:
    """Return the ``reason=`` keyword value, or None if not passed."""
    for kw in call.keywords:
        if kw.arg == "reason":
            return kw.value
    return None


def _scan_file(
    file_path: Path,
    *,
    receiver_tokens: frozenset[str],
    method_names: frozenset[str],
    resolver_names: frozenset[str],
    entry_field_names: frozenset[str],
    repo_root: Path,
) -> tuple[int, list[Violation]]:
    """Scan one Python file. Returns ``(calls_inspected, violations)``."""
    try:
        rel_path = file_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()

    if rel_path in _FILE_ALLOWLIST:
        return 0, []

    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError:
        return 0, []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return 0, []

    source_lines = source.splitlines()

    calls_inspected = 0
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        receiver_match = _is_quarantine_add_call(
            node,
            receiver_tokens=receiver_tokens,
            method_names=method_names,
        )
        if receiver_match is None:
            continue
        receiver_repr, method_name = receiver_match
        reason_expr = _extract_reason_kwarg(node)
        if reason_expr is None:
            # No reason= passed — uses ``EndpointQuarantine.add``'s default.
            # The default itself is policed by reviewing _quarantine.py
            # source (always pass reason explicitly post-H3) — outside
            # scanner scope here.
            continue
        calls_inspected += 1

        kind, rendered = _classify_reason_expr(
            reason_expr,
            resolver_names=resolver_names,
            entry_field_names=entry_field_names,
        )

        # Compliant shapes — no violation.
        if kind in ("enum_member", "resolver_call", "field_passthrough"):
            continue

        # Lifecycle literal requires explicit allowlist.
        if kind == "literal_lifecycle":
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            violations.append(
                Violation(
                    file_path=rel_path,
                    line=node.lineno,
                    column=node.col_offset,
                    receiver=receiver_repr,
                    method=method_name,
                    reason_repr=rendered,
                    kind="literal_lifecycle_without_allowlist",
                ),
            )
            continue

        # Terminal-classification literal or unknown literal — VIOLATION.
        if kind == "literal_terminal":
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            violations.append(
                Violation(
                    file_path=rel_path,
                    line=node.lineno,
                    column=node.col_offset,
                    receiver=receiver_repr,
                    method=method_name,
                    reason_repr=rendered,
                    kind="literal_terminal",
                ),
            )
            continue

        if kind == "literal_unknown":
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            violations.append(
                Violation(
                    file_path=rel_path,
                    line=node.lineno,
                    column=node.col_offset,
                    receiver=receiver_repr,
                    method=method_name,
                    reason_repr=rendered,
                    kind="literal_unknown",
                ),
            )
            continue

        # Non-SSoT expression — VIOLATION unless allowlisted.
        if _line_has_allowlist(source_lines, node.lineno):
            continue
        violations.append(
            Violation(
                file_path=rel_path,
                line=node.lineno,
                column=node.col_offset,
                receiver=receiver_repr,
                method=method_name,
                reason_repr=rendered,
                kind="non_ssot_expr",
            ),
        )

    return calls_inspected, violations


def run_check(
    scan_root: Path,
    *,
    receiver_tokens: frozenset[str] = _DEFAULT_QUARANTINE_RECEIVER_TOKENS,
    method_names: frozenset[str] = _DEFAULT_QUARANTINE_METHODS,
    resolver_names: frozenset[str] = _DEFAULT_RESOLVER_NAMES,
    entry_field_names: frozenset[str] = _DEFAULT_ENTRY_FIELD_NAMES,
    repo_root: Path = _REPO_ROOT,
) -> GateReport:
    """Run the scanner over ``scan_root`` recursively. Returns aggregate report."""
    report = GateReport()
    if not scan_root.is_dir():
        report.skipped_files.append(str(scan_root))
        return report
    for py_file in scan_root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        report.files_scanned += 1
        calls, vios = _scan_file(
            py_file,
            receiver_tokens=receiver_tokens,
            method_names=method_names,
            resolver_names=resolver_names,
            entry_field_names=entry_field_names,
            repo_root=repo_root,
        )
        report.calls_inspected += calls
        report.violations.extend(vios)
    return report


def _print_report(report: GateReport, *, strict: bool) -> int:
    """Print human-readable report. Returns exit code."""
    print(
        f"Quality Gate 14 — quarantine reason discipline: "
        f"{report.files_scanned} files scanned, "
        f"{report.calls_inspected} quarantine.add(reason=...) inspected, "
        f"{len(report.violations)} violation(s)."
    )
    if report.violations:
        print()
        print(f"Violations ({'STRICT — fails gate' if strict else 'LENIENT — report-only'}):")
        for v in report.violations:
            print(
                f"  {v.file_path}:{v.line}:{v.column}  "
                f"{v.receiver}.{v.method}(reason={v.reason_repr})  [kind={v.kind}]"
            )
        print()
        print("Compliant shapes:")
        print("  (a) `reason=QuarantineReason.<MEMBER>` (StrEnum) or `.value`")
        print(
            "  (b) `reason=resolve_reason_from_verdict(...)` / `_diagnosis(...)` "
            "(optionally `.value`)"
        )
        print("  (c) `reason=entry.resolved_reason` / `.derived_reason` / `.reason`")
        print(
            '  (d) Lifecycle-tag literal `reason="watchdog_recheck"` etc. with '
            "`# h3-allowlist: lifecycle-tag` inline comment"
        )
        if strict:
            return 1
    else:
        if report.passed:
            print("discipline: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Quality Gate 14 — quarantine reason discipline (Mission H3). "
            "Anti-pattern #46 build-time enforcer."
        ),
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=_DEFAULT_SCAN_ROOT,
        help="Root directory to scan (default: src/sovyx)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("SOVYX_H3_GATE_STRICT") == "1",
        help="Exit 1 on any violation (default: LENIENT report-only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report instead of human text",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Alias for default LENIENT mode (used by verify_gates.sh)",
    )
    parser.add_argument(
        "--quarantine-receiver-tokens",
        nargs="*",
        default=None,
        help="Receiver-name token substrings to recognise (default: quarantine _quarantine)",
    )
    parser.add_argument(
        "--quarantine-methods",
        nargs="*",
        default=None,
        help="Method names to scan on quarantine receivers (default: add)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root used for relative path normalisation",
    )
    args = parser.parse_args(argv)

    receiver_tokens = (
        frozenset(args.quarantine_receiver_tokens)
        if args.quarantine_receiver_tokens
        else _DEFAULT_QUARANTINE_RECEIVER_TOKENS
    )
    method_names = (
        frozenset(args.quarantine_methods)
        if args.quarantine_methods
        else _DEFAULT_QUARANTINE_METHODS
    )

    report = run_check(
        args.scan_root,
        receiver_tokens=receiver_tokens,
        method_names=method_names,
        repo_root=args.repo_root,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if (not args.strict or report.passed) else 1

    return _print_report(report, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
