#!/usr/bin/env python3
"""Quality Gate 15 — resource hygiene discipline (Mission H4 §T1.4).

Static AST scanner that walks every ``*.py`` under ``src/sovyx/`` and
verifies four invariants per Mission H4 §0 item #3 + §4.7 ADR-D7:

1. **Producer parity.** Every ``logger.info("self.health.snapshot",
   **kwargs)`` call site's literal keys in ``kwargs`` MUST appear in
   :data:`sovyx.observability._resource_registry._HEALTH_SNAPSHOT_FIELDS`.
   An emitted key absent from the SSoT is a violation.

2. **Consumer parity.** Every ``event_dict.get(<literal>)`` consumer
   whose literal value looks like a snapshot field (matches
   :data:`_SNAPSHOT_FIELD_HEURISTIC_RE`) MUST appear in the SSoT — OR
   match a registered ``legacy_alias``.

3. **Construction-site pairing.** Every ``ort.InferenceSession(...)``
   construction inside ``src/sovyx/{voice,brain}/`` MUST be paired with
   a ``register_onnx_session(label=...)`` call in the same function
   scope (or be allowlisted via ``# h4-allowlist: out-of-process`` /
   ``# h4-allowlist: external-process-helper``). Every
   ``LRULockDict(...)`` construction MUST be paired with a
   ``register_lock_dict(owner_id=...)`` call in the same function
   scope (or be allowlisted via ``# h4-allowlist: <rationale>``).

4. **Future cohort migration target.** Every ``asyncio.to_thread(...)``
   call site is reported as a ``future_migration`` informational
   record (not a violation in LENIENT mode; allowlisted at STRICT via
   ``# h4-allowlist: lifecycle-bootstrap`` etc.). Phase 1.B migrates a
   controlled cohort; Phase 3 STRICT (v0.54.0) requires every site to
   either use :func:`dispatch_to_thread` or carry an allowlist
   annotation.

Exits non-zero on any violation in STRICT mode (``--strict`` flag or
``SOVYX_H4_GATE_STRICT=1``); LENIENT mode (default) prints the report
but exits 0 — used by ``scripts/verify_gates.sh`` during
v0.49.14..v0.53.x staged adoption. ``.github/workflows/publish.yml``
runs the checker post-build in REPORT-only (LENIENT) mode; Phase 3
v0.54.0 swaps both sites to ``--strict``.

Mirrors the H3 Gate 14 / H2 Gate 13 / C6 Gate 12 / C5 Gate 11 / C4
Gate 10 shape exactly — consistent operator UX across the cohort.

Anti-pattern compliance:

* #20 — checker lives under ``scripts/dev/``; consumers patch via
  ``patch.object(module, "_scan_file")`` against this module path.
* #47 — the canonical instance the gate enforces (resource-cohort
  instrumentation SSoT + producer/consumer name parity).

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T1.4. Counterfactual F1 in §3 is verified by the integration test
``tests/integration/scripts/test_check_resource_hygiene_discipline_falsifiability.py``.
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


# The canonical field-name SSoT is imported lazily at run time from
# the registry module so the scanner cannot drift from the production
# truth. A copy here would re-introduce the exact field-name drift
# class the scanner exists to prevent.


def _load_ssot_field_keys() -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(canonical_keys, legacy_aliases)`` from the live SSoT.

    Imports the registry module to read
    ``_HEALTH_SNAPSHOT_FIELDS``. The scanner is the only out-of-tree
    consumer of this module's private attribute; the scanner itself
    is in-repo so the coupling is explicit.
    """
    src_root = _REPO_ROOT / "src"
    sys.path.insert(0, str(src_root))
    try:
        from sovyx.observability._resource_registry import (  # noqa: PLC0415 — runtime import is intentional.
            _HEALTH_SNAPSHOT_FIELDS,
        )

        canonical = frozenset(_HEALTH_SNAPSHOT_FIELDS.keys())
        legacy = frozenset(
            spec.legacy_alias
            for spec in _HEALTH_SNAPSHOT_FIELDS.values()
            if spec.legacy_alias is not None
        )
        return canonical, legacy
    finally:
        if str(src_root) in sys.path:
            sys.path.remove(str(src_root))


_SNAPSHOT_FIELD_HEURISTIC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:to_thread|lock_dict|onnx|gc|tracemalloc|exception_cohort|process|asyncio|system)\.",
)

# Inline allowlist token. Either on the same line as the violating call
# OR the immediately-preceding line. Matches the H3/H2 conventions.
_ALLOWLIST_RE: Final[re.Pattern[str]] = re.compile(r"#\s*h4-allowlist:\s*\S")

# The structured event name that producers emit.
_SNAPSHOT_EVENT_NAME: Final[str] = "self.health.snapshot"

# Files explicitly allowlisted by path. The SSoT module itself, the
# scanner, the thread-dispatch wrapper module legitimately reference
# the snapshot field literals; tests under tests/ are excluded
# elsewhere by directory scope.
_FILE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "src/sovyx/observability/_resource_registry.py",
        "src/sovyx/observability/_thread_dispatch.py",
        "src/sovyx/observability/resources.py",
    },
)

# Construction-site detection (anti-pattern #14 + #15 enforcement).
_ONNX_PAIRING_PATHS: Final[tuple[str, ...]] = ("src/sovyx/voice/", "src/sovyx/brain/")
_REGISTRY_REGISTER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "register_onnx_session",
        "register_lock_dict",
        "record_to_thread_dispatch",
        "record_exception_cohort",
    },
)


@dataclass(frozen=True, slots=True)
class Violation:
    file_path: str
    line: int
    column: int
    # One of: producer_unknown_field, consumer_unknown_field, onnx_unpaired,
    # lockdict_unpaired, future_migration.
    kind: str
    repr_: str


@dataclass
class GateReport:
    files_scanned: int = 0
    snapshot_emit_calls_inspected: int = 0
    get_calls_inspected: int = 0
    onnx_constructions_inspected: int = 0
    lockdict_constructions_inspected: int = 0
    to_thread_calls_inspected: int = 0
    violations: list[Violation] = field(default_factory=list)
    informational: list[Violation] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "snapshot_emit_calls_inspected": self.snapshot_emit_calls_inspected,
            "get_calls_inspected": self.get_calls_inspected,
            "onnx_constructions_inspected": self.onnx_constructions_inspected,
            "lockdict_constructions_inspected": self.lockdict_constructions_inspected,
            "to_thread_calls_inspected": self.to_thread_calls_inspected,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "informational_count": len(self.informational),
            "violations": [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "column": v.column,
                    "kind": v.kind,
                    "repr": v.repr_,
                }
                for v in self.violations
            ],
            "informational": [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "column": v.column,
                    "kind": v.kind,
                    "repr": v.repr_,
                }
                for v in self.informational
            ],
            "skipped_files": self.skipped_files,
        }


def _line_has_allowlist(source_lines: list[str], line_no: int) -> bool:
    if line_no <= 0 or line_no > len(source_lines):
        return False
    if _ALLOWLIST_RE.search(source_lines[line_no - 1]):
        return True
    return bool(line_no >= 2 and _ALLOWLIST_RE.search(source_lines[line_no - 2]))


def _is_snapshot_emit_call(call: ast.Call) -> bool:
    """Return True iff *call* is ``logger.info("self.health.snapshot", ...)``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in ("info", "warning", "debug"):
        return False
    if not call.args:
        return False
    first_arg = call.args[0]
    return (
        isinstance(first_arg, ast.Constant)
        and isinstance(first_arg.value, str)
        and first_arg.value == _SNAPSHOT_EVENT_NAME
    )


def _is_event_dict_get(call: ast.Call) -> bool:
    """Return True iff *call* is ``event_dict.get(<literal>)`` or sibling form."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "get":
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in ("event_dict", "snapshot", "payload", "fields")
    return False


def _is_onnx_session_construction(call: ast.Call) -> bool:
    """Return True iff *call* is ``ort.InferenceSession(...)`` or alias."""
    func = call.func
    # ort.InferenceSession(...)
    if isinstance(func, ast.Attribute) and func.attr == "InferenceSession":
        return True
    # InferenceSession(...)  (rare)
    return isinstance(func, ast.Name) and func.id == "InferenceSession"


def _is_lockdict_construction(call: ast.Call) -> bool:
    """Return True iff *call* is ``LRULockDict(...)`` or alias."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in ("LRULockDict", "_LRULockDict"):
        return True
    return isinstance(func, ast.Attribute) and func.attr in (
        "LRULockDict",
        "_LRULockDict",
    )


def _is_asyncio_to_thread(call: ast.Call) -> bool:
    """Return True iff *call* is ``asyncio.to_thread(...)``."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "to_thread"):
        return False
    receiver = func.value
    return isinstance(receiver, ast.Name) and receiver.id == "asyncio"


def _scope_contains_registry_call(
    scope: ast.AST,
    register_name: str,
) -> bool:
    """Return True iff *scope*'s body contains a call to *register_name*.

    *scope* may be a function/async-function (typical case — paired
    construction inside a constructor or setup helper) OR a whole
    :class:`ast.Module` (when the construction is at module-level, e.g.
    a route module's ``_ENABLE_LOCKS: LRULockDict[str] = LRULockDict(...)``).

    For the module-level case the walk is restricted to top-level
    statements via :func:`ast.iter_child_nodes` to avoid spurious matches
    inside nested function definitions (a register call inside a
    different function in the same file should NOT pair a sibling
    module-level construction).
    """
    if isinstance(scope, ast.Module):
        # Module-level: walk only the immediate top-level statements +
        # expression bodies. Don't descend into nested functions.
        for stmt in scope.body:
            for node in ast.walk(stmt):
                # Stop descending into function defs.
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Name) and f.id == register_name:
                    return True
                if isinstance(f, ast.Attribute) and f.attr == register_name:
                    return True
            # If the statement itself is a function def, skip its body.
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
        return False
    # Function scope — walk the entire body (children + descendants).
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == register_name:
            return True
        if isinstance(f, ast.Attribute) and f.attr == register_name:
            return True
    return False


# Backward-compat alias for existing callers + tests.
_function_contains_registry_call = _scope_contains_registry_call


def _enclosing_scope(
    tree: ast.Module, target: ast.Call
) -> ast.Module | ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the smallest enclosing scope for *target*.

    Returns the innermost function/async-function whose line range
    encloses *target* — or :class:`ast.Module` *tree* itself when the
    target is at module-level (no enclosing function).
    """
    candidate: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    target_lineno = target.lineno
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= target_lineno <= end and (
            candidate is None or node.lineno > candidate.lineno
        ):
            candidate = node
    return candidate if candidate is not None else tree


# Backward-compat alias.
_enclosing_function = _enclosing_scope


def _scan_file(
    file_path: Path,
    *,
    canonical_keys: frozenset[str],
    legacy_aliases: frozenset[str],
    repo_root: Path,
) -> tuple[int, int, int, int, int, list[Violation], list[Violation]]:
    """Scan one file. Returns counters + (violations, informational)."""
    try:
        rel_path = file_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()

    if rel_path in _FILE_ALLOWLIST:
        return 0, 0, 0, 0, 0, [], []

    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError:
        return 0, 0, 0, 0, 0, [], []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return 0, 0, 0, 0, 0, [], []

    source_lines = source.splitlines()
    violations: list[Violation] = []
    informational: list[Violation] = []
    snap_emit_count = 0
    get_count = 0
    onnx_count = 0
    lockdict_count = 0
    to_thread_count = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # 1. Producer parity — logger.info("self.health.snapshot", **payload)
        if _is_snapshot_emit_call(node):
            snap_emit_count += 1
            # Walk **kwargs literal keys (Constant string keys passed
            # via ``**{"k": v, ...}`` are inside a ``Dict`` argument).
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Dict):
                    for key_node in kw.value.keys:
                        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                            key = key_node.value
                            if key.startswith("self.health."):
                                # Snapshot metadata keys are emitter-private
                                # (self.health.snapshot_final, .uptime_s,
                                # .queues, etc.) and excluded.
                                continue
                            if key in canonical_keys or key in legacy_aliases:
                                continue
                            if _line_has_allowlist(source_lines, key_node.lineno):
                                continue
                            violations.append(
                                Violation(
                                    file_path=rel_path,
                                    line=key_node.lineno,
                                    column=key_node.col_offset,
                                    kind="producer_unknown_field",
                                    repr_=repr(key),
                                ),
                            )
                # Direct keyword-form emission (e.g. ``logger.info(
                # "self.health.snapshot", to_thread.pool_size=X)``)
                # is not valid Python — keyword args can't carry dots.
                # The only emission shape is ``**dict``.

        # 2. Consumer parity — event_dict.get("process.rss_bytes")
        if _is_event_dict_get(node) and node.args and isinstance(node.args[0], ast.Constant):
            key = node.args[0].value
            if isinstance(key, str) and _SNAPSHOT_FIELD_HEURISTIC_RE.match(key):
                get_count += 1
                if (
                    key in canonical_keys
                    or key in legacy_aliases
                    or _line_has_allowlist(source_lines, node.lineno)
                ):
                    pass
                else:
                    violations.append(
                        Violation(
                            file_path=rel_path,
                            line=node.lineno,
                            column=node.col_offset,
                            kind="consumer_unknown_field",
                            repr_=repr(key),
                        ),
                    )

        # 3a. ONNX construction-site pairing.
        if _is_onnx_session_construction(node) and any(
            rel_path.startswith(p) for p in _ONNX_PAIRING_PATHS
        ):
            onnx_count += 1
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            scope = _enclosing_scope(tree, node)
            if not _scope_contains_registry_call(scope, "register_onnx_session"):
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        column=node.col_offset,
                        kind="onnx_unpaired",
                        repr_="ort.InferenceSession(...) without register_onnx_session pairing",
                    ),
                )

        # 3b. LRULockDict construction-site pairing.
        if _is_lockdict_construction(node):
            # Exclude the LRULockDict module itself (its own class
            # definition references the type name internally).
            if rel_path.endswith("engine/_lock_dict.py"):
                continue
            lockdict_count += 1
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            scope = _enclosing_scope(tree, node)
            if not _scope_contains_registry_call(scope, "register_lock_dict"):
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        column=node.col_offset,
                        kind="lockdict_unpaired",
                        repr_="LRULockDict(...) without register_lock_dict pairing",
                    ),
                )

        # 4. asyncio.to_thread — informational only in LENIENT.
        if _is_asyncio_to_thread(node):
            to_thread_count += 1
            if _line_has_allowlist(source_lines, node.lineno):
                continue
            informational.append(
                Violation(
                    file_path=rel_path,
                    line=node.lineno,
                    column=node.col_offset,
                    kind="future_migration",
                    repr_="asyncio.to_thread(...) — migrate to dispatch_to_thread(label=...)",
                ),
            )

    return (
        snap_emit_count,
        get_count,
        onnx_count,
        lockdict_count,
        to_thread_count,
        violations,
        informational,
    )


def run_check(
    scan_root: Path,
    *,
    repo_root: Path = _REPO_ROOT,
) -> GateReport:
    report = GateReport()
    if not scan_root.is_dir():
        report.skipped_files.append(str(scan_root))
        return report
    canonical_keys, legacy_aliases = _load_ssot_field_keys()
    for py_file in scan_root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        report.files_scanned += 1
        snap, get_, onnx, ld, tt, vios, info = _scan_file(
            py_file,
            canonical_keys=canonical_keys,
            legacy_aliases=legacy_aliases,
            repo_root=repo_root,
        )
        report.snapshot_emit_calls_inspected += snap
        report.get_calls_inspected += get_
        report.onnx_constructions_inspected += onnx
        report.lockdict_constructions_inspected += ld
        report.to_thread_calls_inspected += tt
        report.violations.extend(vios)
        report.informational.extend(info)
    return report


def _print_report(report: GateReport, *, strict: bool) -> int:
    print(
        f"Quality Gate 15 — resource hygiene discipline: "
        f"{report.files_scanned} files scanned, "
        f"{report.snapshot_emit_calls_inspected} snapshot emits inspected, "
        f"{report.get_calls_inspected} event_dict.get(...) inspected, "
        f"{report.onnx_constructions_inspected} ONNX constructions, "
        f"{report.lockdict_constructions_inspected} LRULockDict constructions, "
        f"{report.to_thread_calls_inspected} asyncio.to_thread() sites, "
        f"{len(report.violations)} violation(s), "
        f"{len(report.informational)} informational."
    )
    if report.violations:
        print()
        print(f"Violations ({'STRICT — fails gate' if strict else 'LENIENT — report-only'}):")
        for v in report.violations:
            print(
                f"  {v.file_path}:{v.line}:{v.column}  [kind={v.kind}]  {v.repr_}",
            )
        print()
        print("Compliant shapes:")
        print(
            "  (a) Snapshot emitters use only keys in "
            "_HEALTH_SNAPSHOT_FIELDS (sovyx/observability/_resource_registry.py)."
        )
        print(
            "  (b) Snapshot consumers (event_dict.get(...)) reference SSoT keys "
            "or registered legacy_alias."
        )
        print(
            "  (c) Every ort.InferenceSession(...) under src/sovyx/{voice,brain}/ "
            "is paired with register_onnx_session(...) in the same function."
        )
        print(
            "  (d) Every LRULockDict(...) is paired with register_lock_dict(...) "
            "in the same function."
        )
        print("  (e) Inline `# h4-allowlist: <rationale>` annotation.")
        if strict:
            return 1
    else:
        print("discipline: PASS")
    if report.informational:
        print()
        print(
            f"Informational ({len(report.informational)} asyncio.to_thread sites "
            "— Phase 3 STRICT requires migration to dispatch_to_thread):"
        )
        for v in report.informational[:20]:
            print(f"  {v.file_path}:{v.line}  {v.repr_}")
        if len(report.informational) > 20:
            print(f"  ... and {len(report.informational) - 20} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Quality Gate 15 — resource hygiene discipline (Mission H4). "
            "Anti-pattern #47 build-time enforcer."
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
        default=os.environ.get("SOVYX_H4_GATE_STRICT") == "1",
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
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help=(
            "Repository root used to compute relative file paths in the "
            "report (default: scanner's repo root). Falsifiability tests "
            "use a synthetic tmp_path root."
        ),
    )
    args = parser.parse_args(argv)

    report = run_check(args.scan_root, repo_root=args.repo_root)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        if args.strict and report.violations:
            return 1
        return 0

    return _print_report(report, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
