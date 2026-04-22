"""Linux — escape a hardware hw:X,Y endpoint to a session-manager virtual.

Complementary inverse of
:class:`~sovyx.voice.health.bypass._linux_pipewire_direct.LinuxPipeWireDirectBypass`.
While that strategy goes *from* a session-manager-backed capture
*toward* a raw ALSA ``hw:X,Y`` device (typical cure for APO-degraded
filter chains), this strategy goes the opposite direction — from a
pinned ``hw:X,Y`` capture *toward* the ``pipewire`` / ``pulse`` /
``default`` virtual PCM.

Why both directions exist:

* The original ``LinuxPipeWireDirectBypass`` targets the pathology
  where PipeWire's ``filter-chain`` inserts noise-suppression / AEC
  modules that destroy the VAD input signal. Moving to hw: is the
  cure.
* This strategy targets the opposite pathology (``VLX-003`` /
  ``VLX-004``): the user's pinned ``hw:X,Y`` node is **held** by
  a session manager at runtime — PortAudio returns ``-9985 Device
  unavailable`` on every combo because another desktop app
  captured the device. Moving to the session-manager virtual is
  the cure because ``pipewire`` / ``pulse`` PCMs are always shared.

This bypass is **runtime-only** — the cascade-candidate-set in
:func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`
already resolves the same class of failure at boot by including the
session-manager virtuals as candidates. The strategy here covers the
dynamic case where the capture task was already running healthy on
``hw:X,Y`` and a later event (user opens Zoom, Bluetooth handset
connects) grabs the hardware.

Thin orchestrator over
:meth:`sovyx.voice._capture_task.AudioCaptureTask.request_session_manager_restart`
with ``target_device`` pinned to the preferred virtual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice._capture_task import SessionManagerRestartVerdict
from sovyx.voice.device_enum import DeviceKind
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


_STRATEGY_NAME = "linux.session_manager_escape"
"""Coordinator-visible strategy identifier — treat as external API."""


# Eligibility-reason tokens.
_REASON_NOT_LINUX = "not_linux_platform"
_REASON_NOT_HARDWARE = "endpoint_not_hardware_source"
_REASON_NO_TARGET = "no_session_manager_target_available"


# Apply-reason tokens for :class:`BypassApplyError.reason`.
_APPLY_REASON_NO_TARGET = "session_manager_escape_no_target"
_APPLY_REASON_DOWNGRADED = "session_manager_escape_downgraded_to_alsa_hw"
_APPLY_REASON_OPEN_FAILED = "session_manager_escape_open_failed_no_stream"
_APPLY_REASON_NOT_RUNNING = "capture_task_not_running"
_APPLY_REASON_NOT_LINUX = "not_linux_platform"


# Conservative cost hint (ms). The open path is the PortAudio device
# switch + first callback; a healthy pipewire virtual reopen lands
# in 80–200 ms on a laptop with PipeWire already running.
_APPLY_COST_MS = 400


def _find_preferred_session_manager_target(
    context: BypassContext,
) -> DeviceEntry | None:
    """Return the best session-manager virtual / OS default target.

    Preference: ``pipewire`` → ``pulse`` / ``pulseaudio`` → ``default``
    → any input with ``kind == SESSION_MANAGER_VIRTUAL``. Returns
    ``None`` when no suitable target is present (device is a bare
    ``hw:X,Y`` with no session manager on the host — rare in 2026).
    """
    from sovyx.voice.device_enum import enumerate_devices

    try:
        devices = enumerate_devices()
    except Exception:  # noqa: BLE001 — detector must never break strategy
        logger.debug("session_manager_escape_enumeration_failed", exc_info=True)
        return None

    current_key = (context.current_device_index, context.host_api_name)

    def _same_as_current(entry: DeviceEntry) -> bool:
        return (entry.index, entry.host_api_name) == current_key

    def _input_only(entry: DeviceEntry) -> bool:
        return entry.max_input_channels > 0

    # Pass 1 — explicit "pipewire" PCM.
    for entry in devices:
        if _same_as_current(entry) or not _input_only(entry):
            continue
        if entry.canonical_name.startswith("pipewire"):
            return entry

    # Pass 2 — PulseAudio-family virtuals.
    for entry in devices:
        if _same_as_current(entry) or not _input_only(entry):
            continue
        if entry.canonical_name.startswith("pulse"):
            return entry

    # Pass 3 — OS-default alias.
    for entry in devices:
        if _same_as_current(entry) or not _input_only(entry):
            continue
        if entry.kind == DeviceKind.OS_DEFAULT:
            return entry

    # Pass 4 — any session-manager-virtual input.
    for entry in devices:
        if _same_as_current(entry) or not _input_only(entry):
            continue
        if entry.kind == DeviceKind.SESSION_MANAGER_VIRTUAL:
            return entry

    return None


class LinuxSessionManagerEscapeBypass:
    """Move a HARDWARE-sourced capture to a session-manager virtual PCM.

    Eligibility:
        * :attr:`BypassContext.platform_key == "linux"`.
        * :attr:`BypassContext.current_device_kind` is
          :attr:`~sovyx.voice.device_enum.DeviceKind.HARDWARE`. A
          device already on a session-manager virtual has nothing to
          escape *to* — the PipeWire-direct strategy covers the
          inverse direction.
        * At least one session-manager-virtual or OS-default input
          device is enumerated on the host.

    Apply:
        Resolves the preferred target via
        :func:`_find_preferred_session_manager_target`, then delegates
        to
        :meth:`AudioCaptureTask.request_session_manager_restart`
        with ``target_device=target``. Any verdict other than
        :attr:`SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED`
        is raised as :class:`BypassApplyError` so the coordinator
        advances to the next strategy.

    Revert:
        No-op by design. The preferred boot-time cure is the
        cascade-candidate-set picking ``hw:X,Y`` again on the next
        boot when the session manager releases the device. Runtime
        revert would require re-running the candidate cascade while
        live — out of scope for this strategy.
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
        if context.current_device_kind != DeviceKind.HARDWARE.value:
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_HARDWARE,
                estimated_cost_ms=0,
            )
        target = _find_preferred_session_manager_target(context)
        if target is None:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_TARGET,
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
        target = _find_preferred_session_manager_target(context)
        if target is None:
            # Eligibility was positive when the coordinator last
            # checked, but a hot-unplug between eligibility and
            # apply removed every session-manager virtual. Surface
            # as a BypassApplyError so the coordinator advances.
            raise BypassApplyError(
                "no session-manager target found at apply time "
                "(hot-unplug between probe and apply)",
                reason=_APPLY_REASON_NO_TARGET,
            )
        logger.info(
            "bypass_strategy_apply_begin",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            endpoint_name=context.endpoint_friendly_name,
            host_api=context.host_api_name,
            target_device_index=target.index,
            target_host_api=target.host_api_name,
            target_name=target.name,
            target_kind=str(target.kind),
        )
        result = await context.capture_task.request_session_manager_restart(
            target_device=target,
        )
        if result.verdict is SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED:
            logger.info(
                "bypass_strategy_apply_ok",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                host_api=result.host_api,
                sample_rate=result.sample_rate,
            )
            return "session_manager_engaged"

        reason = {
            SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW: _APPLY_REASON_DOWNGRADED,
            SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM: _APPLY_REASON_OPEN_FAILED,
            SessionManagerRestartVerdict.NOT_RUNNING: _APPLY_REASON_NOT_RUNNING,
            SessionManagerRestartVerdict.NOT_LINUX: _APPLY_REASON_NOT_LINUX,
        }.get(result.verdict, f"session_manager_escape_verdict_{result.verdict.value}")
        detail = result.detail or f"session_manager restart verdict={result.verdict.value}"
        raise BypassApplyError(detail, reason=reason)

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        # No-op — see class docstring. The cascade-candidate-set
        # handles fall-back-to-hw: on the next boot naturally.
        logger.debug(
            "bypass_strategy_revert_noop",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
        )


__all__ = ["LinuxSessionManagerEscapeBypass"]
