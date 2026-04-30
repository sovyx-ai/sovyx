"""Background audio capture task that feeds the VoicePipeline.

The :class:`~sovyx.voice.pipeline.VoicePipeline` is push-based — frames
must be delivered via ``pipeline.feed_frame()``. This module owns the
microphone side: opens a ``sounddevice.InputStream`` on the selected
input device (through the unified :mod:`sovyx.voice._stream_opener`
pyramid), pulls int16 frames from its callback into an asyncio queue,
and dispatches each frame into the pipeline from a consumer task. On
device disconnection the stream is closed, the task waits for
``capture_reconnect_delay_seconds``, and retries from scratch — again
through the opener, so reconnect inherits host-API × rate × channels
fallback for free.

Lifecycle (owned by the hot-enable endpoint)::

    capture = AudioCaptureTask(pipeline, input_device=device_index)
    await capture.start()
    ...
    await capture.stop()

Post-open validation
--------------------

``sd.InputStream`` on Windows happily opens a broken configuration
(MME + 16 kHz on a 48 kHz Razer driver, privacy-blocked mic, etc.) and
then delivers **all-zero frames** without raising. The pipeline looks
"running" but is deaf. :meth:`AudioCaptureTask.start` hands a
``validate_fn`` to :func:`sovyx.voice._stream_opener.open_input_stream`,
which samples ~600 ms of audio after opening each variant and rejects
it when the peak RMS never crosses ``capture_validation_min_rms_db``.
The opener walks the full pyramid automatically, so silent variants
are replaced by their host-API siblings without any caller-side
bookkeeping.

Without this task the pipeline is silent: frames never arrive and VAD
never fires. See CLAUDE.md §anti-pattern #14 — ONNX inference is run on
``asyncio.to_thread`` already inside :meth:`VoicePipeline.feed_frame`,
so this consumer loop does not need to offload work itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice._agc2 import build_agc2_if_enabled
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._frame_normalizer import FrameNormalizer

# T1.4 step 5 — module-level constants extracted to
# ``voice/capture/_constants``. Re-exported via the explicit
# ``import X as X`` pattern so existing imports (``test_capture_task.py``
# imports ``_RING_EPOCH_SHIFT`` and ``_HEARTBEAT_INTERVAL_S`` directly)
# AND the monkeypatch sites (``sovyx.voice._capture_task._RECONNECT_DELAY_S``)
# keep working without an import-path migration.
from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_MIN_CALLBACKS as _CAPTURE_UNDERRUN_MIN_CALLBACKS,
)
from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_WARN_FRACTION as _CAPTURE_UNDERRUN_WARN_FRACTION,
)
from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_WARN_INTERVAL_S as _CAPTURE_UNDERRUN_WARN_INTERVAL_S,
)
from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_WINDOW_S as _CAPTURE_UNDERRUN_WINDOW_S,
)
from sovyx.voice.capture._constants import _FRAME_SAMPLES as _FRAME_SAMPLES
from sovyx.voice.capture._constants import _HEARTBEAT_INTERVAL_S as _HEARTBEAT_INTERVAL_S
from sovyx.voice.capture._constants import _QUEUE_MAXSIZE as _QUEUE_MAXSIZE
from sovyx.voice.capture._constants import _RECONNECT_DELAY_S as _RECONNECT_DELAY_S
from sovyx.voice.capture._constants import _RING_EPOCH_SHIFT as _RING_EPOCH_SHIFT
from sovyx.voice.capture._constants import _RING_SAMPLES_MASK as _RING_SAMPLES_MASK
from sovyx.voice.capture._constants import _SAMPLE_RATE as _SAMPLE_RATE
from sovyx.voice.capture._constants import (
    _VALIDATION_MIN_RMS_DB as _VALIDATION_MIN_RMS_DB,
)
from sovyx.voice.capture._constants import _VALIDATION_S as _VALIDATION_S

# T1.4 step 3 — Linux session-manager contention helpers extracted to
# ``voice/capture/_contention``. Re-exported via the explicit
# ``import X as X`` pattern so existing imports — particularly
# ``test_capture_device_contended_error.py`` which imports the two
# public helpers directly — keep working without an import-path
# migration.
from sovyx.voice.capture._contention import (
    _SESSION_MANAGER_CONTENTION_ERROR_CODES as _SESSION_MANAGER_CONTENTION_ERROR_CODES,
)
from sovyx.voice.capture._contention import (
    _is_session_manager_contention_pattern as _is_session_manager_contention_pattern,
)
from sovyx.voice.capture._contention import (
    _suggest_session_manager_alternatives as _suggest_session_manager_alternatives,
)

# T1.4 step 4 — pure helpers (RMS dBFS, dBFS regex parsing, device-
# entry resolver) extracted to ``voice/capture/_helpers``. Re-exported
# via the explicit ``import X as X`` pattern so existing imports —
# particularly ``test_capture_task.py`` which imports
# ``_extract_peak_db`` and ``_resolve_input_entry`` directly — keep
# working without an import-path migration.
# T1.4 step 6 — first mixin landed. Subsequent steps add more
# mixins to the composition root per
# ``docs-internal/T1.4-step-6-mixin-surgery-plan.md``.
from sovyx.voice.capture._epoch import EpochMixin

# T1.4 step 2 — exception class hierarchy extracted to
# ``voice/capture/_exceptions``. Re-exported via the explicit
# ``import X as X`` pattern so existing imports like
# ``from sovyx.voice._capture_task import CaptureInoperativeError``
# (used in dashboard tests + voice_factory tests + integration suite)
# keep working without an import-path migration.
from sovyx.voice.capture._exceptions import (
    CaptureDeviceContendedError as CaptureDeviceContendedError,
)
from sovyx.voice.capture._exceptions import CaptureError as CaptureError
from sovyx.voice.capture._exceptions import (
    CaptureInoperativeError as CaptureInoperativeError,
)
from sovyx.voice.capture._exceptions import (
    CaptureSilenceError as CaptureSilenceError,
)
from sovyx.voice.capture._helpers import _PEAK_DB_RE as _PEAK_DB_RE
from sovyx.voice.capture._helpers import _RMS_FLOOR_DB as _RMS_FLOOR_DB
from sovyx.voice.capture._helpers import _extract_peak_db as _extract_peak_db
from sovyx.voice.capture._helpers import _resolve_input_entry as _resolve_input_entry
from sovyx.voice.capture._helpers import _rms_db_int16 as _rms_db_int16
from sovyx.voice.capture._lifecycle_mixin import LifecycleMixin
from sovyx.voice.capture._loop_mixin import LoopMixin

# T1.4 step 1 — restart-verdict types + dataclasses + metric emitters
# extracted to ``voice/capture/_restart`` per master mission Phase 1
# / T1.4. Re-exported here via the explicit ``import X as X`` pattern
# (mypy strict requires this form for an import to count as a public
# re-export) so legacy imports + the 13 timing-primitive test patches
# (per spec) keep working without an import-path migration (CLAUDE.md
# anti-pattern #20).
from sovyx.voice.capture._restart import _LINUX_ALSA_HOST_API as _LINUX_ALSA_HOST_API
from sovyx.voice.capture._restart import (
    _LINUX_SESSION_MANAGER_HOST_APIS as _LINUX_SESSION_MANAGER_HOST_APIS,
)
from sovyx.voice.capture._restart import (
    AlsaHwDirectRestartResult as AlsaHwDirectRestartResult,
)
from sovyx.voice.capture._restart import (
    AlsaHwDirectRestartVerdict as AlsaHwDirectRestartVerdict,
)
from sovyx.voice.capture._restart import (
    ExclusiveRestartResult as ExclusiveRestartResult,
)
from sovyx.voice.capture._restart import (
    ExclusiveRestartVerdict as ExclusiveRestartVerdict,
)
from sovyx.voice.capture._restart import (
    HostApiRotateResult as HostApiRotateResult,
)
from sovyx.voice.capture._restart import (
    HostApiRotateVerdict as HostApiRotateVerdict,
)
from sovyx.voice.capture._restart import (
    SessionManagerRestartResult as SessionManagerRestartResult,
)
from sovyx.voice.capture._restart import (
    SessionManagerRestartVerdict as SessionManagerRestartVerdict,
)
from sovyx.voice.capture._restart import (
    SharedRestartResult as SharedRestartResult,
)
from sovyx.voice.capture._restart import (
    SharedRestartVerdict as SharedRestartVerdict,
)
from sovyx.voice.capture._restart import (
    _emit_alsa_hw_direct_restart_metric as _emit_alsa_hw_direct_restart_metric,
)
from sovyx.voice.capture._restart import (
    _emit_exclusive_restart_metric as _emit_exclusive_restart_metric,
)
from sovyx.voice.capture._restart import (
    _emit_host_api_rotate_metric as _emit_host_api_rotate_metric,
)
from sovyx.voice.capture._restart import (
    _emit_session_manager_restart_metric as _emit_session_manager_restart_metric,
)
from sovyx.voice.capture._restart import (
    _emit_shared_restart_metric as _emit_shared_restart_metric,
)
from sovyx.voice.capture._restart_mixin import RestartMixin
from sovyx.voice.capture._ring import RingMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine._backoff import BackoffSchedule
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._aec import AecProcessor, RenderPcmProvider
    from sovyx.voice._double_talk_detector import DoubleTalkDetector
    from sovyx.voice._noise_suppression import NoiseSuppressor
    from sovyx.voice._snr_estimator import SnrEstimator
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


class AudioCaptureTask(EpochMixin, RingMixin, LifecycleMixin, LoopMixin, RestartMixin):
    """Microphone → VoicePipeline bridge.

    Composition root for the capture-task mixin pattern (T1.4):

    * :class:`~sovyx.voice.capture._epoch.EpochMixin` — owns
      :meth:`samples_written_mark`, the atomic
      ``(epoch, samples_written)`` decomposition.
    * Future steps land additional mixins (``RingMixin``,
      ``RestartMixin``, ``LoopMixin``) per
      ``docs-internal/T1.4-step-6-mixin-surgery-plan.md``.

    Owns a ``sounddevice.InputStream`` running at 16 kHz / int16 /
    512-sample blocks — the exact frame shape the pipeline expects.
    Frames land on an asyncio queue via ``call_soon_threadsafe`` from
    the PortAudio thread and are drained by an async consumer task
    that calls ``pipeline.feed_frame`` for each one.

    Args:
        pipeline: The orchestrator to feed frames into.
        input_device: PortAudio device index/name. ``None`` uses the OS
            default input device.
        sample_rate: Capture rate in Hz. Only 16 kHz is supported by
            the downstream VAD / STT.
        blocksize: Samples per callback block. Must equal
            ``_FRAME_SAMPLES`` so each block is a whole pipeline frame.
        host_api_name: Host API label (``"Windows WASAPI"``, ``"MME"``, …)
            recorded for :meth:`status_snapshot` so ``/api/voice/status``
            can expose which variant is live.
        validate_on_start: When True (default), :meth:`start` samples the
            first ~600 ms of audio and raises :class:`CaptureSilenceError`
            if the peak RMS never crosses the noise floor. Tests can
            disable this to avoid racing PortAudio stubs.
    """

    def __init__(
        self,
        pipeline: VoicePipeline,
        *,
        input_device: int | str | None = None,
        sample_rate: int = _SAMPLE_RATE,
        blocksize: int = _FRAME_SAMPLES,
        host_api_name: str | None = None,
        validate_on_start: bool = True,
        tuning: VoiceTuningConfig | None = None,
        sd_module: Any | None = None,  # noqa: ANN401 — DI for tests
        enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,
        endpoint_guid: str | None = None,
        aec: AecProcessor | None = None,
        render_provider: RenderPcmProvider | None = None,
        double_talk_detector: DoubleTalkDetector | None = None,
        noise_suppressor: NoiseSuppressor | None = None,
        snr_estimator: SnrEstimator | None = None,
        dither_enabled: bool = False,
        dither_amplitude_lsb: float = 1.0,
        wiener_entropy_check_enabled: bool = False,
        wiener_entropy_threshold: float = 0.5,
        resample_peak_check_enabled: bool = False,
    ) -> None:
        self._pipeline = pipeline
        self._input_device = input_device
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._host_api_name = host_api_name
        self._validate_on_start = validate_on_start
        self._tuning = tuning
        self._sd_module = sd_module
        self._enumerate_fn = enumerate_fn
        # Phase 4 / T4.4.d — AEC processor + render reference. Stored
        # so every FrameNormalizer construction site (initial open +
        # all 6 RestartMixin paths) can pass them through. Default
        # ``None`` preserves the pre-AEC behaviour bit-exactly.
        self._aec: AecProcessor | None = aec
        self._render_provider: RenderPcmProvider | None = render_provider
        self._double_talk_detector: DoubleTalkDetector | None = double_talk_detector
        self._noise_suppressor: NoiseSuppressor | None = noise_suppressor
        self._snr_estimator: SnrEstimator | None = snr_estimator
        self._dither_enabled: bool = dither_enabled
        self._dither_amplitude_lsb: float = dither_amplitude_lsb
        self._wiener_entropy_check_enabled: bool = wiener_entropy_check_enabled
        self._wiener_entropy_threshold: float = wiener_entropy_threshold
        self._resample_peak_check_enabled: bool = resample_peak_check_enabled
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False
        self._normalizer: FrameNormalizer | None = None
        self._resolved_device_name: str | None = None
        self._endpoint_guid: str = endpoint_guid or ""
        # Band-aid #10 replacement: per-task exponential backoff
        # schedule for the reconnect loop. Lazy-initialised on first
        # PortAudio error so the zero-error case has zero overhead.
        # Reset to attempt 0 on each successful reconnect so a
        # transient outage doesn't penalise future ones.
        self._reconnect_backoff: BackoffSchedule | None = None
        # TS3 chaos injector — opt-in frame-drop simulation at the
        # CAPTURE_UNDERRUN site. Disabled by default; chaos test
        # matrix sets the env vars to validate that the deaf-
        # detection coordinator (O2) + watchdog promotion fire
        # correctly when frames stop arriving at VAD. The chaos
        # drops the frame BEFORE pipeline.feed_frame — observationally
        # identical to a PortAudio kernel-side underrun from the
        # consumer's perspective.
        self._chaos = ChaosInjector(site_id=ChaosSite.CAPTURE_UNDERRUN.value)

        # Telemetry — populated by the consumer loop.
        self._last_rms_db: float = _RMS_FLOOR_DB
        self._frames_delivered: int = 0
        self._last_heartbeat_monotonic: float = 0.0
        self._frames_since_heartbeat: int = 0
        self._silent_frames_since_heartbeat: int = 0

        # Per-stream lifecycle counters — reset on every successful open
        # so ``audio.stream.closed`` reflects the activity of *that*
        # stream, not the cumulative life of the task.
        self._stream_id: str = ""
        self._stream_underruns: int = 0
        self._stream_overflows: int = 0
        self._stream_callback_frames: int = 0

        # Band-aid #9 — sustained-underrun rolling-window state. Snapshot
        # of the lifetime counters at window start; the consumer loop
        # diffs the live counters against these to compute the per-window
        # rate. ``_underrun_window_started_at`` is None until the first
        # consumer iteration, then set on every window roll.
        self._underrun_window_started_at: float | None = None
        self._underrun_window_callbacks_at_start: int = 0
        self._underrun_window_underruns_at_start: int = 0
        self._last_underrun_warning_monotonic: float | None = None

        # Ring buffer — allocated lazily in :meth:`start` from the
        # per-instance tuning so tests that build the task without
        # calling start() don't pay the ~1 MB allocation cost.
        #
        # Thread-safety: writes happen inside ``_consume_loop`` between
        # ``await`` points and reads from :meth:`tap_recent_frames`
        # happen on the same event loop; the asyncio scheduler serialises
        # them without a lock as long as neither path awaits while
        # mutating the index fields. That invariant is asserted by the
        # unit tests — do not add awaits inside the critical sections.
        #
        # v1.3 §4.2 L4-B — ``_ring_state`` packs ``(epoch, samples_written)``
        # into a single int (layout in ``_RING_EPOCH_SHIFT`` / ``_RING_SAMPLES_MASK``)
        # so external readers via :meth:`samples_written_mark` observe a
        # consistent pair in one atomic ``LOAD_ATTR``. The bare
        # ``_ring_write_index`` remains separate because it is read only
        # by :meth:`tap_recent_frames` on the same event loop as the
        # writer — no cross-loop atomicity required.
        self._ring_buffer: npt.NDArray[np.int16] | None = None
        self._ring_capacity: int = 0
        self._ring_write_index: int = 0
        self._ring_state: int = 0

    # -- Properties -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the capture task is active (stream open + consumer live)."""
        return self._running

    @property
    def input_device(self) -> int | str | None:
        """Selected PortAudio input device (``None`` = OS default)."""
        return self._input_device

    @property
    def input_device_name(self) -> str | None:
        """Resolved PortAudio device name for the active stream.

        Populated during :meth:`start` from the enumerated
        :class:`DeviceEntry`. Remains ``None`` until the stream opens
        successfully, so callers (dashboard diagnostics) can distinguish
        "not yet started" from "OS default" safely.
        """
        return self._resolved_device_name

    @property
    def active_device_name(self) -> str:
        """Non-nullable alias of :attr:`input_device_name`.

        Satisfies :class:`~sovyx.voice.health.contract.CaptureTaskProto`:
        the bypass coordinator + strategies work with a plain ``str``
        instead of ``str | None`` since they only run after the stream
        is open and always need a human-readable label for logs.
        Returns ``""`` during the pre-start window.
        """
        return self._resolved_device_name or ""

    @property
    def active_device_guid(self) -> str:
        """Stable endpoint GUID for the live capture stream — see ADR §1.

        Populated either from the explicit constructor ``endpoint_guid``
        argument or derived at :meth:`start` from the resolved
        :class:`DeviceEntry` via
        :func:`sovyx.voice.health._factory_integration.derive_endpoint_guid`.
        Returns ``""`` before the stream opens so callers can distinguish
        "not yet started" from "OS default" — the bypass coordinator
        will not call this pre-start anyway, but the Protocol requires a
        non-nullable string return.
        """
        return self._endpoint_guid

    @property
    def host_api_name(self) -> str | None:
        """Host API label for the opened stream (``None`` if unknown)."""
        return self._host_api_name

    @property
    def active_device_index(self) -> int:
        """PortAudio index of the open capture device; ``-1`` pre-start.

        Introduced by :mod:`voice-linux-cascade-root-fix` so runtime
        bypass strategies can address the exact numeric index the
        stream is currently bound to. ``-1`` is the structural
        sentinel for "not yet started" — no real PortAudio device
        ever takes that index.
        """
        if isinstance(self._input_device, int):
            return self._input_device
        return -1

    @property
    def active_device_kind(self) -> str:
        """Best-effort semantic kind of the active device.

        Returns the :class:`~sovyx.voice.device_enum.DeviceKind` value
        for the current ``active_device_name`` when enumeration
        succeeds; ``"unknown"`` otherwise. Used by the
        :class:`LinuxSessionManagerEscapeBypass` eligibility probe to
        tell a hardware node from a session-manager virtual.
        Never raises.
        """
        if not self._running:
            return "unknown"
        try:
            from sovyx.voice.device_enum import classify_device_kind

            return classify_device_kind(
                name=self._resolved_device_name or "",
                host_api_name=self._host_api_name or "",
                platform_key=sys.platform,
            ).value
        except Exception:  # noqa: BLE001 — classifier must never fail an apply path
            return "unknown"

    @property
    def last_rms_db(self) -> float:
        """Most recent per-frame RMS in dBFS (updated by consumer loop)."""
        return self._last_rms_db

    @property
    def frames_delivered(self) -> int:
        """Total frames fed to the pipeline since :meth:`start`."""
        return self._frames_delivered

    def status_snapshot(self) -> dict[str, Any]:
        """Compact dict for ``/api/voice/status`` — no async, no locks."""
        return {
            "running": self._running,
            "input_device": self._input_device,
            "host_api": self._host_api_name,
            "sample_rate": self._sample_rate,
            "frames_delivered": self._frames_delivered,
            "last_rms_db": round(self._last_rms_db, 1),
        }

    def apply_mic_ducking_db(self, gain_db: float) -> None:
        """Forward a self-feedback duck gain target to the normalizer.

        Thin adapter invoked by
        :class:`~sovyx.voice.health.SelfFeedbackGate` when TTS starts /
        ends (ADR §4.4.6.b). Before the capture stream opens, the
        normalizer is ``None`` — in that window the call is silently
        dropped: the gate will re-engage on the next utterance once
        the normalizer exists, which matches the ducking contract
        (attenuation is per-TTS-session, not persistent state).

        Args:
            gain_db: Target attenuation in dB. Must be ``<= 0``. The
                underlying :class:`FrameNormalizer` raises ``ValueError``
                on positive gains; we propagate that up so a programming
                error surfaces during testing, not silently in prod.
        """
        normalizer = self._normalizer
        if normalizer is None:
            return
        normalizer.set_ducking_gain_db(gain_db)

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the input stream, validate it, and spawn the consumer task.

        Delegates stream construction to
        :func:`sovyx.voice._stream_opener.open_input_stream`, which walks
        the full host-API × auto_convert × channels × rate pyramid and
        optionally validates each opened stream for silence via
        ``validate_fn``. When every viable variant delivers only zeros,
        :class:`CaptureSilenceError` is raised so callers (notably
        :func:`sovyx.voice.factory.create_voice_pipeline`) can surface a
        precise error payload to the UI.

        Idempotent — a second call while running is a no-op.

        Raises:
            CaptureSilenceError: Every pyramid variant opened cleanly
                but delivered only silence.
            RuntimeError: Every pyramid variant failed with a
                non-silence PortAudio error (device busy, permission,
                AUDCLNT_E_*). ``.code`` carries the classified
                :class:`ErrorCode`.
        """
        if self._running:
            return

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        self._loop = asyncio.get_running_loop()
        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        validate_fn = self._validate_stream_from_queue if self._validate_on_start else None

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=validate_fn,
                # T29 — pass the cascade winner's host_api so the
                # opener's sibling-chain fallback respects the
                # cascade preference. Furo W-4 fix: pre-T29 the
                # opener iterated siblings in raw PortAudio
                # enumeration order even when the cascade chose a
                # specific host_api (Razer + Voice Clarity drift
                # case). The opener's alignment is gated by
                # ``cascade_host_api_alignment_enabled`` (default
                # False in v0.24.0; planned default-flip in
                # v0.25.0) so this wire-up is a no-op until the
                # operator opts in.
                preferred_host_api=self._host_api_name or None,
            )
        except StreamOpenError as exc:
            self._raise_classified_open_error(exc, entry)

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = entry.name if entry is not None else None
        self._ensure_endpoint_guid(entry)
        self._allocate_ring_buffer(tuning)

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
            wiener_entropy_check_enabled=self._wiener_entropy_check_enabled,
            wiener_entropy_threshold=self._wiener_entropy_threshold,
            resample_peak_check_enabled=self._resample_peak_check_enabled,
        )
        if not self._normalizer.is_passthrough:
            logger.info(
                "audio_capture_resample_active",
                source_rate=info.sample_rate,
                source_channels=info.channels,
                target_rate=self._normalizer.target_rate,
                target_window=self._normalizer.target_window,
            )

        self._running = True
        self._last_heartbeat_monotonic = time.monotonic()
        self._consumer = spawn(self._consume_loop(), name="audio-capture-consumer")
        self._emit_stream_opened(info, apo_bypass_attempted=False)
        logger.info(
            "audio_capture_task_started",
            device=self._input_device if self._input_device is not None else "default",
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
            auto_convert=info.auto_convert_used,
            fallback_depth=info.fallback_depth,
            blocksize=self._blocksize,
            normalizer_active=not self._normalizer.is_passthrough,
        )

    async def _validate_stream_from_queue(
        self,
        _stream: Any,  # noqa: ANN401 — provided by opener, not used here
        *,
        device_index: int,  # noqa: ARG002
    ) -> float:
        """Drain ``_VALIDATION_S`` seconds of callback output and return peak dBFS.

        Two validation modes (controlled by
        :attr:`VoiceTuningConfig.capture_validation_require_signal`):

        * **Presence-only (default)**: accepts as soon as
          :attr:`~VoiceTuningConfig.capture_validation_min_frames` frames
          have arrived. Returns ``0.0`` dBFS (well above any threshold)
          so the opener treats the variant as valid regardless of
          ambient signal level. This is the right default for production
          capture: a silent user shouldn't invalidate a perfectly good
          audio path.
        * **Signal-gated (opt-in)**: measures peak RMS and requires it
          to cross ``capture_validation_min_rms_db``. Reserved for the
          setup-wizard and explicit diagnostic flows where the user is
          actively making noise.

        The queue is drained first so stale frames from a previously
        rejected pyramid variant do not leak into the current measurement.
        """
        return await self._validate_stream()

    def _raise_classified_open_error(
        self,
        exc: Any,  # noqa: ANN401 — StreamOpenError, typed lazily
        entry: DeviceEntry,
    ) -> None:
        """Map a :class:`StreamOpenError` to the public exception API.

        Classification order (most specific first):

        1. **All silent** → :class:`CaptureSilenceError`. Every variant
           opened but delivered ≤ validation-RMS audio; the wizard UX
           handles this distinctly (the mic is open but nobody's home).
        2. **Session-manager contention (Linux)** →
           :class:`CaptureDeviceContendedError`. Every variant failed
           with a contention-class error code on Linux AND at least
           one candidate was tried — the strong signal that another
           audio client is holding ``hw:X,Y``. Carries
           ``suggested_actions`` so the dashboard renders actionable
           chips. Introduced by ``voice-linux-cascade-root-fix`` T7.
        3. **Default** → generic :class:`RuntimeError` with ``.code``
           + ``.attempts`` attached for operator debugging. Preserves
           the pre-T7 behaviour for patterns we don't recognise.
        """
        attempts = list(getattr(exc, "attempts", []))
        all_silent = bool(attempts) and all(
            "silent stream" in (a.error_detail or "") for a in attempts
        )
        if all_silent:
            worst = min(
                (
                    _extract_peak_db(a.error_detail)
                    for a in attempts
                    if "silent stream" in (a.error_detail or "")
                ),
                default=_RMS_FLOOR_DB,
            )
            msg = (
                f"Input stream opened on device={entry.index!r} "
                f"(host_api={entry.host_api_name!r}) but every variant delivered only silence "
                f"(peak RMS {worst:.1f} dBFS < threshold {_VALIDATION_MIN_RMS_DB:.1f} dBFS)."
            )
            raise CaptureSilenceError(
                msg,
                device=entry.index,
                host_api=entry.host_api_name,
                observed_peak_rms_db=worst,
            ) from exc

        # T7 — session-manager contention (Linux). See
        # :func:`_is_session_manager_contention_pattern` for the rule.
        if _is_session_manager_contention_pattern(
            platform=sys.platform,
            open_attempts=attempts,
        ):
            suggested = _suggest_session_manager_alternatives()
            msg = (
                f"Every attempt on device={entry.index!r} "
                f"(host_api={entry.host_api_name!r}) failed with a device-busy error "
                "— another audio client (likely PipeWire or PulseAudio) is holding this "
                "device. Try selecting the 'pipewire' or 'default' PCM instead."
            )
            logger.error(
                "audio_capture_device_contended",
                device=entry.index,
                host_api=entry.host_api_name,
                suggested_actions=suggested,
                attempt_count=len(attempts),
            )
            raise CaptureDeviceContendedError(
                msg,
                device=entry.index,
                host_api=entry.host_api_name,
                suggested_actions=suggested,
                attempts=attempts,
            ) from exc

        runtime = RuntimeError(str(exc))
        runtime.code = getattr(exc, "code", None)  # type: ignore[attr-defined]
        runtime.attempts = attempts  # type: ignore[attr-defined]
        raise runtime from exc

    async def stop(self) -> None:
        """Cancel the consumer task and close the stream."""
        if not self._running:
            return
        self._running = False
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None
        await asyncio.to_thread(self._close_stream, "shutdown")
        # Drop any in-flight frames — they are stale once stopped.
        while not self._queue.empty():
            self._queue.get_nowait()
        logger.info("audio_capture_task_stopped")

    async def _validate_stream(self) -> float:
        """Observe the freshly-opened stream for up to ``_VALIDATION_S`` seconds.

        Drains any residual frames left over from a previous pyramid
        variant, then observes the fresh callback for up to
        ``capture_validation_seconds``. Behaviour branches on
        :attr:`VoiceTuningConfig.capture_validation_require_signal`:

        * When ``False`` (default): returns ``0.0`` dBFS as soon as
          :attr:`~VoiceTuningConfig.capture_validation_min_frames` frames
          have arrived — proving the PortAudio callback is live without
          demanding the user speak. If the stream is truly dead (callback
          never fires), the deadline expires and the floor value is
          returned, which trips the opener's silence fallback.
        * When ``True``: measures the peak per-frame RMS and short-circuits
          the moment it crosses ``capture_validation_min_rms_db``. Retains
          the legacy diagnostic semantics used by the setup-wizard.
        """
        # Drain stale frames from any previously rejected variant — the
        # queue is shared across pyramid iterations.
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        require_signal = tuning.capture_validation_require_signal
        min_frames = max(1, tuning.capture_validation_min_frames)
        min_rms_db = tuning.capture_validation_min_rms_db
        deadline = time.monotonic() + tuning.capture_validation_seconds

        peak_db = _RMS_FLOOR_DB
        frames_seen = 0
        while time.monotonic() < deadline:
            timeout = max(deadline - time.monotonic(), 0.05)
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                break
            frames_seen += 1
            db = _rms_db_int16(frame)
            peak_db = max(peak_db, db)
            if require_signal:
                if peak_db >= min_rms_db:
                    return peak_db
            elif frames_seen >= min_frames:
                # Callback is alive — return a value far above any threshold
                # so the opener accepts this variant irrespective of the
                # ambient signal level.
                return 0.0
        return peak_db

    # -- Internals ------------------------------------------------------------

    def _ensure_endpoint_guid(self, entry: DeviceEntry | None) -> None:
        """Populate :attr:`_endpoint_guid` from ``entry`` if still unset.

        Uses the same
        :func:`~sovyx.voice.health._factory_integration.derive_endpoint_guid`
        the cascade + ComboStore use, so the GUID the coordinator keys
        on matches the GUID already persisted on disk. Idempotent: an
        explicit value passed through the constructor is preserved.
        """
        if self._endpoint_guid:
            return
        if entry is None:
            return
        from sovyx.voice.health._factory_integration import derive_endpoint_guid

        self._endpoint_guid = derive_endpoint_guid(entry)
