#!/usr/bin/env python3
"""Quality Gate 17 — FastAPI response_model presence on dashboard routes
(Mission C §C.0).

Static AST scanner that walks every ``*.py`` under
``src/sovyx/dashboard/routes/`` and reports every
``@router.(get|post|put|patch|delete)(...)`` decorator that does NOT
declare ``response_model=`` AND does NOT carry an inline
``# c-allowlist: response_model_skip reason=<text>`` comment on the
same line or the immediately-preceding line.

Why this matters (proposed anti-pattern #57 closure):

* ``response_model=`` is the SINGLE place where FastAPI binds a
  pydantic model into the route's OpenAPI schema. Without it, the
  generated client + the dashboard's zod twin lose their reference
  shape — typed-consumer drift becomes invisible.
* The Mission C audit found ~70 of 125 route decorators missing
  ``response_model=``; closing them is Phase C.3+/C.4 body work. This
  gate ensures NEW routes shipped during the C cycle don't add to the
  backlog without an explicit waiver.
* OpenAPI truthfulness alignment (one of the Mission C.0+C.2 scope
  items): a route with ``response_model=`` produces a typed schema in
  the OpenAPI dump; without it, the schema is the FastAPI default
  ``{}`` envelope.

LENIENT semantics:

* The gate ALWAYS exits 0 unless ``--strict`` (or
  ``SOVYX_C_GATE_STRICT=1``) is passed.
* Pre-mission baseline: ~70 violations across ~26 route files. The
  Mission C remediation plan §3 Phase C.4 progressively closes them
  in batch by subsystem; the LENIENT phase prevents NEW additions to
  the backlog from sliding in silently.

Allowlist:

* Inline marker ``# c-allowlist: response_model_skip reason=<text>``
  on the decorator line or the preceding line. Use sparingly — the
  only legitimate reasons are streaming responses (``StreamingResponse``,
  ``EventSourceResponse``) and file-download routes (``FileResponse``)
  whose body is not a structured JSON object the typed consumer can
  walk. Each waiver MUST carry the ``reason=<text>`` clause.

Anti-pattern compliance:

* #20 — scanner lives under ``scripts/dev/`` (operational tooling).
* #57 (proposed; closure via this gate's STRICT-flip).

Mission anchor:
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 18 +
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
_DEFAULT_SCAN_ROOT = _REPO_ROOT / "src" / "sovyx" / "dashboard" / "routes"


# HTTP-verb attribute names on a FastAPI ``APIRouter``-like receiver.
_HTTP_METHOD_ATTRS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options"},
)


# Receiver names that look like a FastAPI router. We accept any
# ``<name>.<verb>(...)`` whose receiver Name node is "router" OR ends in
# "_router" — this matches the codebase's single ``router = APIRouter()``
# pattern + future per-domain sub-routers.
_ROUTER_RECEIVER_NAMES: Final[frozenset[str]] = frozenset({"router"})
_ROUTER_NAME_SUFFIX: Final[str] = "_router"


# Inline allowlist marker. Either on the same line as the decorator OR
# the immediately-preceding line.
_ALLOWLIST_RE: Final[re.Pattern[str]] = re.compile(
    r"#\s*c-allowlist:\s*response_model_skip\s+reason=\S",
)


@dataclass(frozen=True, slots=True)
class _MissingDecoration:
    """A route decorator missing ``response_model=`` and not allowlisted."""

    file_path: str
    line: int
    column: int
    receiver: str
    method: str
    route_path: str


@dataclass
class GateReport:
    files_scanned: int = 0
    decorators_inspected: int = 0
    decorators_with_response_model: int = 0
    decorators_allowlisted: int = 0
    violations: list[_MissingDecoration] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "decorators_inspected": self.decorators_inspected,
            "decorators_with_response_model": self.decorators_with_response_model,
            "decorators_allowlisted": self.decorators_allowlisted,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "column": v.column,
                    "receiver": v.receiver,
                    "method": v.method,
                    "route_path": v.route_path,
                }
                for v in self.violations
            ],
            "skipped_files": self.skipped_files,
        }


def _is_router_receiver(name: str) -> bool:
    if name in _ROUTER_RECEIVER_NAMES:
        return True
    return name.endswith(_ROUTER_NAME_SUFFIX)


def _extract_route_path(call: ast.Call) -> str:
    """Return the route path literal from the decorator's first positional
    arg, or ``"<dynamic>"`` if not a string constant."""
    if (
        call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(
            call.args[0].value,
            str,
        )
    ):
        return call.args[0].value
    return "<dynamic>"


def _has_allowlist_marker(source_lines: list[str], decorator_lineno: int) -> bool:
    """True iff an allowlist marker comment is on the decorator line OR
    the immediately-preceding line.

    ``decorator_lineno`` is 1-indexed (ast convention).
    """
    if decorator_lineno < 1 or decorator_lineno > len(source_lines):
        return False
    same_line = source_lines[decorator_lineno - 1]
    if _ALLOWLIST_RE.search(same_line):
        return True
    if decorator_lineno >= 2:
        prev_line = source_lines[decorator_lineno - 2]
        if _ALLOWLIST_RE.search(prev_line):
            return True
    return False


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
    # Try to render the file path relative to the repo root for stable
    # output; fall back to the absolute path when scanning a tmp dir
    # (e.g. under the test suite).
    if path.is_absolute():
        try:
            rel: Path = path.relative_to(_REPO_ROOT)
        except ValueError:
            rel = path
    else:
        rel = path
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            receiver = func.value.id
            method = func.attr
            if not _is_router_receiver(receiver):
                continue
            if method not in _HTTP_METHOD_ATTRS:
                continue
            report.decorators_inspected += 1
            has_rm = any(kw.arg == "response_model" for kw in deco.keywords)
            if has_rm:
                report.decorators_with_response_model += 1
                continue
            if _has_allowlist_marker(source_lines, deco.lineno):
                report.decorators_allowlisted += 1
                continue
            report.violations.append(
                _MissingDecoration(
                    file_path=str(rel).replace("\\", "/"),
                    line=deco.lineno,
                    column=deco.col_offset,
                    receiver=receiver,
                    method=method,
                    route_path=_extract_route_path(deco),
                ),
            )


def scan_dir(scan_root: Path = _DEFAULT_SCAN_ROOT) -> GateReport:
    report = GateReport()
    if not scan_root.exists():
        report.skipped_files.append(f"scan root missing: {scan_root}")
        return report
    for path in sorted(scan_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        _scan_file(path, report)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mission C Gate 18 — FastAPI response_model presence on "
            "dashboard routes. LENIENT by default (reports + exit 0); "
            "STRICT via --strict or SOVYX_C_GATE_STRICT=1."
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
        help="Override the dashboard/routes scan root.",
    )
    return parser


def _render_human(report: GateReport) -> str:
    lines: list[str] = []
    lines.append(
        f"Gate 18 — response_model presence: "
        f"{report.files_scanned} file(s), "
        f"{report.decorators_inspected} decorator(s) inspected, "
        f"{report.decorators_with_response_model} bound, "
        f"{report.decorators_allowlisted} allowlisted.",
    )
    if report.skipped_files:
        lines.append("Skipped files:")
        for entry in report.skipped_files:
            lines.append(f"  - {entry}")
    if report.violations:
        lines.append(
            f"{len(report.violations)} violation(s) — routes without "
            "response_model= and no allowlist marker (anti-pattern #57 "
            "surface):",
        )
        for v in report.violations:
            lines.append(
                f"  {v.file_path}:{v.line} @{v.receiver}.{v.method}({v.route_path!r})",
            )
        lines.append("response_model discipline: VIOLATIONS")
    else:
        lines.append("response_model discipline: PASS")
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
