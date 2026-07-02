"""F-020 regression — MISSION-A.2.P3 `sovyx doctor gates` CLI.

Mission anchor:
``docs-internal/missions/MISSION-A2-operator-trust-remediation-2026-05-20.md``
§T3.1..T3.4.

Pre-fix operators on v0.49.x had no surface to discover Quality Gate
STRICT/LENIENT state, the STRICT-flip target tag, or the validation
gate (V-* in OPERATOR-VALIDATION-BACKLOG-2026.md) that unblocks the
flip. Operators relied on memory or grep through CLAUDE.md.

Post-fix ``sovyx doctor gates`` prints the single-source-of-truth
registry. This test file mechanically anchors the registry shape so a
future commit adding a gate to ``scripts/verify_gates.sh`` MUST also
add a row to ``_QUALITY_GATES`` in the same commit.

DOCTOR-2 (2026-07-02): the anchor is now STRUCTURAL — it parses
``scripts/verify_gates.sh`` (``GATE_TOTAL`` + the per-gate
``GATE_NUM`` blocks and their ``scripts/dev/check_*.py`` names) and
asserts the registry matches. The pre-fix anchor asserted the literal
``range(1, 16)``, which codified a 4-gate drift (gates 16-19 shipped
v0.49.38..v0.49.56 without registry rows) instead of catching it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sovyx.cli.commands.doctor import _QUALITY_GATES, doctor_app

runner = CliRunner()

# Registry statuses the operator surface understands. STRICT-when-
# applicable = hard-fails when its inputs exist on this checkout,
# SKIPs when they are structurally absent (Gate 11 / verify_gates.sh
# `skip` branch contract).
_VALID_STATUSES = {"STRICT", "LENIENT", "STRICT-when-applicable"}

_VERIFY_GATES_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_gates.sh"


def _verify_gates_text() -> str:
    """Read verify_gates.sh, or skip where it is structurally absent.

    ``scripts/`` is tracked, so every dev checkout + CI runner has it;
    the guard covers environment-stripped installs (PyPI sdist test
    runs) per Debugging Rule #12 — a structural absence must SKIP, not
    hard-fail.
    """
    if not _VERIFY_GATES_SCRIPT.is_file():
        pytest.skip("scripts/verify_gates.sh absent (stripped install) — anchor inapplicable")
    return _VERIFY_GATES_SCRIPT.read_text(encoding="utf-8")


def _script_gate_total(text: str) -> int:
    match = re.search(r"^GATE_TOTAL=(\d+)\s*$", text, re.MULTILINE)
    assert match is not None, "verify_gates.sh no longer declares GATE_TOTAL=<n>"
    return int(match.group(1))


def _script_gate_blocks(text: str) -> dict[int, str]:
    """Map each ``GATE_NUM=<n>`` (n >= 1) to its block of script text.

    A gate's block runs from its ``GATE_NUM=`` assignment to the next
    one (or EOF). ``GATE_NUM=0`` is the loop-variable initialiser, not
    a gate.
    """
    matches = [
        (int(m.group(1)), m.start())
        for m in re.finditer(r"^GATE_NUM=(\d+)\s*$", text, re.MULTILINE)
        if int(m.group(1)) >= 1
    ]
    blocks: dict[int, str] = {}
    for i, (number, start) in enumerate(matches):
        end = matches[i + 1][1] if i + 1 < len(matches) else len(text)
        blocks[number] = text[start:end]
    return blocks


class TestDoctorGatesStructuralAnchor:
    """DOCTOR-2 — the registry must track scripts/verify_gates.sh."""

    def test_registry_covers_every_gate_in_verify_gates(self) -> None:
        """Registry numbers == 1..GATE_TOTAL == the script's GATE_NUM set.

        A new gate added to verify_gates.sh without a `_QUALITY_GATES`
        row (or a GATE_TOTAL bump without a block, or vice versa) fails
        here — the anchor can no longer freeze at a stale literal.
        """
        text = _verify_gates_text()
        total = _script_gate_total(text)
        script_numbers = sorted(_script_gate_blocks(text))
        assert script_numbers == list(range(1, total + 1)), (
            f"verify_gates.sh GATE_TOTAL={total} but its GATE_NUM blocks are "
            f"{script_numbers} — script is internally inconsistent."
        )
        registry_numbers = [g.number for g in _QUALITY_GATES]
        assert registry_numbers == list(range(1, total + 1)), (
            f"_QUALITY_GATES MUST cover gates 1–{total} inclusive (parsed from "
            f"scripts/verify_gates.sh GATE_TOTAL); got {registry_numbers}. "
            "A new gate added to scripts/verify_gates.sh MUST add a row to "
            "_QUALITY_GATES in the same commit (F-020 discipline)."
        )

    def test_registry_names_match_gate_check_scripts(self) -> None:
        """Each custom gate's check-script filename appears in its row name.

        Gates 1-7 are stock tooling (no scripts/dev/check_*.py); gates
        8+ each invoke exactly one checker whose filename the registry
        row must carry so `sovyx doctor gates` names the real artifact.
        """
        text = _verify_gates_text()
        for number, block in sorted(_script_gate_blocks(text).items()):
            # Anchor to the INVOCATION line — a gate's leading comment
            # block (which may cite its own script) sits ABOVE its
            # GATE_NUM= assignment and therefore inside the PREVIOUS
            # gate's text span.
            match = re.search(
                r"^\s*if uv run python scripts/dev/(check_\w+\.py)",
                block,
                re.MULTILINE,
            )
            if match is None:
                continue  # stock-tooling gate (ruff/mypy/pytest/tsc/vitest…)
            script_name = match.group(1)
            row = next(g for g in _QUALITY_GATES if g.number == number)
            assert script_name in row.name, (
                f"Gate {number} runs scripts/dev/{script_name} but the registry "
                f"row is named {row.name!r} — rename the row (or fix the gate "
                "number) so the operator surface names the real checker."
            )


class TestDoctorGatesRegistry:
    """F-020 — registry shape + STRICT-flip discipline."""

    def test_every_gate_has_valid_status(self) -> None:
        for gate in _QUALITY_GATES:
            assert gate.status in _VALID_STATUSES, (
                f"Gate {gate.number} ({gate.name}) status={gate.status!r} — "
                f"MUST be one of {sorted(_VALID_STATUSES)} (operator-facing "
                "surface depends on this enum)."
            )

    def test_lenient_gates_have_strict_target_and_unblock_anchor(self) -> None:
        """LENIENT gates MUST cite their STRICT-flip target + unblock anchor.

        The anchor is a V-* ID (operator validation backlog) when the
        flip is operator-gated, or a mission body-work anchor (e.g.
        ``C-P0-1``, ``Ω-3 #68``) when the flip is code-gated — never a
        placeholder.
        """
        for gate in _QUALITY_GATES:
            if gate.status == "LENIENT":
                assert gate.strict_target.startswith("v"), (
                    f"Gate {gate.number} LENIENT but strict_target="
                    f"{gate.strict_target!r} — MUST cite a version tag "
                    "(e.g. v0.54.0)."
                )
                assert gate.validation_gate not in {"", "—"}, (
                    f"Gate {gate.number} LENIENT but validation_gate="
                    f"{gate.validation_gate!r} — MUST cite the V-* ID or "
                    "mission anchor that unblocks the STRICT flip."
                )

    def test_strict_gates_have_em_dash_placeholders(self) -> None:
        """STRICT(-when-applicable) gates have no pending flip target."""
        for gate in _QUALITY_GATES:
            if gate.status.startswith("STRICT"):
                assert gate.strict_target == "—", (
                    f"Gate {gate.number} {gate.status} but strict_target="
                    f"{gate.strict_target!r} — already-strict gates have no "
                    "pending target."
                )


class TestDoctorGatesCli:
    """F-020 — CLI rendering invariants."""

    def test_default_invocation_renders_table(self) -> None:
        result = runner.invoke(doctor_app, ["gates"])
        assert result.exit_code == 0, result.output
        # Header text appears (rich Table renders title).
        assert "Quality Gates" in result.output
        # Every gate's name appears in the rendered output.
        for gate in _QUALITY_GATES:
            assert str(gate.number) in result.output

    def test_json_invocation_emits_valid_json(self) -> None:
        result = runner.invoke(doctor_app, ["gates", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == len(_QUALITY_GATES)
        assert all(
            set(row.keys())
            == {
                "number",
                "name",
                "status",
                "strict_target",
                "validation_gate",
            }
            for row in payload
        )

    def test_json_row_matches_registry(self) -> None:
        """JSON output is a faithful render of the in-memory registry."""
        result = runner.invoke(doctor_app, ["gates", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        for json_row, registry_row in zip(payload, _QUALITY_GATES, strict=True):
            assert json_row["number"] == registry_row.number
            assert json_row["name"] == registry_row.name
            assert json_row["status"] == registry_row.status
            assert json_row["strict_target"] == registry_row.strict_target
            assert json_row["validation_gate"] == registry_row.validation_gate

    def test_lenient_footer_appears_when_any_lenient(self) -> None:
        """When ≥1 gate is LENIENT, the footer pointing to the validation backlog appears."""
        result = runner.invoke(doctor_app, ["gates"])
        assert result.exit_code == 0
        # Footer references the operator-validation backlog explicitly.
        assert "OPERATOR-VALIDATION-BACKLOG-2026.md" in result.output


class TestGateExpectations:
    """F-020 — operator-trust contract for specific gates."""

    def test_gate_11_is_strict_when_applicable(self) -> None:
        """Gate 11 (C5 bundle integrity) flipped W0.1 — DOCTOR-2 closure.

        Pre-fix the row said LENIENT / v0.48.0 while verify_gates.sh
        already hard-failed a present-but-partial bundle.
        """
        gate = next(g for g in _QUALITY_GATES if g.number == 11)
        assert gate.status == "STRICT-when-applicable"
        assert gate.strict_target == "—"

    def test_gate_15_h4_lenient_with_v054_target(self) -> None:
        """Gate 15 (H4 resource hygiene) is LENIENT through V-H4-13 / v0.54.0."""
        gate = next(g for g in _QUALITY_GATES if g.number == 15)
        assert gate.status == "LENIENT"
        assert gate.strict_target == "v0.54.0"
        assert gate.validation_gate == "V-H4-13"

    def test_gate_19_name_lock_lenient_with_v052_target(self) -> None:
        """Gate 19 (Ω-3 name-lock) is LENIENT until v0.52.0 (DOCTOR-2 row)."""
        gate = next(g for g in _QUALITY_GATES if g.number == 19)
        assert gate.status == "LENIENT"
        assert gate.strict_target == "v0.52.0"

    def test_gates_1_to_7_all_strict(self) -> None:
        """Baseline gates (ruff/mypy/bandit/pytest/tsc/vitest) are always STRICT."""
        for gate in _QUALITY_GATES[:7]:
            assert gate.status == "STRICT", (
                f"Gate {gate.number} ({gate.name}) should be STRICT but got {gate.status!r}"
            )
