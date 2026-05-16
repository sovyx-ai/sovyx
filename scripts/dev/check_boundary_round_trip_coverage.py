#!/usr/bin/env python3
"""Quality Gate 8 — boundary round-trip test coverage checker.

Mission C2 §T4.1 — STRICT flip discipline.

Every ``Model.model_validate(helper_dict)`` call in
``src/sovyx/dashboard/routes/voice.py`` MUST be paired with a
round-trip regression test under ``tests/dashboard/`` that:

1. Imports the model class, AND
2. Calls ``Model.model_validate(...)`` directly, OR
3. Calls ``assert_boundary_accepts(Model, ...)``
   (the canonical helper from ``tests/dashboard/_boundary_helpers.py``).

Failure surfaces the bug class anti-pattern #40 describes: a typed
boundary drifts from the producer dict shape because no test
round-trips the producer's runtime-bound output through the model.
That's the exact gap that allowed C2's ``VoiceStatusResponse.capture
.input_device: str | None`` to ship narrowed for 7 months while
the producer always emitted ``int | str | None``.

This checker is mechanical, AST-based, runs in < 1 s, and adds
zero runtime dependencies. Invoked from ``scripts/verify_gates.sh``
as Gate 8.

Exit codes:
    0 — every model_validate site has a paired test
    1 — at least one site is uncovered

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T4.1.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTES_FILE = _REPO_ROOT / "src" / "sovyx" / "dashboard" / "routes" / "voice.py"
_TEST_DIR = _REPO_ROOT / "tests" / "dashboard"


def find_model_validate_sites(source_file: Path) -> list[tuple[str, int]]:
    """Walk *source_file* AST; return (model_name, line_no) for every
    ``Model.model_validate(...)`` call where Model is a Name node.

    Skips other ``.model_validate(...)`` callers (e.g. on a parenthesised
    expression, a Call result, or an attribute chain) — those are not
    in the C2 bug class.
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "model_validate":
            continue
        if not isinstance(func.value, ast.Name):
            continue
        results.append((func.value.id, node.lineno))
    return results


def _model_has_paired_test(model_name: str, test_dir: Path) -> bool:
    """Return True iff at least one ``.py`` file under *test_dir*
    imports *model_name* AND exercises one of the two canonical
    coverage patterns.
    """
    # Match either ``ModelName.model_validate(`` (direct round-trip)
    # OR ``assert_boundary_accepts(\s*ModelName,`` (shared helper).
    direct_re = re.compile(rf"\b{re.escape(model_name)}\.model_validate\s*\(")
    helper_re = re.compile(
        rf"\bassert_boundary_accepts\s*\(\s*{re.escape(model_name)}\s*,",
    )
    for path in test_dir.rglob("*.py"):
        if path.name.startswith("_") and not path.name.startswith("__"):
            # Skip private helpers (e.g. ``_boundary_helpers.py``); they
            # MAY mention the model in a docstring but are not tests.
            continue
        if path.name == "__init__.py":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if model_name not in content:
            continue
        if direct_re.search(content) or helper_re.search(content):
            return True
    return False


def main() -> int:
    """Entry point. Returns process exit code."""
    if not _ROUTES_FILE.exists():
        sys.stderr.write(f"FATAL: routes file missing: {_ROUTES_FILE}\n")
        return 1
    if not _TEST_DIR.exists():
        sys.stderr.write(f"FATAL: test dir missing: {_TEST_DIR}\n")
        return 1

    sites = find_model_validate_sites(_ROUTES_FILE)
    if not sites:
        # No call sites — vacuous pass. The gate exists to catch drift
        # AGAINST the cohort; an empty cohort is by definition compliant.
        sys.stdout.write(
            "Quality Gate 8 — boundary round-trip coverage: no "
            "model_validate sites in routes/voice.py (vacuous pass)\n",
        )
        return 0

    # Deduplicate model names (a model used at multiple call sites only
    # needs one paired test).
    unique_models = sorted({name for name, _ in sites})
    uncovered: list[str] = []
    for model_name in unique_models:
        if not _model_has_paired_test(model_name, _TEST_DIR):
            uncovered.append(model_name)

    if uncovered:
        sys.stderr.write(
            "Quality Gate 8 — boundary round-trip coverage FAILED\n",
        )
        sys.stderr.write("\n  Uncovered models in routes/voice.py:\n")
        for model in uncovered:
            lines = [str(line) for name, line in sites if name == model]
            sys.stderr.write(
                f"    - {model} at routes/voice.py:{','.join(lines)}\n",
            )
        sys.stderr.write(
            "\n  Add a regression test in tests/dashboard/ that either:\n"
            "    - Calls ``Model.model_validate(...)`` directly, OR\n"
            "    - Uses ``assert_boundary_accepts(Model, helper_factory=...)``\n"
            "      from ``tests/dashboard/_boundary_helpers.py``.\n"
            "\n"
            "  Mission C2 §T4.1 / anti-pattern #40 — the typed boundary\n"
            "  drifts from the producer dict shape when this discipline\n"
            "  lapses (see /api/voice/status pre-v0.44.2 incident).\n",
        )
        return 1

    sys.stdout.write(
        f"Quality Gate 8 — boundary round-trip coverage: "
        f"{len(unique_models)} model(s) across {len(sites)} call site(s), "
        "all paired with tests\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
