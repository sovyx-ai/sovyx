"""Bypass-coordinator delegation mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's :class:`CaptureIntegrityCoordinator` invocation
path: the sustained-deafness trigger that delegates to the platform
bypass cascade (Windows Voice Clarity / VocaEffectPack APO bypass +
sibling tier-N strategies). The coordinator owns terminal-state
resolution; this mixin owns:

* the per-frame guards that decide WHEN to invoke,
* the spawn pattern that schedules the coordinator off the hot path,
* the invocation-pending watchdog that guards against wedged
  callbacks (T1.14),
* the dedup observability events when re-validation rejects a
  spawned task (T1.23 lock + dedup pattern).

Pre-extraction this surface lived as 4 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — sixth strike of the Phase 5.F.19+ orchestrator
split.

Anti-pattern #32 contract: zero cross-mixin method calls. The
``HeartbeatMixin`` calls ``self._maybe_trigger_bypass_coordinator()``
which now lives on THIS mixin; both mixins share the
``VoicePipeline`` host so MRO resolves the real method even though
the heartbeat mixin's ``if TYPE_CHECKING:`` block only sees the
forward declaration. The dedup helper (``_record_coordinator_dedup``)
is also on this mixin so calls within the bypass methods stay local.

Constants moved with the methods: ``_COORDINATOR_PENDING_TIMEOUT_S``
(T1.14 watchdog deadline) — only used inside
``_reset_coordinator_pending_after_timeout``.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_auto_bypass_enabled: bool`` — master kill switch (tuning flag).
* ``_auto_bypass_threshold: int`` — consecutive-deaf-warning trigger
  threshold (tuning flag).
* ``_coordinator_terminated: bool`` — terminal-state latch; once set,
  short-circuit subsequent invocations until external reset (e.g.
  ``reset_coordinator_after_failover`` on the host).
* ``_coordinator_invocation_pending: bool`` — in-flight latch; cleared
  by the T1.23 outer-finally OR the T1.14 watchdog timeout.
* ``_coordinator_invocation_count: int`` — monotonic counter; the
  watchdog uses it to ignore stale invocations.
* ``_coordinator_dedup_count: int`` — observability counter for
  deduplicated spawned tasks (lock contention).
* ``_coordinator_lock: asyncio.Lock`` — serialises concurrent spawns.
* ``_on_deaf_signal: Callable[[], Awaitable[Sequence[BypassOutcome]]] | None``
  — the coordinator callback wired by the factory.
* ``_deaf_warnings_consecutive: int`` — heartbeat-window counter
  initialised + maintained by HeartbeatMixin; reset to 0 inside this
  mixin's lock when the coordinator fires (snapshot+reset pattern).
* ``_max_vad_prob_since_heartbeat: float`` — heartbeat-window stat
  read for log attribution only.
* ``_vad_frames_since_heartbeat: int`` — same.
* ``_voice_clarity_active: bool`` — Windows Voice Clarity APO flag for
  log attribution.
* ``_config.mind_id`` — read for log attribution.
* ``_state: VoicePipelineState`` — read for log attribution.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice._event_names import CaptureIntegrityEvent
from sovyx.voice.health.contract import BypassVerdict
from sovyx.voice.pipeline._capture_integrity_emit import emit_capture_integrity_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sovyx.voice.health.contract import BypassOutcome
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._state import VoicePipelineState

logger = get_logger(__name__)


_COORDINATOR_PENDING_TIMEOUT_S = _VoiceTuning().pipeline_coordinator_pending_timeout_seconds
"""T1.14 watchdog deadline — see
``VoiceTuningConfig.pipeline_coordinator_pending_timeout_seconds``."""


# Mission C1 §20.M T1.6.b — verdict-classified terminal-latch predicate.
# Defined module-level so tests can call it without instantiating
# :class:`BypassCoordinatorMixin` and so the classification is single-
# source-of-truth.
_NON_TERMINAL_VERDICTS: frozenset[BypassVerdict] = frozenset(
    {
        # Ladder success — pipeline is healthy again, future heartbeats welcome.
        BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY,
        # Coordinator dispatch requests — factory consumer handles them;
        # next deaf heartbeat re-evaluates whether the request fixed it.
        BypassVerdict.CASCADE_REEVALUATION_REQUESTED,
        BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED,
    },
)
"""Verdicts that, when present in ANY outcome of a coordinator
invocation's return list, mark the outcome set as NON-terminal — the
mixin must NOT latch ``_coordinator_terminated`` and must NOT emit
``voice_apo_bypass_ineffective``. Every other verdict is terminal under
the pre-mission semantics (any non-empty outcome ⇒ latch) — see
:func:`_is_terminal_outcome_set` for the inverted predicate."""


def _is_terminal_outcome_set(outcomes: Sequence[BypassOutcome]) -> bool:
    """Return ``True`` iff the outcome set warrants latching
    :attr:`_coordinator_terminated`.

    Preserves the pre-mission semantics (``len(outcomes) > 0 → terminal``)
    EXCEPT when the outcome set contains at least one Mission C1
    non-terminal verdict:

    * :attr:`BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY` — the
      reset ladder recovered the live stream; pipeline is healthy again
      and future deaf heartbeats are welcome.
    * :attr:`BypassVerdict.CASCADE_REEVALUATION_REQUESTED` and
      :attr:`BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED` — the
      coordinator dispatched a downstream request; the factory consumer
      handles it and the next deaf heartbeat re-evaluates whether the
      handler resolved the underlying fault.

    All OTHER verdicts (APPLIED_HEALTHY / APPLIED_STILL_DEAD /
    FAILED_TO_APPLY / REVERTED / NOT_APPLICABLE /
    VAD_FRONTEND_RESET_APPLIED_STILL_DEAD) reach this predicate only
    after strategy iteration OR ladder run — both of which conclude
    with the coordinator either succeeding (APPLIED_HEALTHY) or
    quarantining the endpoint (every other case). The mixin's terminal
    latch is appropriate in both subcases.

    Pre-mission this method was the implicit ``len(outcomes) > 0`` test
    at line 315 — which silenced future heartbeats forever on any
    non-empty outcome including benign dispatch requests. See §20.E.
    """
    if not outcomes:
        return False
    return not any(o.verdict in _NON_TERMINAL_VERDICTS for o in outcomes)


class BypassCoordinatorMixin:
    """CaptureIntegrityCoordinator invocation delegation.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the trigger-guard + spawn + dedup
    + watchdog lifecycle.

    See module docstring for the full responsibility carve-out.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes. Declared
        # TYPE_CHECKING so mypy strict resolves the references without
        # creating runtime attributes that would interfere with the
        # host's own initialisation order.
        _auto_bypass_enabled: bool
        _auto_bypass_threshold: int
        _coordinator_terminated: bool
        _coordinator_invocation_pending: bool
        _coordinator_invocation_count: int
        _coordinator_dedup_count: int
        _coordinator_lock: asyncio.Lock
        _on_deaf_signal: Callable[[], Awaitable[Sequence[BypassOutcome]]] | None
        _deaf_warnings_consecutive: int
        _max_vad_prob_since_heartbeat: float
        _vad_frames_since_heartbeat: int
        _voice_clarity_active: bool
        _config: VoicePipelineConfig
        _state: VoicePipelineState

    def _maybe_trigger_bypass_coordinator(self) -> None:
        """Delegate sustained deafness to the :class:`CaptureIntegrityCoordinator`.

        Called from the heartbeat path after a deaf warning is logged.
        The orchestrator no longer tracks its own one-shot bypass latch —
        the coordinator owns terminal-state resolution via
        :attr:`~sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator.is_resolved`
        and returns an empty outcome list once resolved. We still apply
        three guards locally:

        * ``auto_bypass_enabled`` — master kill switch (tuning flag).
        * ``not _coordinator_terminated`` — once the coordinator has
          reported a terminal outcome (APPLIED_HEALTHY or exhausted)
          don't re-invoke it; the watchdog recheck loop handles recovery.
        * ``_deaf_warnings_consecutive >= _auto_bypass_threshold`` —
          require multiple back-to-back deaf heartbeats so a single
          transient (e.g. device switch) doesn't spin up the full
          strategy iteration.

        Unlike the previous implementation we no longer gate on
        ``voice_clarity_active``: the integrity probe itself classifies
        the signal OS-agnostically, so the coordinator is the
        authoritative gate. ``voice_clarity_active`` survives as a
        logging attribute for dashboard attribution.
        """
        if not self._auto_bypass_enabled:
            return
        if self._coordinator_terminated:
            return
        if self._coordinator_invocation_pending:
            return
        if self._deaf_warnings_consecutive < self._auto_bypass_threshold:
            return
        if self._on_deaf_signal is None:
            return

        self._coordinator_invocation_pending = True
        # T1.14 — bump the invocation counter BEFORE the spawn so the
        # watchdog captures the same count the spawned task observes.
        self._coordinator_invocation_count += 1
        captured_invocation_count = self._coordinator_invocation_count
        logger.warning(
            "voice.deaf.detected",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.state": self._state.name,
                "voice.consecutive_deaf_warnings": self._deaf_warnings_consecutive,
                "voice.threshold": self._auto_bypass_threshold,
                "voice.max_vad_probability": round(self._max_vad_prob_since_heartbeat, 3),
                "voice.frames_processed": self._vad_frames_since_heartbeat,
                "voice.voice_clarity_active": self._voice_clarity_active,
            },
        )
        # Schedule the coordinator on the running loop — this helper
        # runs on the per-frame hot path and must not block.
        spawn(self._invoke_deaf_signal(), name="voice-pipeline-deaf-signal")
        # T1.14 watchdog — if the spawned coordinator wedges (callback
        # in a sync OS call wrapped in to_thread, etc.) the T1.23
        # outer-finally never runs and the pending flag stays True
        # forever, locking out subsequent deaf-signal handling. The
        # watchdog force-clears the flag at
        # ``_COORDINATOR_PENDING_TIMEOUT_S`` if it's still set AND
        # belongs to THIS invocation (counter guard).
        spawn(
            self._reset_coordinator_pending_after_timeout(captured_invocation_count),
            name="voice-pipeline-coord-pending-watchdog",
        )

    async def _invoke_deaf_signal(self) -> None:
        """Invoke the deaf-signal callback and surface its outcomes (O2).

        The entire flow runs under :attr:`_coordinator_lock` so concurrent
        spawns (defensive against future code paths that might introduce
        an ``await`` into the sync trigger) can't double-invoke the
        callback. Three guards re-validate inside the lock because the
        spawn-to-acquire window can be arbitrarily long under load:

        * ``callback is None`` — trapped before lock acquisition (cheap,
          and avoids needlessly contending the lock).
        * ``_coordinator_terminated`` — a concurrent task may have
          latched terminal between spawn and lock acquisition; emit a
          deduplicated event so dashboards can attribute the no-op.
        * ``_deaf_warnings_consecutive < threshold`` — a healthy
          heartbeat between spawn and acquire may have reset the
          counter; the original trigger condition no longer holds, so
          treat as deduplicated.

        On entry to the callback section we **snapshot** the consecutive-
        deaf counter and **reset it to zero** before the ``await``. This
        eliminates the pre-O2 tight-retry loop: previously, deaf
        heartbeats firing during ``await callback()`` accumulated into
        ``_deaf_warnings_consecutive`` so an empty-outcomes return would
        immediately re-cross the threshold on the next heartbeat,
        causing back-to-back coordinator invocations. With the in-lock
        reset, each invocation starts a fresh accumulation window.

        The callback returns the coordinator's
        :class:`~sovyx.voice.health.contract.BypassOutcome` log. Empty
        means the coordinator short-circuited (already resolved this
        session or false alarm) — nothing to emit. A non-empty list is
        terminal: we flip :attr:`_coordinator_terminated` (still inside
        the lock for atomicity) so subsequent deaf warnings short-
        circuit at the sync trigger.

        Telemetry contract:

        * ``voice.deaf.coordinator_invocation_deduplicated`` (INFO) —
          re-validation rejected the spawned task. ``voice.reason``
          attribute distinguishes the cause.
        * ``voice_apo_bypass_activated`` on the APPLIED_HEALTHY outcome
          (strategy_name, attempt_index, reason carry the winning
          mutation path — ``"exclusive_engaged"`` for the Phase 1 Windows
          strategy, future values for new platforms).
        * ``voice_apo_bypass_ineffective`` when the coordinator
          exhausted every eligible strategy without recovery. The
          coordinator quarantines the endpoint via
          :class:`EndpointQuarantine` so the factory fails over to
          another capture device on next boot.
        """
        # T1.23 — outer try/finally wraps the entire body so the
        # pending flag clears on EVERY exit path, including cancellation
        # between spawn and lock acquisition. Pre-T1.23 the flag was
        # cleared at four scattered sites (callback-None early return,
        # terminated dedup, threshold dedup, callback-exception
        # finally inside the lock); a CancelledError landing on
        # ``async with self._coordinator_lock`` (or anywhere outside the
        # inner try block) would leak the flag and lock out every
        # subsequent deaf-signal trigger via the
        # ``_coordinator_invocation_pending`` guard at
        # :meth:`_maybe_invoke_deaf_signal` line 1904. Single outer
        # finally is the canonical "always reset" pattern.
        try:
            callback = self._on_deaf_signal
            if callback is None:
                return

            async with self._coordinator_lock:
                # Re-validate guards under the lock — between spawn and
                # acquisition the world may have changed.
                if self._coordinator_terminated:
                    self._record_coordinator_dedup("terminated_by_concurrent_task")
                    return
                if self._deaf_warnings_consecutive < self._auto_bypass_threshold:
                    self._record_coordinator_dedup("threshold_no_longer_met")
                    return

                # Snapshot + reset — see method docstring for the tight-retry
                # rationale. The snapshot is what we report in telemetry so
                # the recovery_attempted event reflects the counter that
                # justified this specific invocation, not a value mutated
                # mid-flight by concurrent heartbeats.
                invocation_counter_snapshot = self._deaf_warnings_consecutive
                self._deaf_warnings_consecutive = 0

                logger.warning(
                    "voice.deaf.recovery_attempted",
                    **{
                        "voice.mind_id": self._config.mind_id,
                        "voice.consecutive_deaf_warnings": invocation_counter_snapshot,
                        "voice.threshold": self._auto_bypass_threshold,
                        "voice.voice_clarity_active": self._voice_clarity_active,
                        "voice.auto_bypass_enabled": self._auto_bypass_enabled,
                    },
                )
                try:
                    outcomes = await callback()
                except Exception as exc:  # noqa: BLE001 — callback is user-supplied; shield the pipeline
                    # Mission H2 §T2.1 — dual-emit via wrapper. Each
                    # wrapper call emits the neutral event AND its legacy
                    # twin per ADR-D14 (callable-exception path emits both
                    # ``voice.capture_integrity.bypass_failed`` /
                    # ``voice_apo_bypass_failed`` AND
                    # ``voice.capture_integrity.bypassed`` /
                    # ``audio.apo.bypassed`` verdict=failure).
                    emit_capture_integrity_event(
                        CaptureIntegrityEvent.BYPASS_FAILED,
                        "error",
                        mind_id=self._config.mind_id,
                        strategies=[],
                        voice_clarity_active=self._voice_clarity_active,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    emit_capture_integrity_event(
                        CaptureIntegrityEvent.BYPASSED,
                        "error",
                        mind_id=self._config.mind_id,
                        strategies=[],
                        voice_clarity_active=self._voice_clarity_active,
                        verdict="failure",
                        attempts=0,
                        outcomes=[],
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    return

                if not outcomes:
                    # Coordinator short-circuited (false-alarm probe or prior
                    # resolution). Don't burn the terminal flag — we may still
                    # want to retry if deafness persists after a transient
                    # clear. The counter snapshot+reset above already broke
                    # the pre-O2 tight-retry pattern.
                    return

                # Mission C1 §20.M T1.6.b — verdict-classified terminal
                # latch. Pre-mission ANY non-empty outcome set latched
                # ``_coordinator_terminated``, which would (a) silence
                # all future deaf heartbeats forever, and (b) — combined
                # with the line 355 ``applied_healthy is None`` branch —
                # emit ``voice_apo_bypass_ineffective`` on benign
                # coordinator dispatch outcomes (CASCADE_REEVALUATION_
                # REQUESTED, NORMALIZER_ENGAGEMENT_REQUESTED), which are
                # NOT failures and should not latch the mixin.
                #
                # Terminal verdicts (latch on):
                # * APPLIED_HEALTHY (legacy strategy success)
                # * ALL outcomes NOT_APPLICABLE (T6.15 unrecoverable)
                # * Ladder exhausted (every outcome is APPLIED_STILL_DEAD
                #   or VAD_FRONTEND_RESET_APPLIED_STILL_DEAD — no recovery
                #   step worked, coordinator quarantined the endpoint)
                #
                # Non-terminal (do NOT latch):
                # * CASCADE_REEVALUATION_REQUESTED (dispatch — driver
                #   silent; failover IS the cascade re-walk, but the
                #   pipeline itself may recover when failover binds a
                #   surrogate, so future heartbeats are welcome)
                # * NORMALIZER_ENGAGEMENT_REQUESTED (dispatch — stream
                #   re-open in progress; next deaf heartbeat re-evaluates)
                # * VAD_FRONTEND_RESET_APPLIED_HEALTHY (ladder success —
                #   pipeline healthy again, future heartbeats welcome)
                self._coordinator_terminated = _is_terminal_outcome_set(outcomes)
                applied_healthy = next(
                    (o for o in outcomes if o.verdict is BypassVerdict.APPLIED_HEALTHY),
                    None,
                )
                if applied_healthy is not None:
                    # Mission H2 §T2.1 — dual-emit via wrapper. Strategies
                    # list drives the ``voice.bypass_family`` resolution
                    # (e.g. ``alsa_capture_chain`` on Linux, ``voice_clarity``
                    # on Windows). Wraps legacy ``voice_apo_bypass_activated``
                    # + ``audio.apo.bypassed`` verdict=success.
                    strategies_list = [o.strategy_name for o in outcomes]
                    emit_capture_integrity_event(
                        CaptureIntegrityEvent.BYPASS_ACTIVATED,
                        "warning",
                        mind_id=self._config.mind_id,
                        strategies=strategies_list,
                        voice_clarity_active=self._voice_clarity_active,
                        strategy_name=applied_healthy.strategy_name,
                        attempt_index=applied_healthy.attempt_index,
                        reason=applied_healthy.detail,
                        consecutive_deaf_warnings=invocation_counter_snapshot,
                        threshold=self._auto_bypass_threshold,
                        action="capture_integrity_coordinator",
                    )
                    emit_capture_integrity_event(
                        CaptureIntegrityEvent.BYPASSED,
                        "warning",
                        mind_id=self._config.mind_id,
                        strategies=strategies_list,
                        voice_clarity_active=self._voice_clarity_active,
                        verdict="success",
                        strategy_name=applied_healthy.strategy_name,
                        attempt_index=applied_healthy.attempt_index,
                        attempts=len(outcomes),
                        outcomes=[o.verdict.value for o in outcomes],
                        reason=applied_healthy.detail,
                        consecutive_deaf_warnings=invocation_counter_snapshot,
                        threshold=self._auto_bypass_threshold,
                    )
                    return

                # Mission C1 §20.M T1.6.b — VAD-frontend ladder success
                # is a distinct success path from the legacy
                # APPLIED_HEALTHY: pipeline is healthy again but the
                # coordinator did NOT latch terminated (per
                # :func:`_is_terminal_outcome_set`) so future deaf
                # heartbeats remain welcome. Emit a distinct event so
                # dashboards can attribute Sovyx-side recovery (Silero
                # reset / normalizer engage) separately from OS-side
                # bypass success (Voice Clarity disable etc.).
                ladder_healthy = next(
                    (
                        o
                        for o in outcomes
                        if o.verdict is BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY
                    ),
                    None,
                )
                if ladder_healthy is not None:
                    logger.warning(
                        "voice_vad_frontend_reset_activated",
                        mind_id=self._config.mind_id,
                        step=ladder_healthy.strategy_name,
                        attempt_index=ladder_healthy.attempt_index,
                        reason=ladder_healthy.detail,
                        consecutive_deaf_warnings=invocation_counter_snapshot,
                        threshold=self._auto_bypass_threshold,
                        action="vad_frontend_reset_ladder",
                        coordinator_terminated=self._coordinator_terminated,
                    )
                    return

                # Mission C1 §20.M T1.6.b — non-terminal coordinator
                # dispatch outcomes (CASCADE_REEVALUATION_REQUESTED,
                # NORMALIZER_ENGAGEMENT_REQUESTED) are REQUESTS to the
                # factory consumer, NOT failures. Emit an info-level
                # observability event so dashboards see the dispatch
                # without surfacing it as a bypass failure. Pre-mission
                # this branch fell through to ``voice_apo_bypass_ineffective``
                # + ``audio.apo.bypassed verdict=failure`` — the EXACT
                # wrong dashboard signal for a benign coordinator
                # request (see §20.E).
                if not self._coordinator_terminated:
                    logger.info(
                        "voice.coordinator.dispatch_acknowledged",
                        mind_id=self._config.mind_id,
                        verdicts=[o.verdict.value for o in outcomes],
                        consecutive_deaf_warnings=invocation_counter_snapshot,
                        threshold=self._auto_bypass_threshold,
                    )
                    return

                # Every strategy either failed to apply or applied-but-still-dead.
                # The coordinator has already quarantined the endpoint; surface a
                # single operator-facing event so the dashboard / doctor can
                # switch their messaging to "auto-fix could not recover — see
                # manual remediation steps".
                #
                # Mission H2 §T2.1 — dual-emit via wrapper. Strategies list
                # drives the ``voice.bypass_family`` resolution; on Linux
                # this resolves to ``alsa_capture_chain`` /
                # ``pipewire_filter_chain`` etc., on Windows to
                # ``voice_clarity``. Operators triaging the new neutral
                # ``voice.capture_integrity.bypass_ineffective`` event see
                # the correct platform-family token instead of the
                # platform-misleading ``apo`` substring.
                strategies_list = [o.strategy_name for o in outcomes]
                emit_capture_integrity_event(
                    CaptureIntegrityEvent.BYPASS_INEFFECTIVE,
                    "error",
                    mind_id=self._config.mind_id,
                    strategies=strategies_list,
                    voice_clarity_active=self._voice_clarity_active,
                    attempts=len(outcomes),
                    verdicts=[o.verdict.value for o in outcomes],
                    hint=(
                        "CaptureIntegrityCoordinator exhausted every eligible "
                        "bypass strategy. Endpoint quarantined for apo_quarantine_s. "
                        "When tuning.runtime_failover_on_quarantine_enabled=True "
                        "(default v0.31.0+ per Mission "
                        "MISSION-voice-linux-silent-mic-remediation-2026-05-04 "
                        "§Phase 2 T2.6), the deaf-signal closure dispatches "
                        "request_device_change_restart to the next non-quarantined "
                        "boot candidate; until then, the factory will fail over "
                        "only on next boot. Lenient telemetry "
                        "voice.failover.attempted is emitted regardless of the "
                        "gate so dashboards can validate the rollout. Likely "
                        "causes: firmware-level DSP on the mic, a virtual audio "
                        "cable with a fixed format, a damaged capture element, "
                        "or an APO not yet covered by a platform strategy. "
                        "Manual remediation: disable enhancements in the OS sound "
                        "settings, fix the ALSA mixer state (Linux), or switch "
                        "capture device."
                    ),
                )
                # "partial" verdict: at least one strategy applied cleanly but
                # the post-apply re-probe still classified the signal as dead;
                # otherwise every strategy either failed-to-apply or was not
                # applicable, which is a flat failure.
                any_applied = any(o.verdict is BypassVerdict.APPLIED_STILL_DEAD for o in outcomes)
                bypass_verdict = "partial" if any_applied else "failure"
                emit_capture_integrity_event(
                    CaptureIntegrityEvent.BYPASSED,
                    "error",
                    mind_id=self._config.mind_id,
                    strategies=strategies_list,
                    voice_clarity_active=self._voice_clarity_active,
                    verdict=bypass_verdict,
                    attempts=len(outcomes),
                    outcomes=[o.verdict.value for o in outcomes],
                    quarantined=True,
                )
        finally:
            # T1.23 — reset the pending flag on every exit path. The
            # outer try wraps the entire body (callback-None early return,
            # the lock acquisition, the re-validate guards, the snapshot
            # + reset, the inner callback try/except, and the outcomes
            # processing) so a CancelledError landing anywhere — including
            # while waiting on ``self._coordinator_lock`` — clears the
            # flag instead of leaking it. Pre-T1.23 a leaked flag locked
            # out every subsequent deaf-signal trigger via the
            # ``_coordinator_invocation_pending`` short-circuit at
            # :meth:`_maybe_invoke_deaf_signal`.
            self._coordinator_invocation_pending = False

    async def _reset_coordinator_pending_after_timeout(
        self, captured_invocation_count: int
    ) -> None:
        """T1.14 watchdog — clear ``_coordinator_invocation_pending``
        if a wedged ``_invoke_deaf_signal`` task never reaches its
        T1.23 outer-finally.

        T1.23 wraps ``_invoke_deaf_signal`` in an outer try/finally
        that clears the pending flag on every exit path including
        cancellation. That covers wedged callbacks the asyncio
        runtime CAN cancel (e.g. ``await asyncio.sleep(...)`` inside
        the callback). It does NOT cover wedges where the callback
        is in a synchronous OS call wrapped in
        ``asyncio.to_thread(...)`` and that OS call doesn't honour
        thread cancellation — the awaiting asyncio task can be
        cancelled but the worker thread keeps running, and if the
        cancel happens to be eaten somewhere upstream, the flag
        stays True. Net effect: deaf-signal handling locked out
        until process restart.

        This watchdog is the safety net. ``_maybe_invoke_deaf_signal``
        spawns it alongside the coordinator task with the current
        invocation count captured. After
        :data:`_COORDINATOR_PENDING_TIMEOUT_S` (30 s default), the
        watchdog wakes and:

          * If ``self._coordinator_invocation_count !=
            captured_invocation_count``, a SUBSEQUENT invocation
            has fired since this watchdog was spawned. The current
            flag belongs to that newer invocation, NOT to ours;
            no-op (the newer invocation has its own watchdog).
          * If the count matches AND
            ``self._coordinator_invocation_pending`` is still True,
            the original invocation IS wedged. Force-clear the flag
            and emit ``voice.coordinator.pending_flag_timeout_reset``
            so dashboards can attribute the unlock.
          * If the count matches AND the flag is False, the original
            invocation completed cleanly (T1.23 outer-finally
            cleared it). No-op.

        Cancellation: if the watchdog is itself cancelled (loop
        teardown, etc.), suppress and return. The next deaf-signal
        trigger spawns a fresh watchdog.
        """
        try:
            await asyncio.sleep(_COORDINATOR_PENDING_TIMEOUT_S)
        except asyncio.CancelledError:
            return

        if self._coordinator_invocation_count != captured_invocation_count:
            # A newer invocation owns the live flag — leave it alone.
            return
        if not self._coordinator_invocation_pending:
            # The original invocation completed cleanly via T1.23
            # outer-finally; nothing to do.
            return

        # Wedged invocation. Force-clear the flag and emit the
        # structured signal.
        logger.warning(
            "voice.coordinator.pending_flag_timeout_reset",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.timeout_seconds": _COORDINATOR_PENDING_TIMEOUT_S,
                "voice.invocation_count": captured_invocation_count,
                "voice.action_required": (
                    "Coordinator invocation wedged for "
                    f"{_COORDINATOR_PENDING_TIMEOUT_S} s; force-clearing "
                    "the pending flag so subsequent deaf-signal triggers "
                    "can fire. The wedged task may still be running in a "
                    "worker thread (asyncio cannot force-stop OS threads). "
                    "Investigate via `sovyx doctor voice` and the deaf-"
                    "signal callback's logs (typical cause: a sync OS "
                    "call in the callback that doesn't honour "
                    "asyncio.to_thread cancellation)."
                ),
            },
        )
        self._coordinator_invocation_pending = False

    def _record_coordinator_dedup(self, reason: str) -> None:
        """Bump the dedup counter and emit the structured observability event.

        Called from inside the lock when re-validation rejects a spawned
        ``_invoke_deaf_signal`` task. ``reason`` is one of:

        * ``"terminated_by_concurrent_task"`` — terminal latch was set
          by another coordinator invocation while we were waiting for
          the lock.
        * ``"threshold_no_longer_met"`` — a healthy heartbeat reset the
          consecutive-deaf counter between spawn and lock acquisition.

        Non-zero ``_coordinator_dedup_count`` over a release window
        validates that the lock is doing real work — i.e. the
        defense-in-depth pattern is justified even when the asyncio
        single-threaded model would technically suffice. Surface this
        on the dashboard "Voice Health" panel.
        """
        self._coordinator_dedup_count += 1
        logger.info(
            "voice.deaf.coordinator_invocation_deduplicated",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.reason": reason,
                "voice.dedup_count": self._coordinator_dedup_count,
                "voice.consecutive_deaf_warnings": self._deaf_warnings_consecutive,
                "voice.threshold": self._auto_bypass_threshold,
                "voice.coordinator_terminated": self._coordinator_terminated,
            },
        )
