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

v0.44.0 LENIENT ships steps L1 + L3 of the §4.4 ADR-D4 ladder:

* **L1 ``silero_reset``** — clears the live :class:`VoicePipeline._vad`
  LSTM recurrent state via :meth:`VoicePipeline.reset_vad`. Cheapest
  step (< 1 ms); resolves the most common failure mode (LSTM state
  corruption after a long sustained-silence run).
* **L3 ``normalizer_engage``** — calls
  :meth:`AudioCaptureTask.engage_frame_normalizer` to force a stream
  re-open. Resolves frame-shape / source-rate divergence reaching the
  VAD.

L2 (re-instantiate ONNX session), L4 (AGC2 adaptive floor lift), L5
(fallback energy-based VAD) are DEFERRED to v0.44.x patches once their
underlying APIs land — see mission §4.4 table for the full spec. The
ordered :data:`_LADDER_STEPS` tuple extends in those patches; the
coordinator dispatch surface is forward-compatible.

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


logger = get_logger(__name__)


_LADDER_STEPS: tuple[str, ...] = ("silero_reset", "normalizer_engage")
"""Ordered ladder step names — v0.44.0 LENIENT ships L1 + L3 only.

Future v0.44.x patches APPEND deferred steps when their underlying
APIs land: ``"silero_reinstantiate"`` (L2), ``"agc2_floor_lift"`` (L4),
``"fallback_vad"`` (L5). The string labels match
:func:`record_vad_frontend_reset_outcome` step labels exactly.
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

        Two steps are wired in v0.44.0; the ``else`` branch protects
        against forward-compatible :data:`_LADDER_STEPS` additions
        that haven't been wired yet (e.g. ``"silero_reinstantiate"``
        v0.44.x patch in progress).
        """
        if step_name == "silero_reset":
            await pipeline.reset_vad()
            return
        if step_name == "normalizer_engage":
            await self._capture_task.engage_frame_normalizer()
            return
        msg = f"unwired ladder step: {step_name!r}"
        raise RuntimeError(msg)


__all__ = ["VADFrontendRecovery"]
