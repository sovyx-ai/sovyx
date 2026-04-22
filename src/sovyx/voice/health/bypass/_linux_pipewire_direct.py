"""Linux — bypass the session manager by reopening against the ALSA hw device.

Secondary remedy for Linux capture-side DSP that destroys the signal
before it reaches user space. Unlike the mixer-reset strategy (which
leaves the session manager in the path but resets its pre-ADC gain),
this strategy teardown-and-reopens the capture stream targeting the
``ALSA`` host API directly — bypassing PipeWire / PulseAudio /
JACK entirely.

Use cases covered:

* User-installed WirePlumber filter-chain that loops the capture
  source through ``module-echo-cancel`` or a stale ``rnnoise`` plugin.
* Distro-shipped APO-analogue session-manager policy that inserts an
  EQ / limiter on every capture source ("privacy-enhancing" distros).
* Session-manager service in a degraded state where restarting it
  clean would require the user's intervention.

The strategy is **opt-in** (default-off via
:attr:`VoiceTuningConfig.linux_pipewire_direct_bypass_enabled`) because
the kernel ALSA device is single-client on most codecs — engaging the
bypass steals the device from every other desktop app holding an
open PulseAudio/PipeWire source. The user's mic is blocked on those
apps until the strategy reverts or sovyx releases the capture.

Thin orchestrator over
:meth:`sovyx.voice._capture_task.AudioCaptureTask.request_alsa_hw_direct_restart`
(apply) + :meth:`request_session_manager_restart` (revert). The
capture task owns the enumeration + opener fallback; the strategy
only sequences the verdict classification.

See ``docs-internal/plans/linux-alsa-mixer-saturation-fix.md`` §2.4
for the derivation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.logging import get_logger
from sovyx.voice._capture_task import (
    AlsaHwDirectRestartVerdict,
    SessionManagerRestartVerdict,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


_STRATEGY_NAME = "linux.pipewire_direct"
"""Coordinator-visible strategy identifier — treat as external API."""


# Eligibility-reason tokens.
_REASON_NOT_LINUX = "not_linux_platform"
_REASON_DISABLED_BY_TUNING = "pipewire_direct_bypass_disabled_by_tuning"
_REASON_NOT_SESSION_MANAGER = "endpoint_not_served_by_session_manager"


# Apply-reason tokens for :class:`BypassApplyError.reason`.
_APPLY_REASON_NO_ALSA_SIBLING = "alsa_hw_direct_no_sibling"
_APPLY_REASON_DOWNGRADED = "alsa_hw_direct_downgraded_to_session_manager"
_APPLY_REASON_OPEN_FAILED = "alsa_hw_direct_open_failed_no_stream"
_APPLY_REASON_NOT_RUNNING = "capture_task_not_running"
_APPLY_REASON_NOT_LINUX = "not_linux_platform"


# Conservative cost hint (ms). The real cost is dominated by the
# PortAudio re-open which runs the sibling chain; a healthy ALSA
# direct open lands in 100–300 ms on laptops, 600 ms covers the
# session-manager fallback path when ALSA itself rejects the combo.
_APPLY_COST_MS = 600


class LinuxPipeWireDirectBypass:
    """Reopen the capture stream against the ALSA-direct sibling device.

    Eligibility:
        * :attr:`BypassContext.platform_key == "linux"`.
        * :attr:`VoiceTuningConfig.linux_pipewire_direct_bypass_enabled`
          is ``True`` (default-off — user must opt in via
          ``SOVYX_TUNING__VOICE__LINUX_PIPEWIRE_DIRECT_BYPASS_ENABLED=true``
          or the dashboard toggle).
        * :attr:`BypassContext.host_api_name` is one of the known
          session-manager host APIs (``PulseAudio``, ``PipeWire``,
          ``JACK``). When the current stream is *already* on
          ``ALSA``, this strategy has nothing to do — the session
          manager is not in the path — and reports
          ``endpoint_not_served_by_session_manager``.

    Apply:
        Delegates to
        :meth:`AudioCaptureTask.request_alsa_hw_direct_restart`.
        Treats every verdict other than
        :attr:`AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED` as a
        :class:`BypassApplyError` so the coordinator advances.

    Revert:
        Delegates to
        :meth:`AudioCaptureTask.request_session_manager_restart`. A
        non-``SESSION_MANAGER_ENGAGED`` verdict is logged at WARNING
        but does not raise — the coordinator is already in teardown,
        and the watchdog / reconnect loop will pick up a broken
        stream on the next loop iteration.
    """

    name: str = _STRATEGY_NAME

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        if context.platform_key != "linux":
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_LINUX,
                estimated_cost_ms=0,
            )
        tuning = VoiceTuningConfig()
        if not tuning.linux_pipewire_direct_bypass_enabled:
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )
        host_api = context.host_api_name or ""
        if host_api not in {"PulseAudio", "PipeWire", "JACK"}:
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_SESSION_MANAGER,
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
        result = await context.capture_task.request_alsa_hw_direct_restart()
        if result.verdict is AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED:
            logger.info(
                "bypass_strategy_apply_ok",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                host_api=result.host_api,
                sample_rate=result.sample_rate,
            )
            return "alsa_hw_engaged"

        reason = {
            AlsaHwDirectRestartVerdict.NO_ALSA_SIBLING: _APPLY_REASON_NO_ALSA_SIBLING,
            AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER: _APPLY_REASON_DOWNGRADED,
            AlsaHwDirectRestartVerdict.OPEN_FAILED_NO_STREAM: _APPLY_REASON_OPEN_FAILED,
            AlsaHwDirectRestartVerdict.NOT_RUNNING: _APPLY_REASON_NOT_RUNNING,
            AlsaHwDirectRestartVerdict.NOT_LINUX: _APPLY_REASON_NOT_LINUX,
        }[result.verdict]
        detail = result.detail or f"alsa_hw_direct restart verdict={result.verdict.value}"
        raise BypassApplyError(detail, reason=reason)

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        result = await context.capture_task.request_session_manager_restart()
        if result.verdict is SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED:
            logger.info(
                "bypass_strategy_revert_ok",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                host_api=result.host_api,
                sample_rate=result.sample_rate,
            )
            return
        logger.warning(
            "bypass_strategy_revert_failed",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            verdict=result.verdict.value,
            detail=result.detail,
        )


__all__ = ["LinuxPipeWireDirectBypass"]
