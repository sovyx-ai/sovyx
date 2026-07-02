"""Mission C1 §T1.4 — VAD-frontend reset ladder.

Disjoint from :class:`CaptureIntegrityCoordinator`'s bypass strategies:

* bypass strategies (``voice/health/bypass/_strategy.py`` + platform
  implementations) remediate APO / mixer / session faults at the OS
  layer — Windows Voice Clarity, PulseAudio ``module-echo-cancel``,
  CoreAudio Voice Isolation;
* this ladder remediates VAD frontend faults at the Sovyx processing
  layer — Silero LSTM state corruption, ONNX session-state fault,
  FrameNormalizer misalignment, AGC2 floor too low for VAD response.

Anti-pattern #39(a) — verdict-disjoint remediation lives in
verdict-disjoint modules.

Coordinator dispatches :attr:`IntegrityVerdict.VAD_FRONTEND_DEAD` to
:meth:`VADFrontendRecovery.run`. Each ladder step applies a recovery
action, re-probes the live capture stream, and returns a
:class:`BypassOutcome` carrying the per-step verdict. The first step
whose post-step probe returns :attr:`IntegrityVerdict.HEALTHY` resolves
the ladder; the coordinator does NOT latch terminal on ladder success
(pipeline is healthy again, future deaf heartbeats are welcome — see
:mod:`_bypass_coordinator_mixin` §20.M T1.6.b).

All five steps of the §4.4 ADR-D4 ladder ship and are wired, ordered
cheapest-to-most-invasive: L1 ``silero_reset``, L2
``silero_reinstantiate``, L3 ``normalizer_engage``, L4
``agc2_floor_lift``, L5 ``fallback_vad``. (v0.44.0 LENIENT shipped
L1 + L3 only; the remaining steps landed in later patches once their
underlying APIs did.) Per-step cost and semantics are documented on
:data:`_LADDER_STEPS`.

L6 (quarantine) is NOT a ladder step — it's the coordinator's terminal
fall-through after ladder exhaustion. See
:meth:`CaptureIntegrityCoordinator._quarantine_endpoint` with
``terminal_verdict=IntegrityVerdict.VAD_FRONTEND_DEAD``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics_bypass_coordinator import (
    record_vad_frontend_reset_outcome,
)
from sovyx.voice.health.contract import (
    BypassOutcome,
    BypassVerdict,
    IntegrityVerdict,
)

if TYPE_CHECKING:
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe
    from sovyx.voice.health.contract import (
        BypassContext,
        CaptureTaskProto,
        IntegrityResult,
    )


class _PipelineWithVADReset(Protocol):
    """Structural type for the subset of :class:`VoicePipeline` the
    ladder needs.

    :attr:`BypassContext.pipeline_ref` is typed :class:`Any` per the
    circular-import contract documented in
    :mod:`sovyx.voice.health.contract._bypass`; the ladder casts via
    this protocol so mypy strict can verify the call surface without
    importing :class:`VoicePipeline` (which would pull
    ``voice/pipeline/_orchestrator.py`` and break the health-layer
    leaf-import contract).
    """

    async def reset_vad(self) -> None: ...
    async def reinstantiate_vad(self) -> None: ...
    async def swap_vad(self, new_vad: object) -> None: ...


logger = get_logger(__name__)


_LADDER_STEPS: tuple[str, ...] = (
    "silero_reset",  # L1 — pipeline.reset_vad()
    "silero_reinstantiate",  # L2 — pipeline.reinstantiate_vad()
    "normalizer_engage",  # L3 — capture_task.engage_frame_normalizer()
    "agc2_floor_lift",  # L4 — capture_task.apply_agc2_floor_lift(delta_db)
    "fallback_vad",  # L5 — pipeline.swap_vad(FallbackEnergyVAD(...))
)
"""Ordered ladder step names — v0.44.x ships all 5 ladder steps.

The string labels match :func:`record_vad_frontend_reset_outcome`
step labels exactly. The ordering reflects cheapest-to-most-invasive:

* L1 ``silero_reset`` — < 1 ms, in-place LSTM zeroing.
* L2 ``silero_reinstantiate`` — ~50-200 ms, fresh ONNX session load.
* L3 ``normalizer_engage`` — < 5 ms, force capture stream re-open so
  ``FrameNormalizer`` rebuilds on the new source layout.
* L4 ``agc2_floor_lift`` — < 5 ms, bounded gain delta lift via
  ``vad_frontend_reset_max_gain_lift_db`` knob. Defends Silero's
  ``quiet_signal_gate`` invariant (§20.I).
* L5 ``fallback_vad`` — one-shot, swap Silero for the energy-based
  fallback for the remainder of the session. Operator-visible
  degradation: VAD accuracy drops to RMS-threshold semantics, but
  speech routing keeps working.

L6 (quarantine + runtime failover) is NOT a ladder step — it's the
coordinator's terminal fall-through when the entire ladder exhausts.
"""


class VADFrontendRecovery:
    """Mission C1 §T1.4 — recovery ladder for
    :attr:`IntegrityVerdict.VAD_FRONTEND_DEAD`.

    Runs each :data:`_LADDER_STEPS` entry sequentially. Between steps,
    the live capture stream is re-probed; the first step whose
    post-step probe returns HEALTHY resolves the ladder.

    Args:
        probe: The integrity probe used for per-step re-classification.
            Shared with the coordinator so probe state (its private VAD
            instance) is consistent across the ladder + the
            coordinator's own pre-/post-bypass probes.
        capture_task: The live :class:`CaptureTaskProto`. L3 calls
            :meth:`engage_frame_normalizer`; future L4 will read the
            recent RMS summary via :meth:`recent_rms_db_summary`.
        tuning: Frozen :class:`VoiceTuningConfig` snapshot — read for
            the master :attr:`vad_frontend_reset_enabled` kill switch
            + (future) per-step caps like
            :attr:`vad_frontend_reset_max_gain_lift_db`.
    """

    def __init__(
        self,
        *,
        probe: CaptureIntegrityProbe,
        capture_task: CaptureTaskProto,
        tuning: VoiceTuningConfig,
    ) -> None:
        self._probe = probe
        self._capture_task = capture_task
        self._tuning = tuning

    async def run(
        self,
        context: BypassContext,
        before: IntegrityResult,
    ) -> list[BypassOutcome]:
        """Run the ladder; return one :class:`BypassOutcome` per
        attempted step.

        Returns an empty list when
        :attr:`VoiceTuningConfig.vad_frontend_reset_enabled` is False
        (rollback knob per §10) OR when
        :attr:`BypassContext.pipeline_ref` is missing (legacy caller
        without §T1.4.a plumbing — surface as observability event so
        the wire-up gap is loud rather than silent).

        Returns at least one :class:`BypassOutcome` per step actually
        attempted. The first
        :attr:`BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY`
        terminates the ladder; subsequent steps are not run.
        """
        outcomes: list[BypassOutcome] = []
        if not self._tuning.vad_frontend_reset_enabled:
            logger.info(
                "voice.vad_frontend_reset.disabled",
                endpoint_guid=context.endpoint_guid,
                reason="vad_frontend_reset_enabled=False",
            )
            return outcomes
        pipeline = context.pipeline_ref
        if pipeline is None:
            logger.warning(
                "voice.vad_frontend_reset.missing_pipeline_ref",
                endpoint_guid=context.endpoint_guid,
                action_required=(
                    "Mission C1 §T1.4.a — BypassContext.pipeline_ref "
                    "must be populated by the factory before this "
                    "ladder can recover the LIVE pipeline VAD. Probe's "
                    "VAD is a separate instance (capture_integrity.py "
                    "cross-contamination guard) and cannot be mutated "
                    "from here."
                ),
            )
            return outcomes
        for idx, step_name in enumerate(_LADDER_STEPS):
            # Cast via the structural Protocol — see
            # :class:`_PipelineWithVADReset` docstring.
            pipeline_proto: _PipelineWithVADReset = pipeline
            outcome = await self._run_step(
                step_name=step_name,
                attempt_index=idx,
                pipeline=pipeline_proto,
                context=context,
                before=before,
            )
            outcomes.append(outcome)
            if outcome.verdict is BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY:
                logger.info(
                    "voice.vad_frontend_reset.recovered",
                    step=step_name,
                    endpoint_guid=context.endpoint_guid,
                    elapsed_ms=round(outcome.elapsed_ms, 1),
                )
                return outcomes
        logger.error(
            "voice.vad_frontend_reset.exhausted",
            endpoint_guid=context.endpoint_guid,
            steps_tried=len(outcomes),
        )
        return outcomes

    async def _run_step(
        self,
        *,
        step_name: str,
        attempt_index: int,
        pipeline: _PipelineWithVADReset,
        context: BypassContext,
        before: IntegrityResult,
    ) -> BypassOutcome:
        """Apply one ladder step + re-probe + classify."""
        t0 = time.monotonic()
        try:
            await self._apply_step(step_name=step_name, pipeline=pipeline)
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.exception(
                "voice.vad_frontend_reset.step_crashed",
                step=step_name,
                endpoint_guid=context.endpoint_guid,
            )
            record_vad_frontend_reset_outcome(
                step=step_name,
                success=False,
                elapsed_ms=elapsed_ms,
                reason=f"crashed:{type(exc).__name__}",
            )
            return BypassOutcome(
                strategy_name=f"vad_frontend_reset:{step_name}",
                attempt_index=attempt_index,
                verdict=BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD,
                integrity_before=before,
                integrity_after=None,
                elapsed_ms=elapsed_ms,
                detail=f"step_crashed:{type(exc).__name__}",
            )
        after = await self._probe.probe_warm(self._capture_task)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        success = after.verdict is IntegrityVerdict.HEALTHY
        record_vad_frontend_reset_outcome(
            step=step_name,
            success=success,
            elapsed_ms=elapsed_ms,
            reason="" if success else after.verdict.value,
        )
        verdict = (
            BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY
            if success
            else BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD
        )
        detail = step_name if success else f"post_probe_verdict={after.verdict.value}"
        return BypassOutcome(
            strategy_name=f"vad_frontend_reset:{step_name}",
            attempt_index=attempt_index,
            verdict=verdict,
            integrity_before=before,
            integrity_after=after,
            elapsed_ms=elapsed_ms,
            detail=detail,
        )

    async def _apply_step(self, *, step_name: str, pipeline: _PipelineWithVADReset) -> None:
        """Dispatch the step name to its concrete action.

        All 5 ladder steps are wired in v0.44.x. Adding a new step
        means adding both an entry to :data:`_LADDER_STEPS` AND a
        branch here — the ``else`` branch raises so silent omissions
        surface at the first execution rather than as a missing
        recovery action in production.
        """
        if step_name == "silero_reset":
            # L1 — cheapest. Zero LSTM + FSM scalars in place.
            await pipeline.reset_vad()
            return
        if step_name == "silero_reinstantiate":
            # L2 — discard ONNX session + build fresh from the same
            # model artefact. ~50-200 ms; offloaded to to_thread
            # inside ``reinstantiate_vad`` (anti-pattern #14).
            await pipeline.reinstantiate_vad()
            return
        if step_name == "normalizer_engage":
            # L3 — force capture stream re-open so FrameNormalizer
            # rebuilds on the renegotiated source layout.
            await self._capture_task.engage_frame_normalizer()
            return
        if step_name == "agc2_floor_lift":
            # L4 — bounded AGC2 floor lift via the §20.M T1.4.b knob.
            # The applied delta is the AGC2-side bounded value (cap
            # at ``max_gain_db``), surfaced as telemetry for the
            # Phase 3 calibration window.
            knob = self._tuning.vad_frontend_reset_max_gain_lift_db
            applied = self._capture_task.apply_agc2_floor_lift(knob)
            logger.info(
                "voice.vad_frontend_reset.l4_gain_lift_applied",
                requested_delta_db=knob,
                applied_delta_db=applied,
            )
            return
        if step_name == "fallback_vad":
            # L5 — last-resort. Swap Silero for the energy-based
            # fallback for the remainder of the session. The pipeline's
            # ``swap_vad`` does an atomic Python assignment +
            # defensive ``reset()`` on the fresh instance.
            from sovyx.voice._vad_fallback import FallbackEnergyVAD

            fallback = FallbackEnergyVAD()
            await pipeline.swap_vad(fallback)
            logger.warning(
                "voice.vad_frontend_reset.l5_fallback_engaged",
                action_required=(
                    "Silero replaced by energy-based fallback VAD for "
                    "the remainder of this session. VAD accuracy "
                    "degrades to RMS-threshold semantics. Restart the "
                    "daemon to recover Silero on the next boot."
                ),
            )
            return
        msg = f"unwired ladder step: {step_name!r}"
        raise RuntimeError(msg)


__all__ = ["VADFrontendRecovery"]
