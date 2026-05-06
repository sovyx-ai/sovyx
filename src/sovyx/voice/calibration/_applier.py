"""Atomic applier for calibration profiles.

Takes a frozen :class:`CalibrationProfile`, classifies its decisions
into per-confidence-band dispositions, applies the qualifying SET
decisions via a registry of target-class handlers with snapshot+LIFO
rollback semantics, and persists the profile to
``<data_dir>/<mind_id>/calibration.json`` via T2.7's
:func:`save_calibration_profile`.

P1 (v0.30.29) architecture (mission ``MISSION-voice-calibration-extreme-audit-2026-05-06.md`` §5):

* :data:`_TARGET_CLASS_HANDLERS` — registry of async handlers keyed by
  ``CalibrationDecision.target_class``. Two ship in v0.30.29:
  ``"LinuxMixerApply"`` (attenuation/saturation remediation via the
  proven ``apply_mixer_boost_up`` / ``apply_mixer_reset`` path) and
  ``"MindConfig.voice"`` (per-field setattr + ``mind.yaml`` persist).
* :class:`_PreApplySnapshot` — captures pre-apply state once before any
  mutation; populated incrementally by handlers as they record their
  per-decision rollback tokens.
* :func:`CalibrationApplier.apply` is now async; on any sub-decision
  :class:`ApplyError` the LIFO rollback path replays
  ``_revert_decision`` for every previously-applied decision in
  reverse order. Rollback step failures are logged at WARN but never
  mask the original ``ApplyError``.
* Confidence-band gating per spec D7: ``HIGH`` auto-applies,
  ``MEDIUM`` requires ``allow_medium=True`` (CLI ``--yes`` / frontend
  confirm), ``LOW`` is advise-only, ``EXPERIMENTAL`` is skipped.

History:
* v0.30.15 (T2.8) — structural applier; only advise decisions.
* v0.30.29 (P1) — registry + snapshot + LIFO rollback + confidence-band
  gating + R10 promoted from advise to SET targeting LinuxMixerApply.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash as _short_hash
from sovyx.voice.calibration._persistence import (
    profile_path,
    save_calibration_profile,
)
from sovyx.voice.calibration.schema import CalibrationConfidence

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice.calibration.schema import (
        CalibrationDecision,
        CalibrationProfile,
    )
    from sovyx.voice.health.contract import MixerApplySnapshot

# Type aliases used by the handler registry. Both halves are async.
_HandlerFn = Callable[
    ["CalibrationDecision", "_PreApplySnapshot", "CalibrationApplier"],
    Awaitable[Any],
]
_ReverterFn = Callable[
    [Any, "_PreApplySnapshot", "CalibrationApplier"],
    Awaitable[None],
]

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════
# Public types
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of one :meth:`CalibrationApplier.apply` call.

    Attributes:
        profile_path: Where the profile was persisted (or where it
            WOULD be persisted in dry-run mode).
        applied_decisions: SET decisions that were actually applied.
        skipped_decisions: Decisions filtered out (advise + preserve +
            EXPERIMENTAL + LOW-confidence + MEDIUM without
            ``allow_medium``).
        advised_actions: Operator-actionable command strings extracted
            from ``advise`` decisions. The CLI surfaces these in green
            so the operator can chain remediation by copy-paste.
        confirm_required_decisions: MEDIUM-confidence SET decisions
            that did NOT auto-apply (operator confirmation required).
            Surfaced separately so frontends can render a confirm
            dialog and re-invoke with ``allow_medium=True``.
        dry_run: Whether persistence + mutation was bypassed.
        rolled_back: Set to True when the apply chain failed mid-way
            and triggered LIFO rollback. Original ``ApplyError`` is
            re-raised; this flag exists for telemetry/test inspection.
    """

    profile_path: Path
    applied_decisions: tuple[CalibrationDecision, ...]
    skipped_decisions: tuple[CalibrationDecision, ...]
    advised_actions: tuple[str, ...]
    confirm_required_decisions: tuple[CalibrationDecision, ...] = ()
    dry_run: bool = False
    rolled_back: bool = False


class ApplyError(RuntimeError):
    """Raised when a SET decision fails to apply.

    Carries the failed decision + its position in the ``applicable``
    list + a snapshot of the original value so callers (and the LIFO
    rollback path) can render forensic explanations.

    Attributes:
        decision: The :class:`CalibrationDecision` whose handler raised.
        original_value: The pre-apply value of ``decision.target``,
            captured by the snapshot path; useful for forensic
            "what was rolled back to?" displays.
        decision_index: Position in the applier's ``applicable``
            tuple; correlates with the snapshot's
            ``decision_results`` for exact rollback inspection.
        rolled_back: Set to True when the applier's LIFO rollback
            path completed before re-raising the error (P6 v0.30.34
            adds the flag so downstream catchers — wizard
            orchestrator, dashboard banner — can surface "auto-
            rollback fired" without parsing message strings).
    """

    def __init__(
        self,
        message: str,
        *,
        decision: CalibrationDecision,
        original_value: object | None = None,
        decision_index: int | None = None,
        rolled_back: bool = False,
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.original_value = original_value
        self.decision_index = decision_index
        self.rolled_back = rolled_back


# ════════════════════════════════════════════════════════════════════
# Confidence-band dispositions (D7)
# ════════════════════════════════════════════════════════════════════


class _DecisionDisposition(StrEnum):
    """How a decision is treated by :meth:`CalibrationApplier.apply`.

    Mapped from ``operation`` + ``confidence`` per mission spec D7:

    * ``HIGH`` SET → :data:`AUTO_APPLY`
    * ``MEDIUM`` SET → :data:`CONFIRM_REQUIRED` (auto-apply if caller
      passes ``allow_medium=True``)
    * ``LOW`` SET → :data:`ADVISE_ONLY` (never auto-apply)
    * ``EXPERIMENTAL`` SET → :data:`SKIP`
    * ``advise`` operation → :data:`ADVISE_ONLY`
    * ``preserve`` operation → :data:`SKIP`
    """

    AUTO_APPLY = "auto_apply"
    CONFIRM_REQUIRED = "confirm_required"
    ADVISE_ONLY = "advise_only"
    SKIP = "skip"


def _classify_decision_disposition(
    decision: CalibrationDecision,
    *,
    allow_medium: bool,
) -> _DecisionDisposition:
    """Return the disposition for one decision per the D7 contract."""
    if decision.operation == "advise":
        return _DecisionDisposition.ADVISE_ONLY
    if decision.operation == "preserve":
        return _DecisionDisposition.SKIP
    if decision.operation != "set":
        # Unknown operation; safest to skip rather than dispatch.
        return _DecisionDisposition.SKIP

    if decision.confidence == CalibrationConfidence.HIGH:
        return _DecisionDisposition.AUTO_APPLY
    if decision.confidence == CalibrationConfidence.MEDIUM:
        return (
            _DecisionDisposition.AUTO_APPLY
            if allow_medium
            else _DecisionDisposition.CONFIRM_REQUIRED
        )
    if decision.confidence == CalibrationConfidence.LOW:
        return _DecisionDisposition.ADVISE_ONLY
    return _DecisionDisposition.SKIP  # EXPERIMENTAL


# ════════════════════════════════════════════════════════════════════
# Snapshot dataclass
# ════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _PreApplySnapshot:
    """Captures pre-apply state for atomic rollback.

    Populated before any mutation begins; handlers append rollback
    tokens to :attr:`decision_results` as they apply. On
    :class:`ApplyError`, the LIFO rollback path replays the per-handler
    revert function in reverse order.

    The fields are MUTABLE because the apply chain populates them
    incrementally — but the type is module-private (single-writer,
    single-reader within ``CalibrationApplier.apply``).
    """

    # Pre-apply MindConfig dump (for ``MindConfig.voice`` field reverts).
    mind_config_before: dict[str, Any] = field(default_factory=dict)
    # Per-card mixer snapshots (for ``LinuxMixerApply`` reverts).
    mixer_snapshots: dict[int, MixerApplySnapshot] = field(default_factory=dict)
    # (decision_index, applied_ok, rollback_token) per applied decision.
    decision_results: list[tuple[int, bool, Any]] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════
# Handler registry
# ════════════════════════════════════════════════════════════════════


_TARGET_CLASS_HANDLERS: dict[str, tuple[_HandlerFn, _ReverterFn | None]] = {}
"""Registry: ``target_class`` -> ``(apply_handler, revert_handler)``.

Both halves are async functions. Adding a new target class is a
two-step registration via :func:`register_target_class_handler`.
"""


def register_target_class_handler(
    target_class: str,
) -> Callable[[_HandlerFn], _HandlerFn]:
    """Decorator: register an apply handler for ``target_class``.

    Use it WITHOUT a revert handler when the apply is reversible by
    re-running with the inverse value (rare). Most call sites should
    use :func:`register_target_class_pair` instead, which registers
    both apply + revert.

    Example::

        @register_target_class_handler("MyClass")
        async def _apply_my_class(decision, snapshot, applier):
            ...
            return rollback_token
    """

    def _decorator(fn: _HandlerFn) -> _HandlerFn:
        _TARGET_CLASS_HANDLERS[target_class] = (fn, None)
        return fn

    return _decorator


def register_target_class_pair(
    target_class: str,
    *,
    apply: _HandlerFn,
    revert: _ReverterFn,
) -> None:
    """Register both apply + revert handlers for ``target_class``.

    The revert handler accepts the rollback token returned by ``apply``
    plus the snapshot + applier, and is responsible for restoring the
    pre-apply state. It is invoked in LIFO order on
    :class:`ApplyError`.
    """
    _TARGET_CLASS_HANDLERS[target_class] = (apply, revert)


def _lookup_handler_pair(
    target_class: str,
) -> tuple[_HandlerFn, _ReverterFn | None]:
    """Return ``(apply_fn, revert_fn)`` for ``target_class``.

    Raises :class:`KeyError` when the target class has no registered
    handler — surfaced by the applier as :class:`ApplyError` with a
    structured message so the operator sees which decision failed.
    """
    return _TARGET_CLASS_HANDLERS[target_class]


# ════════════════════════════════════════════════════════════════════
# Applier
# ════════════════════════════════════════════════════════════════════


class CalibrationApplier:
    """Apply a CalibrationProfile + persist it atomically.

    Stateless across calls: each :meth:`apply` invocation re-builds the
    pre-apply snapshot, so the same applier instance can serve multiple
    per-mind apply requests safely.

    Args:
        data_dir: The Sovyx data directory under which per-mind
            ``calibration.json`` files are persisted.
        mind_yaml_path: Path to ``mind.yaml`` for the
            ``MindConfig.voice`` handler. ``None`` (default) resolves
            to ``<data_dir>/<mind_id>/mind.yaml`` at apply time.
        tuning: Voice tuning config; passed to mixer apply paths.
            ``None`` (default) instantiates ``VoiceTuningConfig()``
            on first use.
        signing_key_path: Optional Ed25519 PEM private key path. When
            supplied, the profile is signed at the persistence boundary
            (P4 v0.30.32). ``None`` (default) writes unsigned profiles
            which the loader treats as ``REJECTED_NO_SIGNATURE``
            (LENIENT-accepted, STRICT-rejected).
    """

    __slots__ = ("_data_dir", "_mind_yaml_path", "_signing_key_path", "_tuning")

    def __init__(
        self,
        *,
        data_dir: Path,
        mind_yaml_path: Path | None = None,
        tuning: Any = None,  # noqa: ANN401 -- VoiceTuningConfig (lazy-imported to avoid pydantic circular)
        signing_key_path: Path | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._mind_yaml_path = mind_yaml_path
        self._tuning = tuning
        self._signing_key_path = signing_key_path

    # ────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────

    async def apply(
        self,
        profile: CalibrationProfile,
        *,
        dry_run: bool = False,
        allow_medium: bool = False,
    ) -> ApplyResult:
        """Apply the profile + persist it.

        Classifies decisions into dispositions, dispatches AUTO_APPLY
        ones via the registered handlers, persists the profile unless
        ``dry_run=True``, and returns a structured :class:`ApplyResult`.

        Args:
            profile: The profile to apply.
            dry_run: Skip persistence + any state mutation. Returns
                what WOULD have been applied so ``--calibrate
                --dry-run`` can render the plan without committing.
            allow_medium: When True, MEDIUM-confidence SET decisions
                are auto-applied alongside HIGH ones. Wired to the
                CLI's ``--yes`` flag and the frontend's confirmation
                dialog.

        Returns:
            An :class:`ApplyResult` summarising the outcome.

        Raises:
            ApplyError: When a SET decision fails mid-apply. Before
                the exception propagates, the LIFO rollback path
                replays every previously-applied decision in reverse
                order; rollback step failures are logged at WARN but
                do NOT mask the original ApplyError.
        """
        partition = self._partition_decisions(profile, allow_medium=allow_medium)
        applicable = partition["auto_apply"]
        confirm_required = partition["confirm_required"]
        skipped = partition["skipped"]
        advised_actions = tuple(str(d.value) for d in profile.decisions if d.operation == "advise")

        profile_hash = _short_hash(profile.profile_id)
        mind_hash = _short_hash(profile.mind_id)
        logger.info(
            "voice.calibration.applier.apply_started",
            profile_id_hash=profile_hash,
            mind_id_hash=mind_hash,
            decisions_total=len(profile.decisions),
            applicable_count=len(applicable),
            confirm_required_count=len(confirm_required),
            skipped_count=len(skipped),
            advised_count=len(advised_actions),
            dry_run=dry_run,
            allow_medium=allow_medium,
        )

        snapshot = _PreApplySnapshot()
        rolled_back = False

        if applicable and not dry_run:
            try:
                await self._dispatch_with_rollback(
                    applicable,
                    snapshot=snapshot,
                    profile=profile,
                    profile_hash=profile_hash,
                    mind_hash=mind_hash,
                )
            except ApplyError as exc:
                rolled_back = True
                logger.warning(
                    "voice.calibration.applier.apply_failed",
                    profile_id_hash=profile_hash,
                    mind_id_hash=mind_hash,
                    target=exc.decision.target,
                    target_class=exc.decision.target_class,
                    operation=exc.decision.operation,
                    decision_index=exc.decision_index,
                    failure_reason="set_dispatch_failed",
                )
                raise

        if dry_run:
            target_path = profile_path(data_dir=self._data_dir, mind_id=profile.mind_id)
            logger.info(
                "voice.calibration.applier.dry_run",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                applicable_count=len(applicable),
                confirm_required_count=len(confirm_required),
                skipped_count=len(skipped),
            )
        else:
            target_path = save_calibration_profile(
                profile,
                data_dir=self._data_dir,
                signing_key_path=self._signing_key_path,
            )
            logger.info(
                "voice.calibration.applier.apply_succeeded",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                decisions_applied=len(applicable),
                applicable_count=len(applicable),
                confirm_required_count=len(confirm_required),
                skipped_count=len(skipped),
                advised_count=len(advised_actions),
            )

        return ApplyResult(
            profile_path=target_path,
            applied_decisions=applicable,
            skipped_decisions=skipped,
            advised_actions=advised_actions,
            confirm_required_decisions=confirm_required,
            dry_run=dry_run,
            rolled_back=rolled_back,
        )

    # ────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _partition_decisions(
        profile: CalibrationProfile,
        *,
        allow_medium: bool,
    ) -> dict[str, tuple[CalibrationDecision, ...]]:
        """Bucket decisions by disposition + return tuples per bucket."""
        auto_apply: list[CalibrationDecision] = []
        confirm_required: list[CalibrationDecision] = []
        skipped: list[CalibrationDecision] = []
        for d in profile.decisions:
            disp = _classify_decision_disposition(d, allow_medium=allow_medium)
            if disp == _DecisionDisposition.AUTO_APPLY:
                auto_apply.append(d)
            elif disp == _DecisionDisposition.CONFIRM_REQUIRED:
                confirm_required.append(d)
            else:
                # ADVISE_ONLY + SKIP both fall in the "skipped" bucket
                # for the result; advise decisions are also extracted
                # separately into ``advised_actions`` for the CLI.
                skipped.append(d)
        return {
            "auto_apply": tuple(auto_apply),
            "confirm_required": tuple(confirm_required),
            "skipped": tuple(skipped),
        }

    async def _dispatch_with_rollback(
        self,
        applicable: tuple[CalibrationDecision, ...],
        *,
        snapshot: _PreApplySnapshot,
        profile: CalibrationProfile,
        profile_hash: str,
        mind_hash: str,
    ) -> None:
        """Iterate applicable decisions; on ApplyError, LIFO-rollback."""
        applied: list[tuple[int, str, Any]] = []  # (idx, target_class, token)
        rollback_started_mono = 0.0
        try:
            for idx, decision in enumerate(applicable):
                token = await self._apply_one_decision(
                    decision,
                    snapshot=snapshot,
                    decision_index=idx,
                )
                applied.append((idx, decision.target_class, token))
                snapshot.decision_results.append((idx, True, token))
        except ApplyError as exc:
            rollback_started_mono = time.monotonic()
            await self._lifo_rollback(applied, snapshot=snapshot)
            rollback_duration_s = round(time.monotonic() - rollback_started_mono, 3)
            logger.warning(
                "voice.calibration.applier.apply_failed_with_rollback",
                profile_id_hash=profile_hash,
                mind_id_hash=mind_hash,
                decisions_rolled_back=len(applied),
                rollback_duration_s=rollback_duration_s,
            )
            # P6 (v0.30.34) — surface the rolled_back flag on the
            # exception so the wizard orchestrator + dashboard banner
            # can render "auto-rollback fired" without parsing the
            # error message string.
            exc.rolled_back = True
            # Reference profile to acknowledge it for static analysis
            # without leaking through telemetry; profile_id_hash + mind_id_hash
            # already cover identification.
            _ = profile
            raise

    async def _apply_one_decision(
        self,
        decision: CalibrationDecision,
        *,
        snapshot: _PreApplySnapshot,
        decision_index: int,
    ) -> Any:  # noqa: ANN401 -- opaque rollback token; type varies per handler
        """Dispatch one SET decision via the registry; return rollback token."""
        try:
            apply_fn, _revert_fn = _lookup_handler_pair(decision.target_class)
        except KeyError as exc:
            raise ApplyError(
                f"no handler registered for target_class={decision.target_class!r} "
                f"(decision: target={decision.target!r}, value={decision.value!r}). "
                f"Available: {sorted(_TARGET_CLASS_HANDLERS)}",
                decision=decision,
                decision_index=decision_index,
            ) from exc
        try:
            return await apply_fn(decision, snapshot, self)
        except ApplyError:
            raise
        except Exception as exc:
            raise ApplyError(
                f"handler for target_class={decision.target_class!r} raised "
                f"{type(exc).__name__}: {exc}",
                decision=decision,
                decision_index=decision_index,
            ) from exc

    async def _lifo_rollback(
        self,
        applied: list[tuple[int, str, Any]],
        *,
        snapshot: _PreApplySnapshot,
    ) -> None:
        """Replay each applied decision's revert handler in reverse order.

        Step failures are logged at WARN but do NOT mask the original
        ApplyError. After this method returns, the caller re-raises
        the ApplyError so the operator sees both the original failure
        and the rollback outcome.
        """
        for rev_idx in range(len(applied) - 1, -1, -1):
            decision_index, target_class, token = applied[rev_idx]
            try:
                _apply_fn, revert_fn = _lookup_handler_pair(target_class)
            except KeyError:
                logger.warning(
                    "voice.calibration.applier.rollback_step_failed",
                    decision_index=decision_index,
                    target_class=target_class,
                    exception_type="KeyError",
                    reason="handler_unregistered_during_rollback",
                )
                continue
            if revert_fn is None:
                logger.warning(
                    "voice.calibration.applier.rollback_step_failed",
                    decision_index=decision_index,
                    target_class=target_class,
                    exception_type="NotImplementedError",
                    reason="no_revert_registered",
                )
                continue
            try:
                await revert_fn(token, snapshot, self)
            except Exception as exc:  # noqa: BLE001 -- best-effort rollback
                logger.warning(
                    "voice.calibration.applier.rollback_step_failed",
                    decision_index=decision_index,
                    target_class=target_class,
                    exception_type=type(exc).__name__,
                )

    # ────────────────────────────────────────────────────────────────
    # Lazy dependency resolution (used by handlers)
    # ────────────────────────────────────────────────────────────────

    def _resolve_tuning(self) -> Any:  # noqa: ANN401 -- VoiceTuningConfig lazy-imported
        """Return a :class:`VoiceTuningConfig`, instantiating once if needed."""
        if self._tuning is not None:
            return self._tuning
        # Local import to keep schema/circular surface minimal at top.
        from sovyx.engine.config import VoiceTuningConfig

        self._tuning = VoiceTuningConfig()
        return self._tuning

    def _resolve_mind_yaml_path(self, mind_id: str) -> Path:
        """Return the mind.yaml path for ``mind_id``.

        Defaults to ``<data_dir>/<mind_id>/mind.yaml`` when the
        constructor was not given an explicit path.
        """
        if self._mind_yaml_path is not None:
            return self._mind_yaml_path
        return self._data_dir / mind_id / "mind.yaml"


# ════════════════════════════════════════════════════════════════════
# Built-in handlers (registered on import)
# ════════════════════════════════════════════════════════════════════


# ── LinuxMixerApply ────────────────────────────────────────────────


async def _apply_linux_mixer(
    decision: CalibrationDecision,
    snapshot: _PreApplySnapshot,
    applier: CalibrationApplier,
) -> tuple[int, MixerApplySnapshot]:
    """Apply a Linux ALSA mixer remediation.

    Recognised ``decision.value`` strings:

    * ``"boost_up"`` — call :func:`apply_mixer_boost_up` on the first
      attenuated card. Used by R10 for the canonical Sony VAIO H10
      attenuation case.
    * ``"reset"`` — call :func:`apply_mixer_reset` on the first
      saturating card. Reserved for a future R<NN>_mic_saturated rule.

    Returns ``(card_index, MixerApplySnapshot)``; the revert handler
    looks the snapshot up in :attr:`_PreApplySnapshot.mixer_snapshots`
    and replays it via :func:`restore_mixer_snapshot`.

    Raises :class:`ApplyError` on platform mismatch (non-Linux),
    missing ``amixer``, or apply subprocess failure. The caller's
    LIFO rollback handles partial-apply by reverting prior decisions.
    """
    import sys  # noqa: PLC0415 -- local platform check

    if sys.platform != "linux":
        raise ApplyError(
            f"LinuxMixerApply handler requires Linux; running on {sys.platform!r}. "
            f"This decision should have been gated by the rule's platform check.",
            decision=decision,
        )

    # Local imports to keep _applier.py importable on platforms that
    # never have the linux_mixer_* modules loaded.
    from sovyx.voice.health._linux_mixer_apply import (
        apply_mixer_boost_up,
        apply_mixer_reset,
    )
    from sovyx.voice.health._linux_mixer_check import _is_attenuated
    from sovyx.voice.health._linux_mixer_probe import enumerate_alsa_mixer_snapshots
    from sovyx.voice.health.bypass._strategy import BypassApplyError

    intent = str(decision.value)
    if intent not in ("boost_up", "reset"):
        raise ApplyError(
            f"LinuxMixerApply handler does not recognise value={intent!r}. "
            f"Supported: 'boost_up' (attenuation), 'reset' (saturation).",
            decision=decision,
        )

    mixer_snapshots = await asyncio.to_thread(enumerate_alsa_mixer_snapshots)
    if not mixer_snapshots:
        raise ApplyError(
            "no ALSA cards enumerable -- alsa-utils not installed or no audio "
            "devices present. The LinuxMixerApply handler requires at least "
            "one card to mutate.",
            decision=decision,
        )

    if intent == "boost_up":
        candidates = [s for s in mixer_snapshots if _is_attenuated(s)]
    else:  # intent == "reset"
        candidates = [s for s in mixer_snapshots if s.saturation_warning]

    if not candidates:
        raise ApplyError(
            f"LinuxMixerApply value={intent!r} found no matching cards "
            f"({'attenuated' if intent == 'boost_up' else 'saturating'}). "
            f"The rule fired but the regime is no longer present at apply "
            f"time -- this may indicate operator manual intervention "
            f"between probe and apply.",
            decision=decision,
        )

    card_snapshot = candidates[0]
    tuning = applier._resolve_tuning()
    try:
        if intent == "boost_up":
            apply_snap = await apply_mixer_boost_up(
                card_snapshot.card_index,
                card_snapshot,
                tuning=tuning,
            )
        else:  # intent == "reset"
            apply_snap = await apply_mixer_reset(
                card_snapshot.card_index,
                card_snapshot,
                tuning=tuning,
            )
    except BypassApplyError as exc:
        raise ApplyError(
            f"apply_mixer_{intent} failed on card {card_snapshot.card_index}: "
            f"{exc.reason} ({exc})",
            decision=decision,
        ) from exc

    snapshot.mixer_snapshots[card_snapshot.card_index] = apply_snap
    logger.info(
        "voice.calibration.applier.linux_mixer_applied",
        card_index=card_snapshot.card_index,
        intent=intent,
        controls_reverted=len(apply_snap.reverted_controls),
        controls_applied=len(apply_snap.applied_controls),
    )
    return (card_snapshot.card_index, apply_snap)


async def _revert_linux_mixer(
    token: tuple[int, MixerApplySnapshot],
    snapshot: _PreApplySnapshot,
    applier: CalibrationApplier,
) -> None:
    """Restore the pre-apply mixer state for the card the apply mutated."""
    card_index, apply_snap = token
    from sovyx.voice.health._linux_mixer_apply import restore_mixer_snapshot

    tuning = applier._resolve_tuning()
    await restore_mixer_snapshot(apply_snap, tuning=tuning)
    snapshot.mixer_snapshots.pop(card_index, None)
    logger.info(
        "voice.calibration.applier.linux_mixer_reverted",
        card_index=card_index,
    )


register_target_class_pair(
    "LinuxMixerApply",
    apply=_apply_linux_mixer,
    revert=_revert_linux_mixer,
)


# ── MindConfig.voice ───────────────────────────────────────────────


async def _apply_mind_config_voice(
    decision: CalibrationDecision,
    snapshot: _PreApplySnapshot,
    applier: CalibrationApplier,
) -> tuple[str, str, Any]:
    """Mutate ``MindConfig.voice.<field>`` + persist to ``mind.yaml``.

    The decision's ``target`` is a dotted path ending with the field
    name on ``MindConfig.voice`` (e.g. ``"mind.voice.vad_threshold"``);
    only the trailing component is consumed. The pre-mutation value
    is captured in the snapshot's ``mind_config_before`` so the
    reverter can restore it byte-for-byte.

    Raises :class:`ApplyError` on missing field, invalid value type,
    or YAML write failure.
    """
    field_name = decision.target.split(".")[-1]
    mind_id = decision.target.split(".")[0] if "." in decision.target else ""
    # The decision target carries the dotted path; mind_id comes from
    # the profile context. The applier passes profile.mind_id via
    # _dispatch_with_rollback's closure; for the handler API parity we
    # accept that the applier surface knows the mind_id.
    # Resolve via applier._mind_yaml_path if explicit; else derive from
    # data_dir + an expected single-mind layout. Multi-mind awareness
    # lands when the wizard orchestrator passes mind_id explicitly.
    yaml_path = applier._mind_yaml_path
    if yaml_path is None:
        # Caller didn't provide an explicit mind.yaml; fail loudly so
        # ambiguous default-mind dispatches don't silently corrupt
        # the wrong profile.
        raise ApplyError(
            "MindConfig.voice handler needs an explicit mind_yaml_path; "
            "the applier was constructed without one. Pass "
            "``mind_yaml_path=<data_dir>/<mind_id>/mind.yaml`` to "
            "CalibrationApplier(...) before invoking apply().",
            decision=decision,
        )

    pre_value, post_dump = await asyncio.to_thread(
        _mutate_mind_yaml_voice_field,
        yaml_path,
        field_name,
        decision.value,
    )
    snapshot.mind_config_before[field_name] = pre_value
    logger.info(
        "voice.calibration.applier.mind_config_voice_applied",
        field=field_name,
        # value content is bounded (pydantic-validated); safe to surface.
        new_value=str(decision.value),
        # Mark whether this is a fresh field or an overwrite.
        had_prior_value=pre_value is not None,
    )
    _ = post_dump
    _ = mind_id  # currently unused; reserved for future per-mind reconciliation
    return (field_name, str(yaml_path), pre_value)


async def _revert_mind_config_voice(
    token: tuple[str, str, Any],
    snapshot: _PreApplySnapshot,
    applier: CalibrationApplier,
) -> None:
    """Restore one MindConfig.voice field to its pre-apply value."""
    field_name, yaml_path_str, pre_value = token
    from pathlib import Path  # noqa: PLC0415 -- local Path resolution

    yaml_path = Path(yaml_path_str)
    await asyncio.to_thread(
        _restore_mind_yaml_voice_field,
        yaml_path,
        field_name,
        pre_value,
    )
    snapshot.mind_config_before.pop(field_name, None)
    _ = applier  # reserved for future per-mind reconciliation
    logger.info(
        "voice.calibration.applier.mind_config_voice_reverted",
        field=field_name,
    )


def _mutate_mind_yaml_voice_field(
    yaml_path: Path,
    field_name: str,
    new_value: Any,  # noqa: ANN401 -- pydantic-validated MindConfig field value (str|int|float|bool)
) -> tuple[Any, dict[str, Any]]:  # noqa: ANN401 -- prior value matches new_value's domain
    """Read mind.yaml, mutate ``voice.<field_name>``, persist; return (pre_value, post_dump).

    Sync helper invoked inside :func:`asyncio.to_thread`. Atomicity:
    standard YAML rewrite (tmp + rename would be ideal; the existing
    dashboard pattern uses a direct write — we mirror it for parity).

    Raises :class:`ApplyError` on read/parse/write failure.
    """
    import yaml  # noqa: PLC0415 -- local pyyaml import

    if not yaml_path.is_file():
        raise ApplyError(
            f"mind.yaml not found at {yaml_path}; cannot apply MindConfig.voice mutation.",
            decision=None,  # type: ignore[arg-type]
        )
    try:
        text = yaml_path.read_text(encoding="utf-8")
        data: dict[str, Any] = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ApplyError(
            f"mind.yaml at {yaml_path} unreadable or malformed: {exc}",
            decision=None,  # type: ignore[arg-type]
        ) from exc
    voice_section = data.setdefault("voice", {})
    pre_value = voice_section.get(field_name)
    voice_section[field_name] = new_value
    try:
        yaml_path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ApplyError(
            f"failed to write mind.yaml at {yaml_path}: {exc}",
            decision=None,  # type: ignore[arg-type]
        ) from exc
    return (pre_value, data)


def _restore_mind_yaml_voice_field(
    yaml_path: Path,
    field_name: str,
    pre_value: Any,  # noqa: ANN401 -- mirrors _mutate_mind_yaml_voice_field's domain
) -> None:
    """Restore one ``voice.<field>`` to its pre-apply value (best-effort)."""
    import yaml  # noqa: PLC0415 -- local pyyaml import

    if not yaml_path.is_file():
        return
    try:
        text = yaml_path.read_text(encoding="utf-8")
        data: dict[str, Any] = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError):
        return
    voice_section = data.setdefault("voice", {})
    if pre_value is None:
        # Field didn't exist before; remove it on revert.
        voice_section.pop(field_name, None)
    else:
        voice_section[field_name] = pre_value
    try:
        yaml_path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError:
        return


register_target_class_pair(
    "MindConfig.voice",
    apply=_apply_mind_config_voice,
    revert=_revert_mind_config_voice,
)
