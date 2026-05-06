"""Atomic applier for calibration profiles.

Takes a frozen :class:`CalibrationProfile`, partitions its decisions
into ``applicable_decisions`` (``operation=="set"`` AND non-experimental)
vs ``skipped_decisions`` (``advise`` + ``preserve`` + experimental SET),
applies the applicable ones to the appropriate target (``MindConfig.voice``
fields, ALSA mixer via the existing ``_linux_mixer_apply``), validates
post-apply, and persists the profile to
``<data_dir>/<mind_id>/calibration.json`` via T2.7's
:func:`save_calibration_profile`.

Rollback semantics (v0.30.16+):

The mission spec promises atomic apply with snapshot+rollback on any
sub-step failure (mirrors ``_linux_mixer_apply.apply_mixer_preset``).
v0.30.15 ships the **structural** applier -- the only rule (R10) emits
``advise`` decisions, so no SET dispatch path is exercised yet. The
applier records its observed apply attempts but does not yet mutate
state. SET-decision dispatch + per-target snapshot/rollback land in
v0.30.16 alongside R20-R50 rule rollout, where the rules begin emitting
SET decisions for ``mind.voice.*`` fields.

For v0.30.15 ``--calibrate`` is therefore semantically equivalent to
``--full-diag --advise``: the operator gets a structured profile with
verdict + advised actions; manual remediation (``sovyx doctor voice
--fix``) is still required. v0.30.16 promotes R10 + adds R20-R50,
unlocking auto-apply for the canonical Sony VAIO case + 4 more.

History: introduced in v0.30.15 as T2.8 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.calibration._persistence import (
    profile_path,
    save_calibration_profile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice.calibration.schema import (
        CalibrationDecision,
        CalibrationProfile,
    )

logger = get_logger(__name__)


def _short_hash(value: str) -> str:
    """16-hex-char SHA256 prefix; matches engine.py for cross-event correlation."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of one :meth:`CalibrationApplier.apply` call.

    Attributes:
        profile_path: Where the profile was persisted (or where it
            WOULD be persisted in dry-run mode).
        applied_decisions: SET decisions that were actually applied
            (empty in v0.30.15 since R10 emits only advise).
        skipped_decisions: Decisions filtered out -- advise +
            preserve operations + EXPERIMENTAL-confidence SETs.
        advised_actions: Operator-actionable command strings extracted
            from ``advise`` decisions (e.g. ``"sovyx doctor voice
            --fix --yes"``). The CLI surfaces these in green so the
            operator can chain remediation by copy-paste.
        dry_run: Whether persistence + mutation was bypassed.
    """

    profile_path: Path
    applied_decisions: tuple[CalibrationDecision, ...]
    skipped_decisions: tuple[CalibrationDecision, ...]
    advised_actions: tuple[str, ...]
    dry_run: bool


class ApplyError(RuntimeError):
    """Raised when an applier step fails after partial mutation.

    Carries the failed decision + a snapshot of the original value
    so callers (and the rollback path) can render a forensic
    explanation. Reserved for v0.30.16+ when SET-decision dispatch
    lands; v0.30.15 never raises this since the applier only handles
    advise + preserve operations (zero mutation).
    """

    def __init__(
        self,
        message: str,
        *,
        decision: CalibrationDecision,
        original_value: object | None = None,
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.original_value = original_value


class CalibrationApplier:
    """Apply a CalibrationProfile + persist it atomically.

    Stateless across calls: each :meth:`apply` invocation re-reads
    the profile and re-emits its result, so the same applier
    instance can serve multiple per-mind apply requests.
    """

    __slots__ = ("_data_dir",)

    def __init__(self, *, data_dir: Path) -> None:
        """Construct the applier.

        Args:
            data_dir: The Sovyx data directory under which per-mind
                ``calibration.json`` files are persisted.
        """
        self._data_dir = data_dir

    def apply(
        self,
        profile: CalibrationProfile,
        *,
        dry_run: bool = False,
    ) -> ApplyResult:
        """Apply the profile + persist it.

        Partitions decisions, executes any SET decisions (no-op in
        v0.30.15 since R10 emits only advise), persists the profile
        unless ``dry_run=True``, and returns a structured ApplyResult
        the CLI surfaces.

        Args:
            profile: The profile to apply.
            dry_run: Skip persistence + any state mutation. Returns
                what WOULD have been applied so ``--calibrate
                --dry-run`` can render the plan without committing.

        Returns:
            An :class:`ApplyResult` summarizing the outcome.

        Raises:
            ApplyError: If a SET decision targets a field not yet
                supported by this applier (v0.30.15 has no SET
                dispatch wired -- safety net to surface mistakes
                if a future rule emits SET prematurely).
        """
        applicable = profile.applicable_decisions
        skipped = tuple(d for d in profile.decisions if d not in applicable)
        advised_actions = tuple(str(d.value) for d in profile.decisions if d.operation == "advise")

        profile_hash = _short_hash(profile.profile_id)
        mind_hash = _short_hash(profile.mind_id)
        logger.info(
            "voice.calibration.applier.apply_started",
            profile_id_hash=profile_hash,
            mind_id_hash=mind_hash,
            decisions_total=len(profile.decisions),
            applicable_count=len(applicable),
            skipped_count=len(skipped),
            advised_count=len(advised_actions),
            dry_run=dry_run,
        )

        # SET-decision dispatch: in v0.30.15 the only rule (R10) emits
        # advise, so this loop iterates over an empty tuple. Future
        # rules emit SET; the applier raises ApplyError to surface
        # the wire-up gap until per-target dispatch lands in v0.30.20.
        try:
            for decision in applicable:
                self._apply_set_decision(decision, mind_id=profile.mind_id, dry_run=dry_run)
        except ApplyError as exc:
            logger.warning(
                "voice.calibration.applier.apply_failed",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                target=exc.decision.target,
                target_class=exc.decision.target_class,
                operation=exc.decision.operation,
                failure_reason="set_dispatch_unsupported",
            )
            raise

        if dry_run:
            target_path = profile_path(data_dir=self._data_dir, mind_id=profile.mind_id)
            logger.info(
                "voice.calibration.applier.dry_run",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                applicable_count=len(applicable),
                skipped_count=len(skipped),
            )
        else:
            target_path = save_calibration_profile(profile, data_dir=self._data_dir)
            logger.info(
                "voice.calibration.applier.apply_succeeded",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                applicable_count=len(applicable),
                skipped_count=len(skipped),
                advised_count=len(advised_actions),
            )

        return ApplyResult(
            profile_path=target_path,
            applied_decisions=applicable,
            skipped_decisions=skipped,
            advised_actions=advised_actions,
            dry_run=dry_run,
        )

    def _apply_set_decision(
        self,
        decision: CalibrationDecision,
        *,
        mind_id: str,
        dry_run: bool,
    ) -> None:
        """Dispatch a SET decision to its target field.

        v0.30.15 has no rules emitting SET, so this method is a guard
        rail: any unexpected SET decision raises :class:`ApplyError`
        with a clear message. v0.30.16 wires per-target dispatch
        (mind.voice.* via pydantic field setter; tuning.voice.* is
        explicitly NOT auto-applied by design -- see mission spec
        D5).
        """
        # mind_id + dry_run are accepted for forward compatibility;
        # they're consumed by the per-target dispatch table that
        # lands in v0.30.16. Reference them here so static analysers
        # don't flag as unused.
        _ = mind_id
        _ = dry_run
        raise ApplyError(
            f"SET decision on target={decision.target!r} not supported in "
            f"v0.30.15 -- per-target dispatch wires up in v0.30.16 with "
            f"R20-R50 rules. Decision was: {decision!r}",
            decision=decision,
        )
