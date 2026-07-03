#!/usr/bin/env python3
"""Quality Gate 13 — platform-neutral event-name discipline (Mission H2 §T1.4).

Static AST scanner that walks every ``*.py`` under ``src/sovyx/`` and
reports every ``logger.{info,warning,error,debug,critical}(...)`` call
whose event-name literal (first positional arg OR ``event=`` keyword)
embeds a platform-specific token (``apo``, ``wasapi``, ``dsound``,
``pulseaudio``, ``pipewire``, ``coreaudio``, ``wmme``, ``directshow``,
``voice_clarity``, ``module_echo_cancel``, ``voice_isolation``) outside
the four permitted escape hatches:

1. **Platform gate** — the enclosing block is an
   ``if sys.platform == "<plat>":`` / ``if platform.system() == "<Plat>":``
   if-statement at any ancestor level.
2. **Platform suffix** — the literal ends in ``.linux`` / ``.windows``
   / ``.darwin`` (the canonical platform-suffixed dotted-namespace
   pattern from anti-pattern #45 (c)).
3. **Inline allowlist comment** — the line bearing the call OR the
   immediately-preceding line carries ``# h2-allowlist: <rationale>``.
4. **Allowlisted helper module** — the file path is in the explicit
   allowlist (the dual-emission wrapper at
   :mod:`sovyx.voice.pipeline._capture_integrity_emit` legitimately
   emits BOTH legacy and neutral names; its own legacy emissions are
   allowlisted per ADR-D14 — same shape as C4 Gate 10 + C5 Gate 11 +
   C6 Gate 12 conventions).

Exits non-zero on any violation in STRICT mode (``--strict`` flag or
``SOVYX_H2_GATE_STRICT=1``); LENIENT mode (default) prints the report
but exits 0 — used by ``scripts/verify_gates.sh`` during v0.49.x staged
adoption. ``.github/workflows/publish.yml`` runs the checker post-build
in REPORT-only (LENIENT) mode; Phase 3 v0.51.0 swaps both sites to
``--strict``.

Anti-pattern compliance:

* #20 — checker lives under ``scripts/dev/`` (operational tooling), not
  inside production code; consumers patch via ``patch.object(module,
  "_run_checker")`` against this module path.
* #45 — the canonical instance the gate enforces.

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T1.4. Counterfactual F1 in §3 is verified by the integration test
``tests/integration/scripts/test_check_platform_neutral_event_names_falsifiability.py``.
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


# Platform tokens we care about — embedded inside `.` / `_` / start /
# end boundaries within event-name string literals. Deliberately narrow:
# audio-stack OS terminology only. Broadening this set requires a paired
# `# h2-allowlist:` audit pass to avoid silently breaking unrelated
# subsystems (per Mission H2 §13 anti-pattern #45 strict-requirement
# paragraph).
_PLATFORM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[._])"
    r"(apo|wasapi|dsound|pulseaudio|pipewire|coreaudio|wmme|directshow|"
    r"voice_clarity|module_echo_cancel|voice_isolation)"
    r"(?:[._]|$)"
)


# Permitted platform suffix on dotted-namespace events. An event whose
# name ends in one of these is canonically platform-suffixed (e.g.
# ``audio.capture_chain.scan.linux``) and #45(c) explicitly permits the
# pattern.
_PLATFORM_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"\.(linux|windows|darwin)$")


# An event name containing an EXPLICIT platform token anywhere
# (`voice.windows.apo_registry_access_denied`, `voice_apo_linux_pactl_failed`,
# `audio.linux.scan`, etc.) is self-disambiguating — the operator
# reading the event name already knows the platform. #45 permits this
# pattern implicitly via the "platform-suffix" escape hatch (c), and
# the prefix / infix sibling shapes follow the same principle. Without
# this allowlist, valid platform-disambiguated names would false-positive
# on the platform-token regex.
_PLATFORM_DISAMBIGUATING_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[._])(linux|windows|darwin|win32|macos)(?:[._]|$)"
)


# Inline allowlist token. Either on the same line as the violating
# emit OR the immediately-preceding line.
_ALLOWLIST_RE: Final[re.Pattern[str]] = re.compile(r"#\s*h2-allowlist:\s*\S")


# Logger variable names recognised by the scanner. Configurable via
# the ``--logger-names`` flag for tests / future extensions.
_DEFAULT_LOGGER_NAMES: Final[frozenset[str]] = frozenset({"logger", "log", "_logger", "_log"})


# Logger methods we scan. Excluded: ``exception()`` (always paired with
# an exception object; not the primary emit path); ``log()`` (generic;
# rare in this codebase).
_DEFAULT_LEVEL_METHODS: Final[frozenset[str]] = frozenset(
    {"info", "warning", "error", "debug", "critical"}
)


# Platform-gate sentinel expressions. The scanner walks AST upward from
# each violating Call node and checks if any enclosing ``If`` node's
# test expression matches one of these patterns (compared via
# ``ast.unparse``).
_PLATFORM_GATE_SENTINELS_TEMPLATES: Final[tuple[str, ...]] = (
    "sys.platform == {Q}win32{Q}",
    "sys.platform == {Q}linux{Q}",
    "sys.platform == {Q}linux2{Q}",
    "sys.platform == {Q}darwin{Q}",
    "sys.platform.startswith({Q}linux{Q})",
    "sys.platform.startswith({Q}win{Q})",
    "sys.platform.startswith({Q}darwin{Q})",
    "platform.system() == {Q}Windows{Q}",
    "platform.system() == {Q}Linux{Q}",
    "platform.system() == {Q}Darwin{Q}",
)


def _build_gate_sentinel_set() -> frozenset[str]:
    """Expand the templates to both single- and double-quote variants.

    ``ast.unparse`` defaults to single-quote string literals; user code
    typically uses double quotes. Both must match.
    """
    result: set[str] = set()
    for template in _PLATFORM_GATE_SENTINELS_TEMPLATES:
        result.add(template.replace("{Q}", "'"))
        result.add(template.replace("{Q}", '"'))
    return frozenset(result)


_PLATFORM_GATE_SENTINELS: Final[frozenset[str]] = _build_gate_sentinel_set()


# Files explicitly allowlisted — the dual-emission wrapper module
# legitimately emits BOTH legacy and neutral names per ADR-D14. Phase 3
# STRICT removes the wrapper's legacy emission block; this allowlist
# entry is retired at the same time.
_FILE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "src/sovyx/voice/pipeline/_capture_integrity_emit.py",
    }
)


@dataclass(frozen=True, slots=True)
class Violation:
    """A platform-token literal in a logger emit lacking any escape hatch."""

    file_path: str
    line: int
    column: int
    method: str
    literal: str
    token_matched: str


@dataclass
class GateReport:
    """Aggregate scanner report — mirrors the C5 Gate 11 / C6 Gate 12 shape."""

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
                    "method": v.method,
                    "literal": v.literal,
                    "token": v.token_matched,
                }
                for v in self.violations
            ],
            "skipped_files": self.skipped_files,
        }


def _extract_event_literal(call: ast.Call) -> str | None:
    """Return the event-name string literal if the call's first arg is one.

    Supports both positional (``logger.error("event_name", ...)``) and
    keyword (``logger.error(event="event_name", ...)``) shapes.
    """
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    for kw in call.keywords:
        if (
            kw.arg == "event"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _is_logger_call(
    call: ast.Call, logger_names: frozenset[str], level_methods: frozenset[str]
) -> str | None:
    """Return the level method name if this call is on a recognised logger.

    Matches ``logger.info(...)`` / ``log.warning(...)`` etc. via the
    Attribute → Name AST shape. Returns ``None`` for unrelated calls
    (e.g. method calls on other receivers, top-level function calls).
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in level_methods:
        return None
    value = func.value
    if not isinstance(value, ast.Name):
        return None
    if value.id not in logger_names:
        return None
    return func.attr


def _find_enclosing_platform_gate(call: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Walk AST ancestors looking for an ``If`` node whose test matches a sentinel.

    Returns True if any ancestor ``If`` test stringifies to one of the
    canonical platform-gate sentinels. The walk stops at module scope.
    """
    node: ast.AST | None = parents.get(id(call))
    while node is not None:
        if isinstance(node, ast.If):
            try:
                test_str = ast.unparse(node.test)
            except Exception:  # noqa: BLE001 — defensive on weird AST shapes
                test_str = ""
            if test_str in _PLATFORM_GATE_SENTINELS:
                return True
        node = parents.get(id(node))
    return False


def _build_parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """Build a child→parent map for AST upward walks."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _line_has_allowlist(source_lines: list[str], line_no: int) -> bool:
    """Return True if the current OR previous source line carries the allowlist token.

    AST does not track comments; we read the raw source. ``line_no`` is
    1-based per AST convention.
    """
    if line_no <= 0 or line_no > len(source_lines):
        return False
    current = source_lines[line_no - 1]
    if _ALLOWLIST_RE.search(current):
        return True
    if line_no >= 2:
        previous = source_lines[line_no - 2]
        if _ALLOWLIST_RE.search(previous):
            return True
    return False


def _scan_file(
    file_path: Path,
    logger_names: frozenset[str],
    level_methods: frozenset[str],
    repo_root: Path,
) -> tuple[int, list[Violation]]:
    """Scan one Python file. Returns (calls_inspected, violations)."""
    try:
        rel_path = file_path.relative_to(repo_root).as_posix()
    except ValueError:
        # File is outside repo_root (typical in tests using tmp_path that
        # passed a smaller repo_root override). Fall back to absolute.
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
    parents = _build_parent_map(tree)

    calls_inspected = 0
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _is_logger_call(node, logger_names, level_methods)
        if method is None:
            continue
        literal = _extract_event_literal(node)
        if literal is None:
            continue
        calls_inspected += 1
        match = _PLATFORM_TOKEN_RE.search(literal)
        if match is None:
            continue
        # Match found — check escape hatches.
        if _PLATFORM_SUFFIX_RE.search(literal):
            continue
        if _PLATFORM_DISAMBIGUATING_RE.search(literal):
            # Event name contains an explicit platform token anywhere —
            # self-disambiguating per anti-pattern #45 (sibling of escape
            # hatch (c)). Operator reading the event name already knows
            # the platform.
            continue
        if _line_has_allowlist(source_lines, node.lineno):
            continue
        if _find_enclosing_platform_gate(node, parents):
            continue
        violations.append(
            Violation(
                file_path=rel_path,
                line=node.lineno,
                column=node.col_offset,
                method=method,
                literal=literal,
                token_matched=match.group(1),
            )
        )

    return calls_inspected, violations


def run_check(
    scan_root: Path,
    *,
    logger_names: frozenset[str] = _DEFAULT_LOGGER_NAMES,
    level_methods: frozenset[str] = _DEFAULT_LEVEL_METHODS,
    repo_root: Path = _REPO_ROOT,
) -> GateReport:
    """Run the scanner over ``scan_root`` recursively. Returns aggregate report."""
    report = GateReport()
    if not scan_root.is_dir():
        report.skipped_files.append(str(scan_root))
        return report
    for py_file in scan_root.rglob("*.py"):
        # Skip __pycache__ and other generated directories
        if "__pycache__" in py_file.parts:
            continue
        report.files_scanned += 1
        calls, vios = _scan_file(py_file, logger_names, level_methods, repo_root)
        report.calls_inspected += calls
        report.violations.extend(vios)
    return report


def _print_report(report: GateReport, *, strict: bool) -> int:
    """Print human-readable report. Returns exit code."""
    print(
        f"Quality Gate 13 — platform-neutral event names: "
        f"{report.files_scanned} files scanned, "
        f"{report.calls_inspected} logger emits inspected, "
        f"{len(report.violations)} violation(s)."
    )
    if report.violations:
        print()
        print(f"Violations ({'STRICT — fails gate' if strict else 'LENIENT — report-only'}):")
        for v in report.violations:
            print(
                f"  {v.file_path}:{v.line}:{v.column}  {v.method}({v.literal!r})  [token={v.token_matched}]"
            )
        print()
        print("Escape hatches (use one):")
        print('  (a) `if sys.platform == "<plat>": ...` enclosing the emit')
        print("  (b) Platform-suffixed event name (e.g. `audio.capture_chain.scan.linux`)")
        print("  (c) Inline `# h2-allowlist: <rationale>` comment on the same or previous line")
        print("  (d) File-level allowlist in `_FILE_ALLOWLIST` (dual-emission wrappers only)")
        if strict:
            return 1
    else:
        if report.passed:
            print("discipline: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality Gate 13 — platform-neutral event-name discipline (Mission H2)."
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
        default=os.environ.get("SOVYX_H2_GATE_STRICT") == "1",
        help="Exit 1 on any violation (default: LENIENT report-only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report instead of human text",
    )
    parser.add_argument(
        "--logger-names",
        nargs="*",
        default=None,
        help="Logger variable names to recognise (default: logger log _logger _log)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root used for relative path normalisation (default: repo root)",
    )
    args = parser.parse_args(argv)

    logger_names = frozenset(args.logger_names) if args.logger_names else _DEFAULT_LOGGER_NAMES

    report = run_check(args.scan_root, logger_names=logger_names, repo_root=args.repo_root)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if not args.strict or report.passed else 1

    return _print_report(report, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
