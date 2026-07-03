#!/usr/bin/env python3
"""Quality Gate 16 — pydantic↔zod twin field-set completeness (Mission C §C.0).

Read-only audit gate that verifies, for each registered (pydantic model,
zod schema) pair, that every WIRE-shape field the pydantic peer emits
is ALSO declared in the zod-twin schema. Producer emits a key under
``alias=`` → if the zod twin omits it, ``z.infer<typeof Schema>``
returns ``unknown`` (via ``.passthrough()``) and the typed consumer
silently drifts behind the producer.

This is the mechanical closure of proposed anti-pattern #59:
``.passthrough()`` is for forward-additive transport unknowns, NOT for
typed-view holes the consumer expects to read.

Closure relationship to the wider Mission C plan:

* Gate 17 surfaces drift at every pydantic→zod pair registered in
  :data:`_REGISTRY`. Phase C.2 closes the canonical drift instance
  (``ResourceCohortMetricsSchema`` missing 11+ post-A.1 SSoT keys, the
  C-P0-1 NOMINATED #1 finding); subsequent Mission C phases register
  additional pairs as response_model coverage expands.

* The gate is LENIENT in v0.49.38..v0.52.x — reports violations + exits
  zero so ``verify_gates.sh`` surfaces them as a warn count but does
  NOT fail. Phase 3 v0.53.0 STRICT promotion replaces the LENIENT
  branch in ``verify_gates.sh`` with ``bad ...``.

Asymmetry semantics:

* zod keys missing from pydantic → IGNORED. Zod-only fields are
  forward-additive (a producer may legitimately not yet emit a field
  the typed view declares optional).
* pydantic keys missing from zod → VIOLATION. The producer is shipping
  a wire-shape key the typed view does not see; consumers reading
  ``z.infer<Schema>`` get ``undefined`` despite the bytes being on the
  wire (the F-PAR-2 / F-ZOD-2 / F-ADV-2 cross-validated forensic
  signature).

Allowlist:

* Per-pair allowlist of intentional pydantic-only fields. Format:
  inside ``_REGISTRY[N].allowlist_pydantic_only`` — declare the alias
  string + a short rationale. Keep this rare and well-justified;
  the gate's whole purpose is to drag drift into view.

Anti-pattern compliance:

* #20 — scanner lives under ``scripts/dev/`` (operational tooling),
  patched via ``patch.object(module, "_extract_zod_keys")`` against this
  module path.
* #59 (proposed) — the canonical instance the gate enforces.

Mission anchor:
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 17 +
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.0.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ZOD_FILE = _REPO_ROOT / "dashboard" / "src" / "types" / "schemas.ts"


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    """A single (pydantic, zod) twin pair to check."""

    pydantic_dotted_path: str
    """Importable dotted path to the pydantic BaseModel — e.g.
    ``sovyx.dashboard.routes.engine_resources.ResourceCohortMetrics``."""

    zod_export_name: str
    """The TypeScript export const name of the zod schema — e.g.
    ``ResourceCohortMetricsSchema``."""

    label: str
    """Human-readable pair label for the report (e.g. ``"H4 / C-P0-1"``)."""

    allowlist_pydantic_only: frozenset[str] = frozenset()
    """Pydantic field aliases that are intentionally NOT in the zod twin
    (rare; each entry should be load-bearing per the anti-pattern #59
    rationale). The gate skips these in the violation count."""


# Registered (pydantic, zod) twin pairs. Expansion of this registry IS
# how Mission C phases C.3+ broaden Gate 17 coverage; the initial C.0
# ship covers the 3 highest-traffic engine_* endpoints + the canonical
# F-ADV-2 / C-P0-1 instance (ResourceCohortMetrics).
_REGISTRY: Final[tuple[_RegistryEntry, ...]] = (
    _RegistryEntry(
        pydantic_dotted_path=("sovyx.dashboard.routes.engine_resources.ResourceCohortMetrics"),
        zod_export_name="ResourceCohortMetricsSchema",
        label="H4 cohort metrics (C-P0-1 NOMINATED #1)",
    ),
    _RegistryEntry(
        pydantic_dotted_path=("sovyx.dashboard.routes.engine_resources.EngineResourcesResponse"),
        zod_export_name="EngineResourcesResponseSchema",
        label="H4 engine resources envelope",
    ),
    _RegistryEntry(
        pydantic_dotted_path=("sovyx.dashboard.routes.engine_degraded.EngineDegradedResponse"),
        zod_export_name="EngineDegradedResponseSchema",
        label="C4 composite degraded banner",
    ),
    # Mission C.1 §C.1-b — quarantine reason transport binding pair.
    # The pydantic model carries ``reason`` / ``derived_reason`` /
    # ``resolved_reason`` as ``QuarantineReason | str`` Union with a
    # BeforeValidator coercion; the zod twin uses
    # ``QuarantineReasonSchema.or(z.string())`` LENIENT mirror. This
    # registry entry closes the Mission C.6 §3 ``QuarantineEntryModel``
    # deferred row by handing Gate 17 a mechanical drift detector for
    # any future pydantic/zod field addition that lands asymmetrically.
    # Phase 3 STRICT v0.53.0 H3 cycle close drops the ``derived_reason``
    # field on the pydantic side; the registry passes through with the
    # surviving field set.
    _RegistryEntry(
        pydantic_dotted_path=("sovyx.dashboard.routes.voice_health.QuarantineEntryModel"),
        zod_export_name="VoiceHealthQuarantineEntrySchema",
        label="C.1 quarantine reason transport (#46 typed-consumer mirror)",
    ),
)


@dataclass(frozen=True, slots=True)
class FieldDrift:
    """One missing-from-zod field at a registered twin pair."""

    pair_label: str
    pydantic_dotted_path: str
    zod_export_name: str
    missing_field_alias: str


@dataclass
class GateReport:
    """Aggregate scanner report — mirrors the Gate 12/13/14/15 shape."""

    pairs_checked: int = 0
    pydantic_fields_inspected: int = 0
    zod_fields_inspected: int = 0
    violations: list[FieldDrift] = field(default_factory=list)
    skipped_pairs: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "pairs_checked": self.pairs_checked,
            "pydantic_fields_inspected": self.pydantic_fields_inspected,
            "zod_fields_inspected": self.zod_fields_inspected,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "pair": v.pair_label,
                    "pydantic": v.pydantic_dotted_path,
                    "zod": v.zod_export_name,
                    "missing_field_alias": v.missing_field_alias,
                }
                for v in self.violations
            ],
            "skipped_pairs": self.skipped_pairs,
        }


def _pydantic_wire_field_names(dotted_path: str) -> set[str]:
    """Return the wire-shape keys for a pydantic BaseModel.

    A field's wire key is ``Field(alias=...)`` when declared, else the
    Python attribute name. Pydantic v2 exposes this via
    ``model_fields[name].alias``.

    Raises ``ImportError`` if the dotted path cannot be resolved;
    ``AttributeError`` if the resolved object is not a BaseModel.
    """
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        msg = f"Invalid dotted path (no module): {dotted_path!r}"
        raise ValueError(msg)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    fields: set[str] = set()
    for attr_name, finfo in cls.model_fields.items():
        alias = getattr(finfo, "alias", None)
        fields.add(alias if alias else attr_name)
    return fields


# Matches the start of a named export const that binds a z-call. The
# capture group is the export name. Tolerates ``= z.\n  .object({`` and
# ``= z.object({`` shapes that show up in this codebase.
_ZOD_EXPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"^export\s+const\s+(\w+)\s*=\s*z",
    re.MULTILINE,
)


# Matches an ``.object({`` opening on its own (the gate scans within a
# bounded window after the matched export) — handles both ``z.object({``
# and ``z\n  .object({`` line wrappings.
_OBJECT_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"\.object\(\s*\{")


# Matches a key line inside a z.object body. Accepts either a bare
# identifier (``foo:``) or a double-quoted string (``"foo.bar":``).
# Used as the inside-block extractor — caller must restrict to the
# correct block depth.
_KEY_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*                                 # indent
    (?:
        (?P<bare>[a-zA-Z_][a-zA-Z0-9_]*)  # bare identifier key
        |
        "(?P<quoted>[^"]+)"               # quoted string key (dotted)
    )
    \s*:                                  # key terminator
    """,
    re.VERBOSE | re.MULTILINE,
)


def _extract_zod_keys(zod_source: str, export_name: str) -> set[str] | None:
    """Return the field keys declared inside a ``z.object({...})`` body
    bound to the given named export, OR ``None`` if the export cannot
    be located.

    This is a brace-depth-aware extractor: it locates the
    ``export const <export_name> = z`` declaration, finds the first
    subsequent ``.object({`` opening, and walks character-by-character
    until the matching closing brace. Any ``key:`` lines whose
    indentation matches the outermost object body are collected.

    The implementation tolerates the codebase's two stylistic shapes:
    ``= z.object({`` and ``= z\\n  .object({``. Both are common in
    ``dashboard/src/types/schemas.ts``.
    """
    for match in _ZOD_EXPORT_RE.finditer(zod_source):
        if match.group(1) != export_name:
            continue
        # Found the export. Scan forward for the first .object({ opener.
        scan_start = match.end()
        opener = _OBJECT_OPEN_RE.search(zod_source, scan_start)
        if opener is None:
            return None
        body_start = opener.end()  # position right after the `{`
        # Walk brace depth from this point. Stop when depth returns to 0.
        depth = 1
        i = body_start
        n = len(zod_source)
        # Track string-literal state so braces inside strings don't
        # bias the depth count. Handles double quotes + escapes; this
        # codebase doesn't use template literals inside zod schemas.
        in_string = False
        escape = False
        while i < n and depth > 0:
            ch = zod_source[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            i += 1
        if depth != 0:
            # Malformed source — refuse to guess.
            return None
        body_end = i - 1  # position of the matching `}`
        body = zod_source[body_start:body_end]
        keys: set[str] = set()
        # Only collect keys whose LINE START was at the outermost nesting
        # level of `body`. A line like ``  outer_key: z.object({`` starts
        # at depth 0 but ends at depth 1 — its `key:` IS a top-level
        # field of the parent object, so we capture it; subsequent lines
        # of that nested object body start at depth ≥ 1 and are skipped.
        depth2 = 0
        line_start = 0
        depth_at_line_start = 0
        in_str2 = False
        esc2 = False
        for idx, ch in enumerate(body):
            if in_str2:
                if esc2:
                    esc2 = False
                elif ch == "\\":
                    esc2 = True
                elif ch == '"':
                    in_str2 = False
                continue
            if ch == '"':
                in_str2 = True
            elif ch == "{":
                depth2 += 1
            elif ch == "}":
                depth2 -= 1
            elif ch == "\n":
                line = body[line_start:idx]
                if depth_at_line_start == 0:
                    km = _KEY_LINE_RE.match(line)
                    if km is not None:
                        bare = km.group("bare")
                        quoted = km.group("quoted")
                        keys.add(quoted if quoted is not None else bare)
                line_start = idx + 1
                depth_at_line_start = depth2
        # Final partial line (no trailing newline).
        if line_start < len(body) and depth_at_line_start == 0:
            tail = body[line_start:]
            km = _KEY_LINE_RE.match(tail)
            if km is not None:
                bare = km.group("bare")
                quoted = km.group("quoted")
                keys.add(quoted if quoted is not None else bare)
        return keys
    return None


def _check_pair(
    entry: _RegistryEntry,
    zod_source: str,
    report: GateReport,
) -> None:
    """Inspect one registry entry; append any violations to ``report``."""
    try:
        pyd_keys = _pydantic_wire_field_names(entry.pydantic_dotted_path)
    except Exception as exc:  # noqa: BLE001 — pair skip, not gate fail
        report.skipped_pairs.append(
            f"{entry.label}: pydantic import failed: {exc!r}",
        )
        return
    zod_keys = _extract_zod_keys(zod_source, entry.zod_export_name)
    if zod_keys is None:
        report.skipped_pairs.append(
            f"{entry.label}: zod export {entry.zod_export_name!r} not found",
        )
        return
    report.pairs_checked += 1
    report.pydantic_fields_inspected += len(pyd_keys)
    report.zod_fields_inspected += len(zod_keys)
    missing = pyd_keys - zod_keys - set(entry.allowlist_pydantic_only)
    for alias in sorted(missing):
        report.violations.append(
            FieldDrift(
                pair_label=entry.label,
                pydantic_dotted_path=entry.pydantic_dotted_path,
                zod_export_name=entry.zod_export_name,
                missing_field_alias=alias,
            ),
        )


def scan_registry(zod_file: Path = _DEFAULT_ZOD_FILE) -> GateReport:
    """Run Gate 17 over :data:`_REGISTRY` against the project's zod file."""
    report = GateReport()
    try:
        zod_source = zod_file.read_text(encoding="utf-8")
    except OSError as exc:
        report.skipped_pairs.append(
            f"zod file unavailable: {zod_file} ({exc!r})",
        )
        return report
    for entry in _REGISTRY:
        _check_pair(entry, zod_source, report)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mission C Gate 17 — pydantic↔zod twin field-set parity. "
            "LENIENT by default (reports + exit 0); STRICT via --strict or "
            "SOVYX_C_GATE_STRICT=1 (exit 1 on any violation)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any violation (Phase 3 STRICT promotion).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report on stdout instead of the human summary.",
    )
    parser.add_argument(
        "--zod-file",
        type=Path,
        default=_DEFAULT_ZOD_FILE,
        help=(
            "Override the zod-twin schemas.ts path. Default: "
            "dashboard/src/types/schemas.ts under the repo root."
        ),
    )
    return parser


def _render_human(report: GateReport) -> str:
    lines: list[str] = []
    lines.append(
        f"Gate 17 — zod twin completeness: "
        f"{report.pairs_checked} pair(s), "
        f"{report.pydantic_fields_inspected} pydantic field(s) inspected, "
        f"{report.zod_fields_inspected} zod field(s) inspected.",
    )
    if report.skipped_pairs:
        lines.append("Skipped pairs:")
        for entry in report.skipped_pairs:
            lines.append(f"  - {entry}")
    if report.violations:
        lines.append(
            f"{len(report.violations)} violation(s) — pydantic fields absent "
            "from the zod twin (anti-pattern #59 surface):",
        )
        # Group by pair for readability.
        by_pair: dict[str, list[str]] = {}
        for v in report.violations:
            by_pair.setdefault(v.pair_label, []).append(v.missing_field_alias)
        for pair_label, aliases in by_pair.items():
            lines.append(f"  {pair_label}:")
            for alias in sorted(aliases):
                lines.append(f"    - {alias}")
        lines.append("zod twin discipline: VIOLATIONS")
    else:
        lines.append("zod twin discipline: PASS")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    report = scan_registry(zod_file=args.zod_file)
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
