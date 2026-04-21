"""CI gate — every ``raise`` inside an ``except`` block must spell its cause.

Python tracks the implicit chain via ``__context__`` whenever you raise a
new exception while another is being handled, but the resulting tracebacks
are noisy: the message says ``During handling of the above exception,
another exception occurred`` and the original traceback is buried. The
explicit ``raise NewError(...) from <cause>`` form (PEP 3134) replaces
``__context__`` with ``__cause__``, which renders as
``The above exception was the direct cause of the following exception``
— a much clearer story for the operator reading a crash log.

The trade-off is intentional: every ``except Y as e: raise X(...)`` block
must spell out either ``from e`` (chain) or ``from None`` (suppress, when
the new exception has no causal link to the caught one). Forgetting
``from`` is almost always a bug — the operator gets the implicit chain
but loses the chance for the dictionary-style failure attribution that
``from`` enables.

Allowed forms inside ``except Y as e:``:

  * ``raise``                              — bare re-raise of the caught
                                              exception, no new exception.
  * ``raise e``                            — explicit re-raise of the
                                              captured variable.
  * ``raise X(...) from e``                — chain to the cause.
  * ``raise X(...) from <other_exc_var>``  — chain to another exception
                                              already in scope.
  * ``raise X(...) from None``             — suppress the implicit chain
                                              (use sparingly and document
                                              with a comment).
  * ``raise X(...)  # noqa: cause-suppressed``
                                            — explicit lint escape hatch
                                              for the rare case where
                                              ``from`` is genuinely
                                              inappropriate (e.g. the
                                              ``except`` block exists only
                                              to translate an OS error
                                              and the cause is unrelated).

Wired into ``.github/workflows/ci.yml`` as the ``exception-chain-gate``
job after ``schema-gate``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Token operators understand. The check is line-based against the
# verbatim source — anything else (comment alignment, multi-line raises)
# is not considered "marked" because it makes the noqa less obviously
# tied to the raise.
_NOQA_TOKEN = "# noqa: cause-suppressed"


class _ChainVisitor(ast.NodeVisitor):
    """AST visitor that collects every uncaused raise inside an except.

    Tracks the lexical stack of ``except`` handlers so the rule applies
    only to the body of an ``except`` (not the bare ``try`` body, not
    the ``else`` / ``finally`` clauses — those don't have an active
    exception in the frame).
    """

    def __init__(self, source_lines: list[str]) -> None:
        self._source_lines = source_lines
        self._handler_stack: list[str | None] = []
        # ``raise`` without an exception inside ``finally`` is also
        # disallowed (it would re-raise nothing); we don't enforce that
        # here because Python itself raises a SyntaxError for the
        # invalid forms — only ``except`` is interesting.
        self.violations: list[tuple[int, str]] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Record the captured-name (``except Y as e: -> "e"``) and recurse."""
        self._handler_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._handler_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802 — ast API.
        """Check the raise statement against the cause-chain rules."""
        # Outside of any ``except`` block — Python's normal rules apply.
        if not self._handler_stack:
            self.generic_visit(node)
            return

        # Bare ``raise`` (no exception expression): re-raise the active
        # exception. Always allowed.
        if node.exc is None:
            self.generic_visit(node)
            return

        # Re-raise of the captured exception variable: ``raise e`` where
        # the active handler is ``except Y as e``. Always allowed.
        captured = self._handler_stack[-1]
        if captured is not None and isinstance(node.exc, ast.Name) and node.exc.id == captured:
            self.generic_visit(node)
            return

        # Has an explicit cause (``from e``, ``from None``, ``from
        # other``)? Always allowed.
        if node.cause is not None:
            self.generic_visit(node)
            return

        # Honour the cause-suppressed escape hatch (token defined in
        # ``_NOQA_TOKEN``) when present on the raise line itself
        # (Python source is 1-indexed; list is 0-indexed).
        line_no = node.lineno
        line_idx = line_no - 1
        if 0 <= line_idx < len(self._source_lines) and _NOQA_TOKEN in self._source_lines[line_idx]:
            self.generic_visit(node)
            return

        # Otherwise: this is a violation.
        snippet = ast.unparse(node).strip()
        self.violations.append((line_no, snippet))
        self.generic_visit(node)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return every cause-chain violation in ``path`` (line, source)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # Surface as a violation rather than silently skipping —
        # otherwise a broken file would let the gate pass.
        return [(exc.lineno or 0, f"SyntaxError: {exc.msg}")]
    visitor = _ChainVisitor(source.splitlines())
    visitor.visit(tree)
    return visitor.violations


def _iter_source_files(root: Path) -> list[Path]:
    """Yield every ``.py`` under ``root`` excluding caches."""
    return [
        p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts and not p.name.startswith(".")
    ]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean, 1 on any violation."""
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
    for file in _iter_source_files(args.root):
        total_files += 1
        for line, snippet in _scan_file(file):
            total_violations += 1
            print(
                f"{file}:{line}: raise inside except missing 'from' clause: {snippet}",
                file=sys.stderr,
            )

    if total_violations:
        # ASCII-only sentinels so the gate prints cleanly on Windows
        # consoles (cp1252) as well as Linux/macOS CI runners.
        print(
            f"\nFAIL: {total_violations} cause-chain violation(s) across {total_files} files.",
            file=sys.stderr,
        )
        print(
            "  Fix: append ' from e' (chain) or ' from None' (suppress, with",
            file=sys.stderr,
        )
        print(
            "  comment) to each raise. Hard-to-rewrite cases can use the",
            file=sys.stderr,
        )
        print(
            f"  '{_NOQA_TOKEN}' inline escape on the raise line.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {total_files} files clean - every raise inside except has a cause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
