#!/usr/bin/env python3
"""Quality Gate 8 — boundary round-trip test coverage checker.

Mission C2 §T4.1 — STRICT flip discipline (voice scope).
Mission C C.6 §1 — LENIENT scope expansion (all-routes scope).

Every ``Model.model_validate(helper_dict)`` call in a dashboard
route MUST be paired with a round-trip regression test under
``tests/dashboard/`` (or test_dir override) that:

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

Scopes:

* ``voice`` (default — STRICT) — scans only
  ``src/sovyx/dashboard/routes/voice.py``. Uncovered model → exit 1.
  This is the original Mission C2 §T4.1 contract; behaviour is
  identical to the pre-Mission-C-C.6-§1 baseline so the gate's
  rollback path is "do nothing" or ``SOVYX_GATES_C2_SCOPE=voice``.
* ``all`` (LENIENT default — STRICT-flippable) — scans every
  ``*.py`` file under ``src/sovyx/dashboard/routes/`` (excluding
  ``__init__.py`` and ``_*`` private helpers). Uncovered models are
  REPORTED but the gate exits 0 unless ``--strict`` (or
  ``SOVYX_C_GATE_STRICT=1``) is passed. This LENIENT-first cadence
  lays the foundation for per-route paired-test landings before any
  STRICT flip; the cluster of routes that gained ``response_model=``
  via Mission C C.4 ships without paired boundary tests yet, and
  the LENIENT mode prevents that backlog from red-ing the harness.

Scope selection precedence:

1. ``--scope <voice|all>`` CLI flag.
2. ``SOVYX_GATES_C2_SCOPE`` env var.
3. Default ``voice``.

This checker is mechanical, AST-based, runs in < 1 s, and adds
zero runtime dependencies. Invoked from ``scripts/verify_gates.sh``
as Gate 8 (default voice scope; current STRICT behaviour preserved).

Exit codes (voice scope):
    0 — every model_validate site has a paired test
    1 — at least one site is uncovered

Exit codes (all scope, LENIENT default):
    0 — always (warnings emitted to stderr; report still available via --json)

Exit codes (all scope + --strict):
    0 — every model_validate site has a paired test
    1 — at least one site is uncovered

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T4.1; ``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md``
§3 Phase C.6 sub-sequence step 1.
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
_DEFAULT_ROUTES_DIR = _REPO_ROOT / "src" / "sovyx" / "dashboard" / "routes"
_DEFAULT_TEST_DIR = _REPO_ROOT / "tests" / "dashboard"

_VALID_SCOPES: Final[frozenset[str]] = frozenset({"voice", "all"})


@dataclass(frozen=True, slots=True)
class _CallSite:
    model_name: str
    file_rel: str
    line: int


@dataclass
class GateReport:
    scope: str = "voice"
    strict: bool = True
    files_scanned: int = 0
    sites: list[_CallSite] = field(default_factory=list)
    unique_models: list[str] = field(default_factory=list)
    uncovered_models: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.uncovered_models

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "strict": self.strict,
            "files_scanned": self.files_scanned,
            "site_count": len(self.sites),
            "unique_model_count": len(self.unique_models),
            "unique_models": list(self.unique_models),
            "uncovered_model_count": len(self.uncovered_models),
            "uncovered_models": [
                {
                    "model": model,
                    "lines": sorted(
                        {
                            f"{site.file_rel}:{site.line}"
                            for site in self.sites
                            if site.model_name == model
                        }
                    ),
                }
                for model in self.uncovered_models
            ],
            "passed": self.passed,
            "skipped_files": self.skipped_files,
        }


def find_model_validate_sites(
    source_file: Path,
    *,
    file_rel: str | None = None,
) -> list[_CallSite]:
    """Walk *source_file* AST; return one ``_CallSite`` per
    ``Model.model_validate(...)`` call where ``Model`` is a Name node.

    Skips other ``.model_validate(...)`` callers (e.g. on a parenthesised
    expression, a Call result, or an attribute chain) — those are not
    in the C2 bug class.
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    rel = file_rel if file_rel is not None else source_file.name
    results: list[_CallSite] = []
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
        results.append(
            _CallSite(model_name=func.value.id, file_rel=rel, line=node.lineno),
        )
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


def _resolve_target_files(routes_root: Path, scope: str) -> list[Path]:
    """Return the list of route .py files to scan for *scope*.

    For ``voice`` scope: ``[routes_root/voice.py]`` if it exists, else [].
    For ``all`` scope: every ``*.py`` under ``routes_root`` excluding
    ``__init__.py`` and files whose basename starts with ``_`` (private
    helpers — not route modules).
    """
    if scope == "voice":
        candidate = routes_root / "voice.py"
        return [candidate] if candidate.exists() else []
    # scope == "all"
    files: list[Path] = []
    for path in sorted(routes_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if path.name.startswith("_"):
            continue
        files.append(path)
    return files


def _scan(
    routes_root: Path,
    test_dir: Path,
    *,
    scope: str,
    strict: bool,
) -> GateReport:
    report = GateReport(scope=scope, strict=strict)
    target_files = _resolve_target_files(routes_root, scope)
    if not target_files:
        return report
    for source_file in target_files:
        try:
            rel = str(source_file.relative_to(_REPO_ROOT)).replace("\\", "/")
        except ValueError:
            rel = source_file.name
        try:
            sites = find_model_validate_sites(source_file, file_rel=rel)
        except (OSError, SyntaxError) as exc:
            report.skipped_files.append(f"{rel}: {exc!r}")
            continue
        report.files_scanned += 1
        report.sites.extend(sites)
    report.unique_models = sorted({s.model_name for s in report.sites})
    report.uncovered_models = [
        model for model in report.unique_models if not _model_has_paired_test(model, test_dir)
    ]
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mission C Gate 8 — boundary round-trip coverage. "
            "Default scope: voice (STRICT). Set --scope all or "
            "SOVYX_GATES_C2_SCOPE=all for LENIENT cross-route scan."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=sorted(_VALID_SCOPES),
        default=None,
        help=("Scan scope (precedence: CLI flag > env > default 'voice')."),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Force STRICT mode (exit 1 on uncovered). Default behaviour "
            "in voice scope; ALSO settable via SOVYX_C_GATE_STRICT=1."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report on stdout.",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=None,
        help="Override the routes scan root (for testing).",
    )
    parser.add_argument(
        "--test-dir",
        type=Path,
        default=None,
        help="Override the tests dir (for testing).",
    )
    return parser


def _render_voice_human(report: GateReport) -> str:
    """Render the voice-scope output. Preserves the verify_gates.sh
    summary-line grep contract:
        "boundary round-trip coverage:.*all paired"
        "vacuous pass"
    """
    lines: list[str] = []
    if not report.sites:
        lines.append(
            "Quality Gate 8 — boundary round-trip coverage: no "
            "model_validate sites in routes/voice.py (vacuous pass)",
        )
        return "\n".join(lines)
    if report.uncovered_models:
        lines.append("Quality Gate 8 — boundary round-trip coverage FAILED")
        lines.append("\n  Uncovered models in routes/voice.py:")
        for model in report.uncovered_models:
            line_nums = sorted({str(s.line) for s in report.sites if s.model_name == model})
            lines.append(f"    - {model} at routes/voice.py:{','.join(line_nums)}")
        lines.append(
            "\n  Add a regression test in tests/dashboard/ that either:\n"
            "    - Calls ``Model.model_validate(...)`` directly, OR\n"
            "    - Uses ``assert_boundary_accepts(Model, helper_factory=...)``\n"
            "      from ``tests/dashboard/_boundary_helpers.py``.\n"
            "\n"
            "  Mission C2 §T4.1 / anti-pattern #40 — the typed boundary\n"
            "  drifts from the producer dict shape when this discipline\n"
            "  lapses (see /api/voice/status pre-v0.44.2 incident)."
        )
        return "\n".join(lines)
    lines.append(
        f"Quality Gate 8 — boundary round-trip coverage: "
        f"{len(report.unique_models)} model(s) across {len(report.sites)} "
        "call site(s), all paired with tests"
    )
    return "\n".join(lines)


def _render_all_human(report: GateReport) -> str:
    """Render the all-scope output. Distinct summary line from voice
    scope so verify_gates.sh's current grep ('all paired') does NOT
    false-positive on LENIENT-warn output. The string 'all-routes'
    appears in the summary to make scope visible in logs.
    """
    lines: list[str] = []
    header = (
        f"Quality Gate 8 — boundary round-trip coverage (all-routes scope, "
        f"{'STRICT' if report.strict else 'LENIENT'}): "
        f"{report.files_scanned} file(s), "
        f"{len(report.unique_models)} unique model(s) across "
        f"{len(report.sites)} call site(s)."
    )
    lines.append(header)
    if report.skipped_files:
        lines.append("Skipped files:")
        for entry in report.skipped_files:
            lines.append(f"  - {entry}")
    if report.uncovered_models:
        verdict = "VIOLATIONS" if report.strict else "warn"
        lines.append(
            f"{len(report.uncovered_models)} uncovered model(s) — "
            "no paired round-trip test in tests/dashboard/ "
            f"(LENIENT default; --strict surfaces these as failures): {verdict}"
        )
        for model in report.uncovered_models:
            line_refs = sorted(
                {f"{s.file_rel}:{s.line}" for s in report.sites if s.model_name == model}
            )
            lines.append(f"  - {model} at {', '.join(line_refs)}")
        lines.append(
            "  Add a paired regression test (Model.model_validate(...) "
            "or assert_boundary_accepts(Model, ...))."
        )
    else:
        lines.append("all-routes coverage: every model has a paired test")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    scope = args.scope or os.environ.get("SOVYX_GATES_C2_SCOPE") or "voice"
    if scope not in _VALID_SCOPES:
        sys.stderr.write(
            f"FATAL: unknown scope {scope!r}; expected one of {sorted(_VALID_SCOPES)}\n"
        )
        return 2

    # Voice scope is STRICT by default (preserves Mission C2 contract);
    # all scope is LENIENT unless --strict or SOVYX_C_GATE_STRICT=1.
    if scope == "voice":
        strict = True
    else:  # scope == "all"
        strict = args.strict or os.environ.get("SOVYX_C_GATE_STRICT") == "1"

    routes_root = args.scan_root or _DEFAULT_ROUTES_DIR
    test_dir = args.test_dir or _DEFAULT_TEST_DIR

    if not routes_root.exists():
        sys.stderr.write(f"FATAL: routes root missing: {routes_root}\n")
        return 1
    if not test_dir.exists():
        sys.stderr.write(f"FATAL: test dir missing: {test_dir}\n")
        return 1

    report = _scan(routes_root, test_dir, scope=scope, strict=strict)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif scope == "voice":
        # Voice scope renders to stdout on pass / vacuous, stderr on
        # fail — mirrors pre-Mission-C-C.6-§1 behaviour.
        rendered = _render_voice_human(report)
        if report.uncovered_models:
            sys.stderr.write(rendered + "\n")
        else:
            sys.stdout.write(rendered + "\n")
    else:
        # All-routes scope always renders to stdout (LENIENT warnings
        # are not errors); STRICT failures still set the exit code.
        sys.stdout.write(_render_all_human(report) + "\n")

    if report.uncovered_models and strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
