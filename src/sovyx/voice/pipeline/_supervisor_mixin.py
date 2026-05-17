"""Voice pipeline supervisor mixin — soft-recovery governor surface.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 2 / Option D (Soft Recovery).

Provides :meth:`SupervisorMixin.request_soft_recovery` — a state-reset
recovery primitive that clears the deaf-detection latch + ladder-
exhausted flag + voice axis from :class:`EngineDegradedStore` + bumps
the governor's retry counter. Invoked by the heartbeat governor at
:mod:`sovyx.voice.pipeline._heartbeat_mixin` after N consecutive
deaf-warnings while both ``_coordinator_terminated`` and
``_failover_ladder_exhausted`` are True.

Design rationale (per mission spec §"Phase 2 design RESOLVED"):

* Option A/B/C (full pipeline restart via factory re-invocation) was
  rejected after reconnaissance revealed that ``create_voice_pipeline``
  returns a multi-service ``VoiceBundle`` requiring registry hot-swap
  across ≥5 components — substantial regression risk vs the
  operator-problem actually solved.
* The v0.43.1 operator-session's failure mode is NOT "pipeline
  objects are dead" but "state machine is LATCHED". Pipeline +
  ONNX sessions are alive; only ``_coordinator_terminated`` and
  ``_failover_ladder_exhausted`` flags are stuck.
* Soft recovery composes the EXISTING
  :meth:`VoicePipeline.reset_coordinator_after_failover` primitive
  (battle-tested since v0.30.x) with a clear of the ladder-exhausted
  flag + a clear of the voice axis in the store. Next heartbeat that
  detects deaf state runs through coordinator → failover ladder as
  if fresh.

Anti-pattern compliance:

* #5 — no registry mutation; soft recovery stays within the pipeline.
* #14 — no event-loop blocking; reset operations are constant-time.
* #32 — host-owned attributes declared in TYPE_CHECKING for mypy
  strict + MRO discipline.
* #34 — recovery is default-on (no kill-switch); the four governor
  tuning knobs gate the trigger conditions instead.
* #35 — every governor state field reads via ``getattr(..., default)``
  in the heartbeat mixin's consumer code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice.pipeline._config import VoicePipelineConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SoftRecoveryResult:
    """Outcome of a single :meth:`SupervisorMixin.request_soft_recovery`
    invocation. Returned to the governor for telemetry + retry-budget
    bookkeeping.

    Attributes:
        success: ``True`` when every reset step completed without
            raising. Failures are non-fatal — the governor still
            bumps the retry counter so a wedged pipeline does not
            thrash, but the operator-facing banner stays surfaced.
        elapsed_ms: Wall-clock duration of the recovery sequence.
        error_class: ``type(exc).__name__`` when any step raised;
            empty string on success. Per anti-pattern #8 / xdist-safe.
    """

    success: bool
    elapsed_ms: int
    error_class: str


class SupervisorMixin:
    """Soft-recovery surface mounted on :class:`VoicePipeline`.

    Mounted via multiple inheritance per the existing voice/pipeline/
    mixin pattern. The host (``VoicePipeline`` in
    ``_orchestrator.py``) owns the instance-state initialisation; this
    mixin owns the read+write logic on those fields PLUS the existing
    :meth:`VoicePipeline.reset_coordinator_after_failover` method
    that already lives on the host (declared in TYPE_CHECKING below
    so MRO falls through to the real implementation).
    """

    if TYPE_CHECKING:
        # Host-owned attributes / methods. Anti-pattern #32 case (b)
        # forward declarations — TYPE_CHECKING-only so MRO falls
        # through to the real implementations on sibling mixins.
        _config: VoicePipelineConfig
        _coordinator_terminated: bool
        _failover_ladder_exhausted: bool
        _deaf_warnings_consecutive: int
        _max_vad_prob_since_heartbeat: float
        _vad_frames_since_heartbeat: int
        _last_terminal_deaf_warn_monotonic: float

        def reset_coordinator_after_failover(self) -> None: ...

    async def request_soft_recovery(self, *, reason: str) -> SoftRecoveryResult:
        """Reset the deaf-detection + ladder-exhausted state so the
        pipeline re-arms a fresh failover cycle.

        Mission C4 §Phase 2 / Option D — soft state reset, NOT a
        clean restart. The pipeline + capture task + ONNX sessions
        are NEVER torn down; only the latched flags clear so the
        next heartbeat that detects deaf state runs through
        coordinator → failover ladder as if the prior exhaustion had
        not happened.

        Sequence:

        1. ``reset_coordinator_after_failover()`` — clears the four
           heartbeat-window counters owned by HeartbeatMixin
           (existing primitive, shipped Mission MISSION-voice-linux-
           silent-mic-remediation-2026-05-04 §Phase 2 T2.6).
        2. ``_failover_ladder_exhausted = False`` — releases C3's
           deaf-warning throttle gate AND signals the
           ``EngineDegradedStore`` voice-axis producer that a fresh
           ladder is allowed.
        3. ``_last_terminal_deaf_warn_monotonic = 0.0`` — resets C3's
           1/min throttle clock so the next deaf-warn (if any) fires
           promptly with ``coordinator_terminal=False``.
        4. ``EngineDegradedStore.clear_axis("voice")`` — operator-
           facing banner stops surfacing during the recovery attempt.
           If the recovery fails (new ladder ALSO exhausts), the
           store re-populates via the existing
           ``_runtime_failover.py`` wire shim and the banner returns.
        5. Emit ``voice.supervisor.soft_recovery_complete`` with
           reason + elapsed_ms + retry-budget context.

        Anti-pattern #14 compliance: every operation is constant-time
        and synchronous; ``async def`` is preserved for symmetry with
        the rest of the supervisor surface (Phase 2 may add async
        steps in future patches) AND to let the governor await the
        result without spawning a background task. No
        ``time.sleep``-equivalent inside; no I/O.

        Args:
            reason: Human-readable trigger (e.g. ``"auto_recovery_governor"``
                or ``"operator_manual"`` for a Phase 3 doctor command).

        Returns:
            :class:`SoftRecoveryResult` with success flag + elapsed_ms.
            Failures are non-fatal — the caller (governor) increments
            its retry counter regardless.
        """
        started_at = time.monotonic()
        try:
            self.reset_coordinator_after_failover()
            self._failover_ladder_exhausted = False
            self._last_terminal_deaf_warn_monotonic = 0.0

            # Clear voice axis from the cross-axis store. Best-effort
            # via a defensive import so store unavailability cannot
            # block the recovery path.
            try:
                from sovyx.engine._degraded_store import get_default_degraded_store

                get_default_degraded_store().clear_axis("voice")
            except Exception:  # noqa: BLE001 — observability only
                logger.debug(
                    "voice.supervisor.degraded_store_clear_failed",
                    reason=reason,
                )

            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "voice.supervisor.soft_recovery_complete",
                **{
                    "voice.reason": reason,
                    "voice.elapsed_ms": elapsed_ms,
                    "voice.mind_id": self._config.mind_id,
                },
            )
            return SoftRecoveryResult(
                success=True,
                elapsed_ms=elapsed_ms,
                error_class="",
            )
        except Exception as exc:  # noqa: BLE001 — supervisor must never raise
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.error(
                "voice.supervisor.soft_recovery_failed",
                **{
                    "voice.reason": reason,
                    "voice.elapsed_ms": elapsed_ms,
                    "voice.error_class": type(exc).__name__,
                    "voice.error": str(exc),
                    "voice.mind_id": getattr(
                        getattr(self, "_config", None),
                        "mind_id",
                        "unknown",
                    ),
                },
            )
            return SoftRecoveryResult(
                success=False,
                elapsed_ms=elapsed_ms,
                error_class=type(exc).__name__,
            )


__all__ = ["SoftRecoveryResult", "SupervisorMixin"]
