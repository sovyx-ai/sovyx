"""Forward-chaining rule engine for voice calibration.

Iterates rules in priority order (highest first), invokes
``rule.applies(ctx)`` then ``rule.evaluate(ctx)``, accumulates the
emitted :class:`CalibrationDecision` tuples, records a
:class:`ProvenanceTrace` per firing, and returns a frozen
:class:`CalibrationProfile`.

Design contracts (ratified per mission spec §5.2):

* **Deterministic**: same inputs (fingerprint + measurements + triage)
  -> byte-identical output (modulo profile_id UUID4 and timestamps,
  both injected at engine boundary). Tested via
  ``tests/property/test_calibration_engine.py`` once the property
  test layer lands in T2.11.
* **Forward-chaining only**: no backward-chaining, no goal-seeking,
  no probabilistic inference. Each rule is a pure function from
  context to RuleEvaluation.
* **Conflict resolution**: when 2 rules emit ``set`` decisions for
  the same target, the higher-priority rule wins (first writer in
  evaluation order). Subsequent attempts emit
  ``voice.calibration.engine.rule_conflict`` telemetry but do NOT
  override. ``advise`` and ``preserve`` decisions never conflict
  (multiple advisories for the same target are recorded
  independently).
* **No mutation**: the engine produces a profile but never applies
  it. Application is the applier's job (T2.8 -- ``_applier.py``).

History: introduced in v0.30.15 as T2.4 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING

from sovyx.voice.calibration._provenance import ProvenanceRecorder
from sovyx.voice.calibration.rules import RULE_SET_VERSION, iter_rules
from sovyx.voice.calibration.rules._base import (
    CalibrationRule,
    RuleContext,
)
from sovyx.voice.calibration.schema import (
    CALIBRATION_PROFILE_SCHEMA_VERSION,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
)

if TYPE_CHECKING:
    from sovyx.voice.diagnostics import TriageResult


def _read_engine_version() -> str:
    """Return the running ``sovyx`` package version, or ``"unknown"`` on failure.

    Uses :func:`importlib.metadata.version`; never raises so engine
    instantiation in editable / not-yet-installed contexts (e.g.
    ``pytest`` against an uninstalled checkout) still works.
    """
    try:
        return _pkg_version("sovyx")
    except PackageNotFoundError:
        return "unknown"


class EngineMode(StrEnum):
    """How the engine renders / applies its produced profile.

    Closed enum so the OTel
    ``voice.calibration.engine.run_started{mode=...}`` label has
    bounded cardinality.
    """

    APPLY = "apply"  # Engine produces a profile + applier mutates state.
    DRY_RUN = "dry_run"  # Engine produces a profile; applier is bypassed.
    EXPLAIN = "explain"  # Engine produces a profile; renderer shows rule trace.


class CalibrationEngine:
    """Forward-chaining rule engine producing CalibrationProfile.

    The engine is stateless across calls: each :meth:`evaluate`
    invocation builds a fresh provenance recorder + decision list,
    so the same engine instance can serve multiple per-mind
    calibrations without cross-contamination.
    """

    __slots__ = ("_rules", "_engine_version", "_rule_set_version")

    def __init__(
        self,
        *,
        rules: tuple[CalibrationRule, ...] | None = None,
        engine_version: str | None = None,
        rule_set_version: int | None = None,
    ) -> None:
        """Construct the engine.

        Args:
            rules: Override the rule set (default: discovered via
                :func:`iter_rules`). Test code injects a smaller set
                here for isolation.
            engine_version: Override the running engine version
                string (default: ``importlib.metadata.version("sovyx")``).
                Tests pass a fixed string for determinism.
            rule_set_version: Override the rule set version stamp
                (default: :data:`RULE_SET_VERSION` from the rules
                package). Tests pass a fixed int for determinism.
        """
        discovered = tuple(rules) if rules is not None else tuple(iter_rules())
        # Sort by priority descending. Tie-breaker: rule_id alphabetical
        # (deterministic). Stable sort preserves insertion order for
        # equal-priority rules with equal rule_id (impossible in
        # practice, but spec-precise).
        self._rules: tuple[CalibrationRule, ...] = tuple(
            sorted(discovered, key=lambda r: (-r.priority, r.rule_id))
        )
        self._engine_version: str = (
            engine_version if engine_version is not None else _read_engine_version()
        )
        self._rule_set_version: int = (
            rule_set_version if rule_set_version is not None else RULE_SET_VERSION
        )

    @property
    def rules(self) -> tuple[CalibrationRule, ...]:
        """Read-only view of the engine's rule set, sorted by priority desc."""
        return self._rules

    @property
    def engine_version(self) -> str:
        return self._engine_version

    @property
    def rule_set_version(self) -> int:
        return self._rule_set_version

    def evaluate(
        self,
        *,
        mind_id: str,
        fingerprint: HardwareFingerprint,
        measurements: MeasurementSnapshot,
        triage_result: TriageResult | None = None,
        profile_id: str | None = None,
        generated_at_utc: str | None = None,
    ) -> CalibrationProfile:
        """Run all applicable rules and return a frozen CalibrationProfile.

        Args:
            mind_id: The mind whose calibration is being computed.
                Required (no sentinel default per anti-pattern #35).
            fingerprint: Captured hardware identity.
            measurements: Targeted diag artifacts.
            triage_result: Optional cross-correlation input from L1's
                full-diag verdict. Without it, rules that gate on
                ``triage_result.winner`` (R10, R20, R30, R40, R50)
                will not fire even when the underlying signal is
                present -- this is intentional, the operator should
                run ``--full-diag`` first to disambiguate.
            profile_id: Override for the generated UUID4 (testability).
            generated_at_utc: Override for the generation timestamp
                (testability).

        Returns:
            A frozen :class:`CalibrationProfile` containing every
            decision emitted by every rule that fired, ranked by
            evaluation order (which is priority-descending). The
            ``provenance`` tuple records the matched conditions for
            each rule firing.
        """
        recorder = ProvenanceRecorder()
        decisions: list[CalibrationDecision] = []
        # Track ``(target, target_class)`` pairs of "set" decisions so
        # we can detect conflict on lower-priority rules. ``advise``
        # and ``preserve`` operations are not subject to conflict
        # resolution (multiple advisories for the same target are
        # recorded independently for explainability).
        set_targets_seen: set[tuple[str, str]] = set()

        for rule in self._rules:
            ctx = RuleContext(
                fingerprint=fingerprint,
                measurements=measurements,
                triage_result=triage_result,
                prior_decisions=tuple(decisions),
            )
            if not rule.applies(ctx):
                continue

            evaluation = rule.evaluate(ctx)

            # Conflict resolution: drop "set" decisions whose target
            # was already claimed by a higher-priority rule. Other
            # operations pass through.
            kept: list[CalibrationDecision] = []
            for d in evaluation.decisions:
                if d.operation == "set":
                    key = (d.target, d.target_class)
                    if key in set_targets_seen:
                        # Conflict -- a higher-priority rule already
                        # wrote this target. Skip this decision.
                        # Telemetry emission lands in T2.10
                        # (``voice.calibration.engine.rule_conflict``).
                        continue
                    set_targets_seen.add(key)
                kept.append(d)

            if not kept:
                # Rule fired but every produced decision was either a
                # conflict or empty. Skip the provenance record so
                # ``--explain`` doesn't surface noise.
                continue

            decisions.extend(kept)
            confidence = _aggregate_confidence(tuple(kept))
            recorder.record(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                matched_conditions=evaluation.matched_conditions,
                produced_decisions=tuple(_decision_summary(d) for d in kept),
                confidence=confidence,
            )

        return CalibrationProfile(
            schema_version=CALIBRATION_PROFILE_SCHEMA_VERSION,
            profile_id=profile_id if profile_id is not None else str(uuid.uuid4()),
            mind_id=mind_id,
            fingerprint=fingerprint,
            measurements=measurements,
            decisions=tuple(decisions),
            provenance=recorder.freeze(),
            generated_by_engine_version=self._engine_version,
            generated_by_rule_set_version=self._rule_set_version,
            generated_at_utc=(
                generated_at_utc
                if generated_at_utc is not None
                else datetime.now(tz=UTC).isoformat(timespec="microseconds")
            ),
            signature=None,  # Signed at persistence boundary (T2.7).
        )


def _aggregate_confidence(
    decisions: tuple[CalibrationDecision, ...],
) -> CalibrationConfidence:
    """Pick the most-conservative confidence across produced decisions.

    A rule firing's recorded confidence equals the highest confidence
    among its produced decisions. ``EXPERIMENTAL`` < ``LOW`` <
    ``MEDIUM`` < ``HIGH`` is the ordering used. If no decisions are
    present (impossible in practice -- the caller guards on empty
    ``kept``), defaults to ``LOW``.
    """
    order = {
        CalibrationConfidence.EXPERIMENTAL: 0,
        CalibrationConfidence.LOW: 1,
        CalibrationConfidence.MEDIUM: 2,
        CalibrationConfidence.HIGH: 3,
    }
    if not decisions:
        return CalibrationConfidence.LOW
    return max(decisions, key=lambda d: order[d.confidence]).confidence


def _decision_summary(d: CalibrationDecision) -> str:
    """Render a CalibrationDecision as the one-line provenance string.

    The string format is intended for operator readability under
    ``--explain``; loaders MUST NOT parse it back into a structured
    decision (use the ``decisions`` tuple field for that).
    """
    return f"{d.operation}: {d.target} = {d.value!r} ({d.confidence.value})"
