"""Windows — bypass the WASAPI shared-mode APO chain via exclusive mode.

This is the primary remedy for the Windows Voice Clarity / VocaEffectPack
APO cluster: when a capture endpoint's shared-mode stream is being
destroyed by the platform-owned DSP chain (VAD goes dead while audible
RMS remains healthy), re-opening in WASAPI exclusive mode bypasses the
APO chain entirely — the ``IAudioClient`` talks to the kernel driver
directly, with no user-mode processing pipeline in between.

The strategy is a thin orchestrator over
:meth:`sovyx.voice._capture_task.AudioCaptureTask.request_exclusive_restart`
(mutation) + :meth:`request_shared_restart` (revert). The heavy lifting
— exclusive-mode negotiation, shared-mode fallback detection, verdict
classification — lives in the capture task.

See ``docs-internal/plans/voice-apo-os-agnostic-fix.md`` §4.1 for the
full derivation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice._capture_task import (
    ExclusiveRestartVerdict,
    SharedRestartVerdict,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError, BypassRevertError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


# Coordinator-visible strategy name — stable external API; changing it
# breaks dashboard filters and the per-strategy metric counter attributes.
_STRATEGY_NAME = "win.wasapi_exclusive"

# Tokens for the ``Eligibility.reason`` field. Listed here so other
# packages can key on them without string-literal drift.
_REASON_NOT_WIN32 = "not_win32_platform"
_REASON_NOT_WASAPI = "not_wasapi_endpoint"

# Conservative cost hint (ms). The real cost is dominated by PortAudio's
# exclusive-mode negotiation on the audio thread — typically 200–400 ms
# on modern Windows + Razer/Realtek hardware; 500 ms is a safe upper
# bound used purely for telemetry (the coordinator never sequences on
# cost).
_APPLY_COST_MS = 500


class WindowsWASAPIExclusiveBypass:
    """Exclusive-mode reopen strategy for Windows capture endpoints.

    Eligibility:
        * ``platform_key == "win32"``
        * ``host_api_name`` looks like a WASAPI endpoint (substring
          match on ``"WASAPI"``, case-insensitive, so both the bare
          ``"WASAPI"`` label and PortAudio's ``"Windows WASAPI"`` pass).

    Apply:
        Delegates to
        :meth:`AudioCaptureTask.request_exclusive_restart`. Treats the
        following verdicts as :class:`BypassApplyError`:

        * ``DOWNGRADED_TO_SHARED`` — WASAPI silently granted shared
          mode (device held by another exclusive app, or policy denied
          exclusive access). The APO chain is still in the path.
        * ``OPEN_FAILED_SHARED_FALLBACK`` — exclusive failed and the
          shared fallback recovered. APO chain still in the path.
        * ``OPEN_FAILED_NO_STREAM`` — both paths failed; pipeline has
          no capture source. Coordinator moves on; watchdog may pick
          up the re-enable later.
        * ``NOT_RUNNING`` — capture task stopped between eligibility
          probe and apply (race). Coordinator treats this as FAILED.

    Revert:
        Delegates to :meth:`AudioCaptureTask.request_shared_restart`
        with its own verdict set. A failed revert is logged at WARNING
        but does not raise — the coordinator is already in teardown
        and the reconnect loop will recover eventually.
    """

    name: str = _STRATEGY_NAME

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        if context.platform_key != "win32":
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_WIN32,
                estimated_cost_ms=0,
            )
        host_api = (context.host_api_name or "").upper()
        if "WASAPI" not in host_api:
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_WASAPI,
                estimated_cost_ms=0,
            )
        return Eligibility(
            applicable=True,
            reason="",
            estimated_cost_ms=_APPLY_COST_MS,
        )

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        logger.info(
            "bypass_strategy_apply_begin",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            endpoint_name=context.endpoint_friendly_name,
            host_api=context.host_api_name,
        )
        result = await context.capture_task.request_exclusive_restart()
        if result.verdict is ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED:
            logger.info(
                "bypass_strategy_apply_ok",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                host_api=result.host_api,
                sample_rate=result.sample_rate,
            )
            return "exclusive_engaged"

        # Every non-engagement verdict is a failure for this strategy —
        # the coordinator advances to the next candidate (e.g.
        # ``win.disable_sysfx`` in Phase 3).
        shared_fallback_reason = "exclusive_open_failed_shared_fallback"
        reason = {
            ExclusiveRestartVerdict.DOWNGRADED_TO_SHARED: "exclusive_downgraded_to_shared",
            ExclusiveRestartVerdict.OPEN_FAILED_SHARED_FALLBACK: shared_fallback_reason,
            ExclusiveRestartVerdict.OPEN_FAILED_NO_STREAM: "exclusive_open_failed_no_stream",
            ExclusiveRestartVerdict.NOT_RUNNING: "capture_task_not_running",
        }[result.verdict]
        detail = result.detail or f"exclusive restart verdict={result.verdict.value}"
        raise BypassApplyError(detail, reason=reason)

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        result = await context.capture_task.request_shared_restart()
        if result.verdict is SharedRestartVerdict.SHARED_ENGAGED:
            logger.info(
                "bypass_strategy_revert_ok",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                host_api=result.host_api,
                sample_rate=result.sample_rate,
            )
            return

        # B3 (atomic revert): the pre-B3 contract logged a WARNING and
        # silently returned, leaving the capture pipeline in an opaque
        # half-applied state and burying the failure in stream-of-
        # consciousness logs. Raise BypassRevertError with a stable
        # reason so the coordinator emits the structured
        # ``voice.bypass.revert_failed`` event and dashboards can
        # attribute the failure to (strategy_name, reason).
        reason = f"shared_restart_{result.verdict.value}"
        detail = result.detail or f"shared restart verdict={result.verdict.value}"
        raise BypassRevertError(detail, reason=reason)


__all__ = ["WindowsWASAPIExclusiveBypass"]
