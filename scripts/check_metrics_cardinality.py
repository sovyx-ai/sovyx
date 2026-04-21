"""CI gate — refuse high-cardinality dimensions as metric labels.

Stand-alone enforcement of §22.7 of IMPL-OBSERVABILITY-001
("metric cardinality budget"). The runtime
:class:`sovyx.observability.metrics.CardinalityBudget` is the *belt*:
once a label tuple blows the global ceiling it gets folded into an
overflow series and warns once. This gate is the *braces*: it refuses
labels at PR time that we already know are unbounded by their nature
(per-user, per-request, per-message), so the budget never has to fire
against them in production.

The set of banned dimensions is the consensus list of "things that
look like an ID" — values that are unique-per-event by construction
and would push a single metric's series count into the millions over
a long-running daemon:

  * Caller-scoped IDs: ``user_id``, ``session_id``, ``request_id``,
    ``correlation_id``
  * Trace IDs: ``trace_id``, ``span_id``, ``event_id``, ``saga_id``,
    ``cause_id`` (these belong on logs/spans, never on metrics)
  * PII-shaped: ``email``, ``phone``, ``ip``, ``ip_address``
  * High-granularity timestamps: ``timestamp``, ``time``, ``date``,
    ``datetime``
  * Free-form strings: ``message``, ``query``, ``url``, ``path``
    (use a templated low-cardinality variant like ``route_template``)

The gate scans every literal ``attributes={...}`` dict passed to
``.add(...)`` / ``.record(...)`` anywhere under ``src/sovyx/`` and
flags any banned key. Computed dicts (``attributes=build_attrs(...)``)
are skipped — by definition we can't tell their cardinality at compile
time, and the runtime budget is the right enforcement point for those.

Wired into ``.github/workflows/ci.yml`` as ``metrics-cardinality-gate``
after ``otel-semconv-gate``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ── Banned dimensions ─────────────────────────────────────────────────
# Each entry is a *literal* attribute key that is unique-per-event by
# construction. The gate flags any literal dict using these names so
# the engineer either drops the dimension or replaces it with a
# coarse-grained substitute (``user_tier`` instead of ``user_id``,
# ``route_template`` instead of ``url``, etc.).
_BANNED_DIMENSIONS: frozenset[str] = frozenset(
    {
        # Caller-scoped IDs
        "user_id",
        "session_id",
        "request_id",
        "correlation_id",
        # Trace / event IDs (belong on logs, never on metrics)
        "trace_id",
        "span_id",
        "event_id",
        "saga_id",
        "cause_id",
        # PII-shaped values — also caught by PIIRedactor on logs, but
        # metrics labels never go through the redactor chain so they
        # need a dedicated guard.
        "email",
        "phone",
        "ip",
        "ip_address",
        "ipv4",
        # High-granularity timestamps
        "timestamp",
        "time",
        "date",
        "datetime",
        # Free-form strings — use a low-cardinality template
        "message",
        "query",
        "url",
        "path",
    }
)

# Method names whose ``attributes=`` keyword we audit. Both Counter
# (``add``) and Histogram (``record``) reach the OTel instrument
# through :class:`_BudgetedInstrument`, which forwards
# ``attributes={...}`` straight through to the OTel call.
_METRIC_METHOD_NAMES: frozenset[str] = frozenset({"add", "record"})


def _iter_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` under *root*, sorted, excluding caches."""
    return [
        p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts and not p.name.startswith(".")
    ]


def _extract_attrs_keys(call: ast.Call) -> set[str] | None:
    """Return the literal-string keys of an ``attributes={...}`` kwarg.

    Returns ``None`` when:
      * The call has no ``attributes=`` keyword.
      * The keyword value is not a literal dict (computed dict, name
        reference, function call). Computed dicts are out of scope —
        we can't determine cardinality at compile time, and the runtime
        budget covers them.
    """
    for kw in call.keywords:
        if kw.arg != "attributes":
            continue
        if not isinstance(kw.value, ast.Dict):
            return None
        keys: set[str] = set()
        for key_node in kw.value.keys:
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                keys.add(key_node.value)
        return keys
    return None


class _MetricsLabelVisitor(ast.NodeVisitor):
    """Collect every banned label key passed to ``.add`` / ``.record``."""

    def __init__(self, source_path: Path) -> None:
        self._source_path = source_path
        self.violations: list[tuple[int, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast API.
        """Inspect call sites that look like ``something.add(value, attributes=...)``.

        Heuristic — not every ``.add()`` is a metric call (sets,
        deques, async queues all have ``add`` too). We narrow by
        requiring the ``attributes=`` keyword *and* a literal dict
        value; both are nearly unique to OTel-shaped instruments and
        keep the false-positive rate at zero in the current codebase.
        """
        if isinstance(node.func, ast.Attribute) and node.func.attr in _METRIC_METHOD_NAMES:
            keys = _extract_attrs_keys(node)
            if keys is not None:
                banned = keys & _BANNED_DIMENSIONS
                for label in sorted(banned):
                    self.violations.append((node.lineno, node.func.attr, label))
        self.generic_visit(node)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return every banned-label violation in *path*."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, "syntax-error", exc.msg or "unknown")]
    visitor = _MetricsLabelVisitor(path)
    visitor.visit(tree)
    return visitor.violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean run, 1 on any violation."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("src/sovyx"),
        help="Source tree to scan (default: src/sovyx)",
    )
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2

    total_files = 0
    total_violations = 0
    for file in _iter_python_files(args.root):
        total_files += 1
        for line, method, label in _scan_file(file):
            total_violations += 1
            print(
                f"{file}:{line}: metric .{method}() uses banned high-cardinality label '{label}'",
                file=sys.stderr,
            )

    if total_violations:
        print(
            f"\nFAIL: {total_violations} cardinality violation(s) across {total_files} files.",
            file=sys.stderr,
        )
        print(
            "  Fix: replace the per-event dimension with a low-cardinality "
            "alternative — e.g. 'user_tier' instead of 'user_id', "
            "'route_template' instead of 'url'/'path', or move the "
            "high-cardinality value to a structured log field instead.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {total_files} files clean - no metric uses a banned high-cardinality label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
