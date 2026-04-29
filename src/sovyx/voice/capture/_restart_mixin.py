"""Restart-strategy methods — :class:`RestartMixin`.

Extracted from ``voice/_capture_task.py`` per master mission Phase 1
/ T1.4 step 8. Companion to ``capture/_restart.py`` which carries the
verdict / result types this mixin returns. Splitting types and
behaviour across two files keeps each under the CLAUDE.md anti-
pattern #16 ceiling (~500 LOC mixed-responsibility) — types own
shape, mixin owns transactional restart semantics.

Step 8a landed the **Windows pair**:

* :meth:`_reopen_stream_after_device_error` — generic reconnect
  helper used by the consumer loop on ``sd.PortAudioError`` AND by
  :meth:`request_exclusive_restart` as its shared-mode fallback.
* :meth:`request_exclusive_restart` — APO-bypass reopen in WASAPI
  exclusive mode. v0.20.2 / Bug C — the result distinguishes a real
  exclusive engagement from a downgraded shared-mode reopen.
* :meth:`request_shared_restart` — symmetric revert to shared mode.

Step 8b (this commit) lands the **Linux pair** + supporting
sibling-discovery helpers:

* :meth:`request_alsa_hw_direct_restart` — bypass the PipeWire /
  PulseAudio session manager by reopening the kernel ALSA sibling
  directly. Twin of ``request_exclusive_restart`` for Linux.
* :meth:`request_session_manager_restart` — symmetric revert to
  the PipeWire/PulseAudio session manager. Also serves the
  ``LinuxSessionManagerEscapeBypass`` apply path with an explicit
  ``target_device``.
* :meth:`_find_sibling_with_host_api` /
  :meth:`_find_sibling_with_host_api_in` — canonical-name sibling
  lookup helpers shared by the Linux pair.

Mixin contract — the host class (``AudioCaptureTask``)
initialises the stream-state attributes in ``__init__``. Method
calls back to the host class (``self._close_stream``,
``self._emit_stream_opened``, ``self._signal_consumer_shutdown``,
``self._audio_callback``, ``self._allocate_ring_buffer``) resolve
via MRO; the mixin doesn't own those — they live on
:class:`AudioCaptureTask` (or other mixins) and are reachable via
the composed instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice._agc2 import build_agc2_if_enabled
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.capture._helpers import _resolve_input_entry
from sovyx.voice.capture._restart import (
    _LINUX_ALSA_HOST_API,
    _LINUX_SESSION_MANAGER_HOST_APIS,
    AlsaHwDirectRestartResult,
    AlsaHwDirectRestartVerdict,
    ExclusiveRestartResult,
    ExclusiveRestartVerdict,
    HostApiRotateResult,
    HostApiRotateVerdict,
    SessionManagerRestartResult,
    SessionManagerRestartVerdict,
    SharedRestartResult,
    SharedRestartVerdict,
    _emit_alsa_hw_direct_restart_metric,
    _emit_exclusive_restart_metric,
    _emit_host_api_rotate_metric,
    _emit_session_manager_restart_metric,
    _emit_shared_restart_metric,
)
from sovyx.voice.pipeline._frame_types import (
    CaptureRestartFrame,
    CaptureRestartReason,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._aec import AecProcessor, RenderPcmProvider
    from sovyx.voice._double_talk_detector import DoubleTalkDetector
    from sovyx.voice._noise_suppression import NoiseSuppressor
    from sovyx.voice._snr_estimator import SnrEstimator
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.pipeline._orchestrator import VoicePipeline


logger = get_logger(__name__)


__all__ = ["RestartMixin"]


class RestartMixin:
    """Restart-strategy methods sharing AudioCaptureTask state.

    Windows pair (step 8a): exclusive ↔ shared mode toggle for the
    Voice Clarity / VocaEffectPack APO-bypass strategy. Each
    ``request_*_restart`` returns a structured
    :class:`ExclusiveRestartResult` / :class:`SharedRestartResult`
    so the bypass coordinator can distinguish "engaged" from
    "downgraded" without parsing logs.

    Linux pair (step 8b): ALSA-hw-direct ↔ session-manager toggle
    for the PipeWire/PulseAudio bypass strategies. Same engaged-
    vs-downgraded distinction in
    :class:`AlsaHwDirectRestartResult` /
    :class:`SessionManagerRestartResult`.
    """

    # Host-class state declarations for mypy strict. The host class
    # (``AudioCaptureTask``) sets these in ``__init__``; the mixin
    # only reads / writes via ``self``.
    _running: bool
    _tuning: VoiceTuningConfig | None
    _input_device: int | str | None
    _enumerate_fn: Callable[[], list[DeviceEntry]] | None
    _host_api_name: str | None
    _queue: asyncio.Queue[Any]
    _stream: Any
    _sample_rate: int
    _blocksize: int
    _sd_module: ModuleType | None
    _normalizer: FrameNormalizer | None
    _aec: AecProcessor | None
    _render_provider: RenderPcmProvider | None
    _double_talk_detector: DoubleTalkDetector | None
    _noise_suppressor: NoiseSuppressor | None
    _snr_estimator: SnrEstimator | None
    _dither_enabled: bool
    _dither_amplitude_lsb: float
    _resolved_device_name: str | None
    _pipeline: VoicePipeline

    # Method-via-MRO declarations — these live on AudioCaptureTask
    # (or future LoopMixin) and resolve through the composed
    # instance. The annotations document the contract; mypy strict
    # accepts the call without complaint.
    def _close_stream(self, reason: str = "unknown") -> None: ...
    def _emit_stream_opened(
        self,
        info: Any,  # noqa: ANN401 — StreamInfo dataclass, typed lazily
        *,
        apo_bypass_attempted: bool,
    ) -> None: ...
    def _signal_consumer_shutdown(self) -> None: ...
    def _audio_callback(
        self,
        indata: npt.NDArray[np.int16],
        frames: int,
        time_info: object,
        status: object,
    ) -> None: ...
    def _allocate_ring_buffer(self, tuning: VoiceTuningConfig) -> None: ...

    async def request_exclusive_restart(self) -> ExclusiveRestartResult:
        """Re-open the capture stream in WASAPI exclusive mode.

        Called by the orchestrator when it decides that a capture-side
        APO (Windows Voice Clarity / VocaEffectPack) is destroying the
        microphone signal — exclusive mode bypasses the entire APO chain
        by talking to the IAudioClient directly. The current stream is
        torn down first; on failure the method logs and returns without
        raising so a single heartbeat loop iteration does not crash the
        pipeline.

        Idempotent — safe to call while stopped; in that case it is a
        no-op. The orchestrator already latches the request so the
        callback fires at most once per session.

        Returns:
            An :class:`ExclusiveRestartResult` describing whether
            exclusive mode was actually engaged. v0.20.2 / Bug C —
            pre-v0.20.2 this method returned ``None`` and logged
            success whenever the reopen succeeded, even when WASAPI
            fell back to shared mode (APO still in the signal path).
            Callers now inspect ``result.engaged`` to distinguish a
            real APO bypass from a cosmetic restart.
        """
        if not self._running:
            logger.debug("audio_capture_exclusive_restart_skipped_not_running")
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_exclusive_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        base_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        exclusive_tuning = base_tuning.model_copy(update={"capture_wasapi_exclusive": True})
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        # T32 — snapshot pre-restart substrate for the
        # CaptureRestartFrame emission. Capturing here (before the
        # close + reopen sequence) means old_* fields reflect the
        # substrate that was about to fail / be replaced; the new_*
        # fields are filled in below from the StreamInfo returned by
        # the opener. CLAUDE.md anti-pattern #29 — frame is
        # observability layer, NOT state-machine; the authoritative
        # substrate state still lives in the AudioCaptureTask
        # attributes that are mutated below.
        old_host_api = self._host_api_name or ""
        old_device_id = self._resolved_device_name or str(self._input_device or "")
        logger.warning(
            "audio_capture_exclusive_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
        )

        # Tear down the existing stream on the PortAudio thread before
        # we try to grab the device exclusively — otherwise WASAPI
        # returns AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED on our own stream.
        await asyncio.to_thread(self._close_stream, "exclusive_restart")
        # Clear any residual frames from the shared-mode callback.
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=exclusive_tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
                # T29 — preserve cascade-aligned host_api on
                # restart. ``self._host_api_name`` carries either
                # the cascade winner (initial start) or the
                # post-restart host_api (subsequent calls).
                preferred_host_api=self._host_api_name or None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_exclusive_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Fall back to shared mode so the pipeline keeps running
            # (deaf, but alive — the dashboard banner will still guide
            # the user through the manual APO-disable path).
            try:
                await self._reopen_stream_after_device_error()
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "audio_capture_exclusive_fallback_failed",
                    error=str(fallback_exc),
                )
                # Stream is gone and no recovery path inside the task
                # can resurrect it — unblock the consume loop so the
                # supervisor sees a completed consumer and rebuilds.
                self._signal_consumer_shutdown()
                result = ExclusiveRestartResult(
                    verdict=ExclusiveRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    host_api=self._host_api_name,
                    device=self._input_device,
                    detail=(
                        f"exclusive open failed ({exc}); shared fallback "
                        f"also failed ({fallback_exc})"
                    ),
                )
                _emit_exclusive_restart_metric(result)
                return result
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.OPEN_FAILED_SHARED_FALLBACK,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(f"exclusive open failed ({exc}); recovered into shared mode"),
            )
            _emit_exclusive_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # T32 — emit CaptureRestartFrame BEFORE the ring epoch
        # increment so the dashboard's restart-history timeline
        # receives the substrate transition AT the actual moment of
        # change. APO_DEGRADED + bypass_tier=3 (WASAPI exclusive is
        # the Tier 3 strategy in the bypass coordinator's pyramid).
        # ``new_signal_processing_mode`` carries the WASAPI mode
        # ("exclusive" if the opener honoured the request, "shared"
        # otherwise — the v0.20.2 / Bug C downgrade case is detected
        # below and the frame value matches reality even when the
        # request was rejected).
        new_mode = "exclusive" if info.exclusive_used else "shared"
        self._pipeline.record_capture_restart(
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=time.monotonic(),
                restart_reason=CaptureRestartReason.APO_DEGRADED.value,
                old_host_api=old_host_api,
                new_host_api=self._host_api_name or "",
                old_device_id=old_device_id,
                new_device_id=self._resolved_device_name or str(self._input_device or ""),
                old_signal_processing_mode="shared",
                new_signal_processing_mode=new_mode,
                bypass_tier=3,
            )
        )
        # Reset the ring buffer so the bypass coordinator's post-apply
        # integrity probe only sees frames from the reopened stream.
        self._allocate_ring_buffer(exclusive_tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=True)
        # v0.20.2 / Bug C — an opener that couldn't honour exclusive
        # (device busy, policy denied, old PortAudio) falls through to
        # shared variants of the same combo and returns a stream with
        # ``exclusive_used=False``. The pipeline is alive but the APO
        # chain is still in the signal path — the deaf condition that
        # triggered the request is unchanged. Distinguish this from a
        # real engagement so the dashboard / orchestrator / user know
        # the bypass did not take.
        if not info.exclusive_used:
            logger.error(
                "audio_capture_exclusive_restart_downgraded_to_shared",
                device=self._input_device,
                host_api=self._host_api_name,
                sample_rate=self._sample_rate,
                channels=info.channels,
            )
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.DOWNGRADED_TO_SHARED,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "WASAPI granted shared mode instead of exclusive — APO "
                    "chain still in signal path. Another app may hold the "
                    "device exclusively or Windows policy denied exclusive "
                    "access."
                ),
            )
            _emit_exclusive_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_exclusive_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
            exclusive_used=info.exclusive_used,
        )
        result = ExclusiveRestartResult(
            verdict=ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_exclusive_restart_metric(result)
        return result

    async def _reopen_stream_after_device_error(self) -> None:
        """Reopen the stream after a ``sd.PortAudioError`` in the consume loop.

        Uses the same unified opener as :meth:`start` so reconnect after
        a USB-headset yank inherits host-API × auto_convert × channels
        fallback automatically.
        """
        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,  # reconnect path skips validation
                # T29 — preserve cascade alignment after device-error
                # reconnect. Without this the opener's sibling chain
                # would re-pick PortAudio enumeration order on the
                # USB-yank reconnect path, drifting away from the
                # cascade winner.
                preferred_host_api=self._host_api_name or None,
            )
        except StreamOpenError as exc:
            raise RuntimeError(str(exc)) from exc
        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # Reset the ring buffer — stale frames from the pre-error stream
        # would mislead any integrity probe issued immediately after the
        # reconnect.
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)

    async def request_shared_restart(self) -> SharedRestartResult:
        """Revert the capture stream to shared mode.

        Symmetric twin of :meth:`request_exclusive_restart` — re-opens
        the device with ``capture_wasapi_exclusive=False`` so a failed
        APO-bypass experiment (or an explicit user unpin) restores the
        pre-bypass state. Used by
        :class:`sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`
        when a strategy evaluated STILL_DEAD or when a later strategy
        superseded an earlier one.

        Idempotent — safe to call while stopped; in that case it is a
        no-op. All metric + log semantics mirror the exclusive path so
        dashboards can correlate engagements and reverts one-to-one.

        Returns:
            A :class:`SharedRestartResult` describing the outcome. A
            non-``SHARED_ENGAGED`` verdict means the pipeline has no
            active capture until the next reconnect cycle or explicit
            restart.
        """
        if not self._running:
            logger.debug("audio_capture_shared_restart_skipped_not_running")
            result = SharedRestartResult(
                verdict=SharedRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_shared_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        base_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        shared_tuning = base_tuning.model_copy(update={"capture_wasapi_exclusive": False})
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        # T32 — snapshot pre-restart substrate. The revert path
        # ("shared restart") is operator-initiated (the bypass
        # coordinator's revert hook OR an explicit dashboard unpin),
        # so the frame's ``restart_reason`` is MANUAL and
        # ``bypass_tier`` resets to 0. The previous ``new_*``
        # substrate of the matching ``request_exclusive_restart``
        # frame is THIS frame's ``old_*`` — the timeline forms a
        # coherent transition pair on the dashboard.
        old_host_api = self._host_api_name or ""
        old_device_id = self._resolved_device_name or str(self._input_device or "")
        logger.warning(
            "audio_capture_shared_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
        )

        # Mirror request_exclusive_restart — tear down the existing
        # stream on the PortAudio thread so the shared reopen does not
        # race against our own exclusive handle.
        await asyncio.to_thread(self._close_stream, "shared_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=shared_tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
                # T29 — symmetric to request_exclusive_restart.
                preferred_host_api=self._host_api_name or None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_shared_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Stream is gone and no recovery path inside the task can
            # resurrect it (no callback → no frames → no PortAudioError
            # → consume loop parked on queue.get). Unblock the loop so
            # the supervisor sees a completed consumer and rebuilds.
            self._signal_consumer_shutdown()
            result = SharedRestartResult(
                verdict=SharedRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                detail=f"shared reopen failed: {exc}",
            )
            _emit_shared_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # T32 — emit CaptureRestartFrame for the revert pair. MANUAL
        # reason because the shared restart is always initiated by an
        # external policy decision (operator unpin, coordinator
        # revert) — never an automatic bypass. ``bypass_tier=0``
        # signals to the dashboard that no tier is currently active.
        self._pipeline.record_capture_restart(
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=time.monotonic(),
                restart_reason=CaptureRestartReason.MANUAL.value,
                old_host_api=old_host_api,
                new_host_api=self._host_api_name or "",
                old_device_id=old_device_id,
                new_device_id=self._resolved_device_name or str(self._input_device or ""),
                old_signal_processing_mode="exclusive",
                new_signal_processing_mode="shared",
                bypass_tier=0,
            )
        )
        self._allocate_ring_buffer(shared_tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)
        logger.warning(
            "audio_capture_shared_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = SharedRestartResult(
            verdict=SharedRestartVerdict.SHARED_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_shared_restart_metric(result)
        return result

    async def request_alsa_hw_direct_restart(self) -> AlsaHwDirectRestartResult:
        """Reopen the capture stream against the ALSA-direct sibling device.

        Linux-specific twin of :meth:`request_exclusive_restart`. The
        ``LinuxPipeWireDirectBypass`` strategy invokes this when it
        wants to bypass a misbehaving PipeWire/PulseAudio filter chain
        (e.g. ``module-echo-cancel``, ``rnnoise`` filter, user-added
        EQ) and talk to the kernel ALSA device directly.

        Resolution: re-enumerate input devices, locate the sibling whose
        :attr:`DeviceEntry.canonical_name` matches the current endpoint
        AND whose :attr:`DeviceEntry.host_api_name` equals ``"ALSA"``.
        When found, that entry is handed to the unified opener as the
        starting point — the opener's sibling-chain fallback then
        automatically covers the "ALSA open refused, fall back to
        PulseAudio" path.

        Idempotent — safe to call while stopped or on a non-Linux host;
        in either case it is a no-op and the existing stream (if any)
        is preserved.

        Returns:
            An :class:`AlsaHwDirectRestartResult`. Callers inspect
            ``result.engaged`` (``True`` iff the ALSA host API actually
            won the fallback pyramid) to know whether the PipeWire
            bypass is in effect.
        """
        if not self._running:
            logger.debug("audio_capture_alsa_hw_direct_restart_skipped_not_running")
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result
        if sys.platform != "linux":
            logger.debug(
                "audio_capture_alsa_hw_direct_restart_skipped_not_linux",
                platform=sys.platform,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NOT_LINUX,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=f"request_alsa_hw_direct_restart is Linux-only; running on {sys.platform}",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        alsa_entry = self._find_sibling_with_host_api(_LINUX_ALSA_HOST_API)
        if alsa_entry is None:
            logger.warning(
                "audio_capture_alsa_hw_direct_restart_no_sibling",
                device=self._input_device,
                host_api=self._host_api_name,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NO_ALSA_SIBLING,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "no ALSA-host-API sibling found for current endpoint "
                    "(PortAudio build without ALSA, or device held exclusive)"
                ),
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        # T32 — snapshot pre-restart substrate (session-manager-backed
        # endpoint) before the close + ALSA-direct reopen sequence. The
        # frame's old_signal_processing_mode is a Linux-specific label
        # (``"session_manager"``) describing the PipeWire/PulseAudio
        # filter chain that's about to be bypassed.
        old_host_api = self._host_api_name or ""
        old_device_id = self._resolved_device_name or str(self._input_device or "")
        logger.warning(
            "audio_capture_alsa_hw_direct_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
            target_host_api=_LINUX_ALSA_HOST_API,
            target_device_index=alsa_entry.index,
        )

        # Tear down the existing (session-manager-backed) stream before
        # we grab the kernel device — some ALSA drivers reject a second
        # client even for read-only capture.
        await asyncio.to_thread(self._close_stream, "alsa_hw_direct_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=alsa_entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
                # T29 — explicit ALSA preference for the
                # PipeWire/PulseAudio bypass. The opener's
                # sibling-chain falls back to ALSA-prefixed siblings
                # first if the explicit ``alsa_entry`` device fails,
                # before trying session-manager siblings (which
                # would silently undo the bypass).
                preferred_host_api=_LINUX_ALSA_HOST_API,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_alsa_hw_direct_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Mirror the exclusive-fallback behaviour: try to recover
            # the pipeline through shared mode so the user is not left
            # with a dead stream.
            try:
                await self._reopen_stream_after_device_error()
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "audio_capture_alsa_hw_direct_fallback_failed",
                    error=str(fallback_exc),
                )
                self._signal_consumer_shutdown()
                result = AlsaHwDirectRestartResult(
                    verdict=AlsaHwDirectRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    host_api=self._host_api_name,
                    device=self._input_device,
                    detail=(
                        f"ALSA-direct open failed ({exc}); session-manager "
                        f"fallback also failed ({fallback_exc})"
                    ),
                )
                _emit_alsa_hw_direct_restart_metric(result)
                return result
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=f"ALSA-direct open failed ({exc}); recovered via session manager",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = alsa_entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # T32 — emit CaptureRestartFrame BEFORE the ring-buffer
        # epoch increment. APO_DEGRADED + bypass_tier=2
        # (LinuxPipeWireDirectBypass is the Tier 2 strategy for the
        # Linux signal-processing-degradation case). The
        # new_signal_processing_mode reflects whether the opener
        # honoured the ALSA-direct request — if WASAPI-style
        # downgrade applies on Linux too (opener falls back to a
        # session-manager sibling), the field reads
        # ``"session_manager"`` so the dashboard can render the
        # downgrade case correctly.
        new_mode = "alsa_hw_direct" if info.host_api == _LINUX_ALSA_HOST_API else "session_manager"
        self._pipeline.record_capture_restart(
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=time.monotonic(),
                restart_reason=CaptureRestartReason.APO_DEGRADED.value,
                old_host_api=old_host_api,
                new_host_api=self._host_api_name or "",
                old_device_id=old_device_id,
                new_device_id=self._resolved_device_name or str(self._input_device or ""),
                old_signal_processing_mode="session_manager",
                new_signal_processing_mode=new_mode,
                bypass_tier=2,
            )
        )
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=True)

        if info.host_api != _LINUX_ALSA_HOST_API:
            logger.error(
                "audio_capture_alsa_hw_direct_restart_downgraded_to_session_manager",
                device=self._input_device,
                host_api=info.host_api,
                sample_rate=self._sample_rate,
                channels=info.channels,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"opener fell back to {info.host_api!r} — session manager still in signal path"
                ),
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_alsa_hw_direct_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = AlsaHwDirectRestartResult(
            verdict=AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_alsa_hw_direct_restart_metric(result)
        return result

    async def request_session_manager_restart(
        self,
        target_device: DeviceEntry | None = None,
    ) -> SessionManagerRestartResult:
        """Revert the capture stream to the PipeWire/PulseAudio session manager.

        Linux-specific twin of :meth:`request_shared_restart`. Two
        legitimate callers:

        * :class:`LinuxPipeWireDirectBypass` (revert path) — no
          ``target_device`` supplied, the method searches for the
          first sibling whose :attr:`DeviceEntry.host_api_name` lies
          in :data:`_LINUX_SESSION_MANAGER_HOST_APIS`.
        * :class:`LinuxSessionManagerEscapeBypass` (apply path, T6
          of voice-linux-cascade-root-fix) — supplies a concrete
          ``target_device`` resolved to a session-manager virtual
          (``pipewire``, ``pulse``) or the OS default ``default`` PCM.
          The method skips sibling discovery and opens directly.

        When neither path yields a target the method returns
        :attr:`SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW` with
        the existing stream preserved.

        Args:
            target_device: Optional explicit target. When ``None``,
                the canonical-name-sibling discovery runs. When
                provided, the method opens against that device
                verbatim — callers are responsible for pre-filtering.

        Returns:
            A :class:`SessionManagerRestartResult`. A non-engaged
            verdict means either the session-manager reopen was not
            feasible (``DOWNGRADED_TO_ALSA_HW``, ``NO_TARGET``) or the
            pipeline is now without a live capture
            (``OPEN_FAILED_NO_STREAM``).
        """
        if not self._running:
            logger.debug("audio_capture_session_manager_restart_skipped_not_running")
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_session_manager_restart_metric(result)
            return result
        if sys.platform != "linux":
            logger.debug(
                "audio_capture_session_manager_restart_skipped_not_linux",
                platform=sys.platform,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.NOT_LINUX,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"request_session_manager_restart is Linux-only; running on {sys.platform}"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result

        if target_device is not None:
            session_entry: DeviceEntry | None = target_device
        else:
            session_entry = self._find_sibling_with_host_api_in(
                _LINUX_SESSION_MANAGER_HOST_APIS,
            )
        if session_entry is None:
            logger.warning(
                "audio_capture_session_manager_restart_no_sibling",
                device=self._input_device,
                host_api=self._host_api_name,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "no PulseAudio/PipeWire sibling available — device is "
                    "ALSA-direct only; existing stream preserved"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        # T32 — snapshot pre-restart substrate (typically the
        # ALSA-direct kernel device the bypass was using) before the
        # close + session-manager reopen.
        old_host_api = self._host_api_name or ""
        old_device_id = self._resolved_device_name or str(self._input_device or "")
        logger.warning(
            "audio_capture_session_manager_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
            target_host_api=session_entry.host_api_name,
            target_device_index=session_entry.index,
        )

        await asyncio.to_thread(self._close_stream, "session_manager_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=session_entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
                # T29 — explicit session-manager preference. The
                # bypass target's host_api (PipeWire / PulseAudio)
                # is the resolution priority for sibling-chain
                # fallback so a flaky kernel-ALSA endpoint doesn't
                # silently win the chain back.
                preferred_host_api=session_entry.host_api_name,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_session_manager_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            self._signal_consumer_shutdown()
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                detail=f"session-manager reopen failed: {exc}",
            )
            _emit_session_manager_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = session_entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # T32 — emit CaptureRestartFrame for the Linux revert pair.
        # Two legitimate semantics: (a) revert from a prior
        # ALSA-direct bypass (operator unpin / coordinator revert) →
        # MANUAL; (b) the LinuxSessionManagerEscapeBypass apply path
        # using ``target_device`` to escape a misbehaving plain ALSA
        # endpoint by routing through PipeWire/PulseAudio instead →
        # APO_DEGRADED. The discriminator is whether ``target_device``
        # was supplied: explicit-target = the Tier 1 escape strategy;
        # None = the Tier 2 revert. Bypass tier matches.
        is_escape = target_device is not None
        self._pipeline.record_capture_restart(
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=time.monotonic(),
                restart_reason=(
                    CaptureRestartReason.APO_DEGRADED.value
                    if is_escape
                    else CaptureRestartReason.MANUAL.value
                ),
                old_host_api=old_host_api,
                new_host_api=self._host_api_name or "",
                old_device_id=old_device_id,
                new_device_id=self._resolved_device_name or str(self._input_device or ""),
                old_signal_processing_mode=(
                    "alsa_hw_direct" if not is_escape else "session_manager"
                ),
                new_signal_processing_mode="session_manager",
                bypass_tier=1 if is_escape else 0,
            )
        )
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)

        if info.host_api not in _LINUX_SESSION_MANAGER_HOST_APIS:
            logger.warning(
                "audio_capture_session_manager_restart_downgraded_to_alsa_hw",
                device=self._input_device,
                host_api=info.host_api,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"opener fell back to {info.host_api!r} — session manager not in signal path"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_session_manager_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = SessionManagerRestartResult(
            verdict=SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_session_manager_restart_metric(result)
        return result

    async def request_host_api_rotate(
        self,
        target_host_api: str,
        *,
        target_exclusive: bool = False,
    ) -> HostApiRotateResult:
        """Pivot the capture stream to a sibling on ``target_host_api``.

        T28 — drives the Tier 2 ``WindowsHostApiRotateThenExclusive``
        bypass strategy. For endpoints whose runtime ``host_api_name``
        is ``MME`` / ``Windows DirectSound`` / ``Windows WDM-KS``, the
        rotation moves the capture stream to ``Windows WASAPI`` so a
        subsequent :meth:`request_exclusive_restart` can engage
        exclusive mode (which bypasses every APO layer).

        Cross-platform safety: the strategy itself gates on
        ``platform_key == "win32"`` but this method is defensive —
        direct invocation on a non-Windows host returns
        :attr:`HostApiRotateVerdict.NOT_WIN32` with the existing
        stream preserved.

        Twin-pair pattern: this method mutates ``self._host_api_name``
        on success so subsequent device-error reopens through
        :meth:`_reopen_stream_after_device_error` honour the rotated
        host_api. The cascade-alignment-enabled opener (Furo W-4 fix
        in ``_stream_opener._device_chain``) is a prerequisite —
        without it, the next reopen drifts back to PortAudio
        enumeration order and silently undoes the rotation. The
        cross-validator at
        :func:`engine/config.py::_enforce_paranoid_mission_dependencies`
        rejects the contradictory configuration at boot.

        Args:
            target_host_api: Host_api label of the target sibling
                (typically ``"Windows WASAPI"``). The method resolves
                a :class:`DeviceEntry` whose ``canonical_name`` matches
                the current endpoint AND whose ``host_api_name``
                equals this argument; that entry is handed to the
                unified opener.
            target_exclusive: When ``True``, the rotated stream opens
                in WASAPI exclusive mode atomically — saves a second
                close/reopen cycle compared to ``rotate; then
                request_exclusive_restart``. Default ``False`` for
                the Tier 2 2-phase strategy (Phase A rotates shared,
                Phase B engages exclusive separately).

        Returns:
            A :class:`HostApiRotateResult` describing the outcome.
            ``ROTATED_SUCCESS`` is the only "engaged" verdict;
            everything else means the rotation didn't take or was
            never attempted.
        """
        source_host_api = self._host_api_name
        if not self._running:
            logger.debug("audio_capture_host_api_rotate_skipped_not_running")
            result = HostApiRotateResult(
                verdict=HostApiRotateVerdict.NOT_RUNNING,
                engaged=False,
                target_host_api=target_host_api,
                source_host_api=source_host_api,
                detail="capture task is not running",
            )
            _emit_host_api_rotate_metric(result)
            return result
        if sys.platform != "win32":
            logger.debug(
                "audio_capture_host_api_rotate_skipped_not_win32",
                platform=sys.platform,
            )
            result = HostApiRotateResult(
                verdict=HostApiRotateVerdict.NOT_WIN32,
                engaged=False,
                target_host_api=target_host_api,
                source_host_api=source_host_api,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(f"request_host_api_rotate is Windows-only; running on {sys.platform}"),
            )
            _emit_host_api_rotate_metric(result)
            return result

        # Resolve a sibling on the target host_api. The sibling
        # discovery walks the current device's canonical_name peers
        # filtered to ``target_host_api``. No sibling = no rotation
        # possible (e.g. WASAPI build excludes the active endpoint).
        target_entry = self._find_sibling_with_host_api(target_host_api)
        if target_entry is None:
            logger.warning(
                "audio_capture_host_api_rotate_no_sibling",
                device=self._input_device,
                source_host_api=source_host_api,
                target_host_api=target_host_api,
            )
            result = HostApiRotateResult(
                verdict=HostApiRotateVerdict.NO_TARGET_SIBLING,
                engaged=False,
                target_host_api=target_host_api,
                source_host_api=source_host_api,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"no {target_host_api!r}-host-API sibling found for "
                    f"current endpoint (PortAudio build without that "
                    f"backend, or device held exclusive)"
                ),
            )
            _emit_host_api_rotate_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        base_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        rotate_tuning = base_tuning.model_copy(
            update={"capture_wasapi_exclusive": target_exclusive}
        )
        # T32 — snapshot pre-rotation substrate for the
        # CaptureRestartFrame emission.
        old_host_api = self._host_api_name or ""
        old_device_id = self._resolved_device_name or str(self._input_device or "")
        logger.warning(
            "audio_capture_host_api_rotate_begin",
            device=self._input_device,
            source_host_api=source_host_api,
            target_host_api=target_host_api,
            target_device_index=target_entry.index,
            target_exclusive=target_exclusive,
        )

        # Tear down the existing stream on the PortAudio thread before
        # re-opening on the target host_api — some backends reject a
        # second client even for read-only capture (mirrors the close
        # pattern used by request_exclusive_restart).
        await asyncio.to_thread(self._close_stream, "host_api_rotate")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=target_entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=rotate_tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
                # Critical: the opener's preferred_host_api MUST be
                # the rotation target so the sibling-chain fallback
                # respects the strategy's intent — without this the
                # opener could fall back to the source host_api and
                # silently undo the rotation.
                preferred_host_api=target_host_api,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_host_api_rotate_failed",
                error=str(exc),
                device=self._input_device,
                source_host_api=source_host_api,
                target_host_api=target_host_api,
            )
            # Mirror the exclusive-fallback behaviour: try to recover
            # the pipeline through the source host_api so the user is
            # not left with a dead stream.
            try:
                await self._reopen_stream_after_device_error()
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "audio_capture_host_api_rotate_fallback_failed",
                    error=str(fallback_exc),
                )
                self._signal_consumer_shutdown()
                result = HostApiRotateResult(
                    verdict=HostApiRotateVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    target_host_api=target_host_api,
                    source_host_api=source_host_api,
                    detail=(
                        f"host-api rotate open failed ({exc}); source-"
                        f"host-api fallback also failed ({fallback_exc})"
                    ),
                )
                _emit_host_api_rotate_metric(result)
                return result
            result = HostApiRotateResult(
                verdict=HostApiRotateVerdict.DOWNGRADED_TO_SOURCE,
                engaged=False,
                target_host_api=target_host_api,
                source_host_api=source_host_api,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"target {target_host_api!r} open failed ({exc}); "
                    f"recovered to source {source_host_api!r}"
                ),
            )
            _emit_host_api_rotate_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = target_entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        from sovyx.voice._agc2_adaptive_floor import build_agc2_adaptive_floor

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
                adaptive_floor=build_agc2_adaptive_floor(
                    enabled=_agc2_tuning.voice_agc2_adaptive_floor_enabled,
                    window_seconds=_agc2_tuning.voice_agc2_adaptive_floor_window_seconds,
                    quantile=_agc2_tuning.voice_agc2_adaptive_floor_quantile,
                    sample_rate=info.sample_rate,
                ),
            ),
            aec=self._aec,
            render_provider=self._render_provider,
            double_talk_detector=self._double_talk_detector,
            noise_suppressor=self._noise_suppressor,
            snr_estimator=self._snr_estimator,
            dither_enabled=self._dither_enabled,
            dither_amplitude_lsb=self._dither_amplitude_lsb,
        )
        # T32 — emit CaptureRestartFrame for the rotation. Tier 2
        # bypass = APO_DEGRADED reason + bypass_tier=2.
        new_mode = "exclusive" if info.exclusive_used else "shared"
        self._pipeline.record_capture_restart(
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=time.monotonic(),
                restart_reason=CaptureRestartReason.APO_DEGRADED.value,
                old_host_api=old_host_api,
                new_host_api=self._host_api_name or "",
                old_device_id=old_device_id,
                new_device_id=self._resolved_device_name or str(self._input_device or ""),
                old_signal_processing_mode="shared",
                new_signal_processing_mode=new_mode,
                bypass_tier=2,
            )
        )
        self._allocate_ring_buffer(rotate_tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=True)

        # Defensive: even when the opener honoured the target
        # host_api, double-check the resulting info matches — a
        # future opener regression that ignored preferred_host_api
        # silently would otherwise show as ROTATED_SUCCESS while the
        # actual stream stayed on the source host_api.
        if info.host_api != target_host_api:
            logger.error(
                "audio_capture_host_api_rotate_downgraded",
                device=self._input_device,
                source_host_api=source_host_api,
                target_host_api=target_host_api,
                resulting_host_api=info.host_api,
            )
            result = HostApiRotateResult(
                verdict=HostApiRotateVerdict.DOWNGRADED_TO_SOURCE,
                engaged=False,
                target_host_api=target_host_api,
                source_host_api=source_host_api,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"opener fell back to {info.host_api!r} — target "
                    f"{target_host_api!r} not in resulting signal path"
                ),
            )
            _emit_host_api_rotate_metric(result)
            return result
        logger.warning(
            "audio_capture_host_api_rotate_ok",
            device=self._input_device,
            source_host_api=source_host_api,
            target_host_api=target_host_api,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = HostApiRotateResult(
            verdict=HostApiRotateVerdict.ROTATED_SUCCESS,
            engaged=True,
            target_host_api=target_host_api,
            source_host_api=source_host_api,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_host_api_rotate_metric(result)
        return result

    def _find_sibling_with_host_api(self, host_api: str) -> DeviceEntry | None:
        """Return the enumeration sibling of the current endpoint on ``host_api``.

        Siblings share the same :attr:`DeviceEntry.canonical_name` but
        are served by different host APIs — on Linux a single USB
        microphone typically appears once via ``ALSA``, once via
        ``PulseAudio``, once via ``PipeWire``. Returns ``None`` when the
        current entry has no sibling on the requested host API.
        """
        return self._find_sibling_with_host_api_in(frozenset({host_api}))

    def _find_sibling_with_host_api_in(
        self,
        host_apis: frozenset[str],
    ) -> DeviceEntry | None:
        """Return the first enumeration sibling whose host API is in ``host_apis``.

        Uses the same :func:`_resolve_input_entry` entry point as the
        start + restart paths so any DI-provided ``enumerate_fn`` is
        honoured. Returns ``None`` on enumeration failure rather than
        raising — the caller translates the absence into a structured
        verdict.
        """
        try:
            current = _resolve_input_entry(
                input_device=self._input_device,
                enumerate_fn=self._enumerate_fn,
                host_api_name=self._host_api_name,
            )
        except RuntimeError:
            return None
        canonical = current.canonical_name
        if self._enumerate_fn is not None:
            entries = self._enumerate_fn()
        else:
            from sovyx.voice.device_enum import enumerate_devices

            entries = enumerate_devices()
        for entry in entries:
            if entry.max_input_channels <= 0:
                continue
            if entry.canonical_name != canonical:
                continue
            if entry.host_api_name in host_apis:
                return entry
        return None
