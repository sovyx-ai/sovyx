"""Rule Protocol + per-evaluation context for the calibration engine.

A "rule" is a pure function over (fingerprint, measurements, triage,
prior_decisions) -> RuleEvaluation. The engine (T2.4 -- ``engine.py``)
discovers rules under :mod:`sovyx.voice.calibration.rules`, sorts them
by priority descending, and invokes ``applies()`` then ``evaluate()``
in order. Each rule that produces decisions records a provenance trace
with the conditions it matched, so ``--explain`` (T2.9) can render
exactly why a decision exists.

Rules live as one file per rule under ``rules/`` named
``R<NN>_<short_slug>.py``. Each module exports a module-level
``rule: CalibrationRule`` singleton; the discovery helper in
:mod:`sovyx.voice.calibration.rules` (``iter_rules()``) walks the
package and collects them.

Determinism contract: ``applies()`` and ``evaluate()`` MUST be pure
functions of their inputs. The engine relies on
``CalibrationProfile`` byte-equivalence for re-runs on identical
fingerprint+measurements, so any time-dependence, randomness, or
side-effect inside a rule breaks the determinism gate.

History: introduced in v0.30.15 as T2.4 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sovyx.voice.calibration.schema import (
        CalibrationDecision,
        HardwareFingerprint,
        MeasurementSnapshot,
    )
    from sovyx.voice.diagnostics import TriageResult


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Inputs visible to a rule during ``applies()`` and ``evaluate()``.

    The context is constructed once per rule firing by the engine and
    passed by reference; rules MUST treat it as immutable. The
    ``prior_decisions`` field is the engine's running list of
    decisions produced by higher-priority rules in the same pass, so
    a low-priority rule can inspect what high-priority rules already
    decided and back off (typical pattern: R70 capture-mode-tune
    skips itself if R20 APO-bypass already set the capture mode).
    """

    fingerprint: HardwareFingerprint
    measurements: MeasurementSnapshot
    triage_result: TriageResult | None
    prior_decisions: tuple[CalibrationDecision, ...]


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """Output of one ``rule.evaluate(ctx)`` call.

    Carries both the decisions emitted and the human-readable
    conditions that matched, so the engine can record a complete
    provenance trace without the rule needing a separate
    ``matched_conditions()`` method.

    Empty ``decisions`` is permitted: a rule may legitimately match
    its preconditions but produce no decisions because, for example,
    a higher-priority rule already covered the field. The engine
    still records the provenance entry so ``--explain`` shows the
    rule was considered.
    """

    decisions: tuple[CalibrationDecision, ...]
    matched_conditions: tuple[str, ...]


@runtime_checkable
class CalibrationRule(Protocol):
    """The contract every calibration rule satisfies.

    Implementations live as one file per rule under
    :mod:`sovyx.voice.calibration.rules` and export a module-level
    ``rule: CalibrationRule`` singleton.

    Attributes:
        rule_id: Stable identifier across versions (e.g.
            ``"R10_mic_attenuated"``). Forms the OTel
            ``voice.calibration.engine.rule_fired{rule_id=...}``
            label so cardinality stays bounded.
        rule_version: Bumped on every behaviour-changing edit to the
            rule body. Used by the persistence layer (T2.7) for
            cache invalidation when a stored profile references an
            obsolete rule version.
        priority: 1-100. Higher fires first. Conflict resolution on
            same target field: highest-priority rule wins (first
            writer in evaluation order); subsequent attempts emit a
            ``rule_conflict`` telemetry event.
        description: One-line operator-facing description used by
            ``--explain`` headers.
    """

    rule_id: str
    rule_version: int
    priority: int
    description: str

    def applies(self, ctx: RuleContext) -> bool:
        """Cheap precondition check. Return True to invoke ``evaluate``."""

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        """Produce decisions + matched conditions for this firing.

        MUST be deterministic: same ``ctx`` -> byte-identical output.
        """
