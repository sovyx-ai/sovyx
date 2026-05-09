"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger
from sovyx.observability.saga import SagaHandle, begin_saga, end_saga
from sovyx.observability.tasks import spawn
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._speaker_consistency import (
    SpeakerConsistencyMonitor,
    compute_spectral_centroid,
)
from sovyx.voice.health._metrics import (
    record_time_to_first_utterance,
)
from sovyx.voice.jarvis import JarvisConfig, JarvisIllusion, split_at_boundaries
from sovyx.voice.pipeline._barge_in import BargeInDetector
from sovyx.voice.pipeline._bypass_coordinator_mixin import BypassCoordinatorMixin
from sovyx.voice.pipeline._config import VoicePipelineConfig, validate_config
from sovyx.voice.pipeline._events import (
    BargeInEvent,
    PipelineErrorEvent,
    SpeechEndedEvent,
    SpeechStartedEvent,
    TranscriptionCompletedEvent,
    TTSCompletedEvent,
    TTSStartedEvent,
    WakeWordDetectedEvent,
)
from sovyx.voice.pipeline._frame_recording_mixin import FrameRecordingMixin
from sovyx.voice.pipeline._frame_types import (
    BargeInInterruptionFrame,
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401 — back-compat re-exports for test sites that read the heartbeat constants via ``orch_mod._HEARTBEAT_INTERVAL_S`` etc. (anti-pattern #20)
    _DEAF_MIN_FRAMES as _DEAF_MIN_FRAMES,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _DEAF_VAD_MAX_THRESHOLD as _DEAF_VAD_MAX_THRESHOLD,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _HEARTBEAT_INTERVAL_S as _HEARTBEAT_INTERVAL_S,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _NOISE_FLOOR_DRIFT_ALERT_ENABLED as _NOISE_FLOOR_DRIFT_ALERT_ENABLED,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS as _NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _NOISE_FLOOR_DRIFT_THRESHOLD_DB as _NOISE_FLOOR_DRIFT_THRESHOLD_DB,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS as _SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _SNR_LOW_ALERT_ENABLED as _SNR_LOW_ALERT_ENABLED,
)
from sovyx.voice.pipeline._heartbeat_mixin import (  # noqa: F401
    _SNR_LOW_ALERT_THRESHOLD_DB as _SNR_LOW_ALERT_THRESHOLD_DB,
)
from sovyx.voice.pipeline._heartbeat_mixin import HeartbeatMixin
from sovyx.voice.pipeline._listener_wireup_mixin import ListenerWireupMixin
from sovyx.voice.pipeline._output_queue import AudioOutputQueue
from sovyx.voice.pipeline._public_accessors_mixin import PublicAccessorsMixin
from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import PipelineStateMachine
from sovyx.voice.pipeline._utterance_id_mixin import UtteranceIdentityMixin
from sovyx.voice.pipeline._wake_word_mixin import WakeWordRouterMixin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.events import EventBus
    from sovyx.voice._mm_notification_client import MMNotificationListener
    from sovyx.voice._wake_word_router import WakeWordRouter
    from sovyx.voice.health._driver_update_listener_win import DriverUpdateListener
    from sovyx.voice.health._self_feedback import SelfFeedbackGate
    from sovyx.voice.health.contract import BypassOutcome
    from sovyx.voice.stt import STTEngine
    from sovyx.voice.tts_piper import TTSEngine
    from sovyx.voice.vad import SileroVAD, VADEvent
    from sovyx.voice.wake_word import WakeWordDetector

logger = get_logger(__name__)

# Pipeline tuning constants (timing/frame thresholds).
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32ms at 16kHz
_SILENCE_FRAMES_END = 22  # ~700ms silence -> end of utterance
_MAX_RECORDING_FRAMES = 312  # ~10s max recording
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160ms sustained speech -> barge-in
_FILLER_DELAY_MS = 800  # Play filler if no LLM token within this
_TEXT_MIN_WORDS = 3  # Min words before TTS synthesis
# Heartbeat-specific tuning constants moved to ``_heartbeat_mixin.py``
# alongside the methods that consume them (anti-pattern #16 split).
# ``_DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY`` stays here because it is
# read by the bypass-coordinator path (``_maybe_trigger_bypass_coordinator``
# / ``_invoke_deaf_signal``) which lives on the host.
_DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY = _VoiceTuning().deaf_warnings_before_exclusive_retry

_AGC2_VAD_FEEDBACK_ENABLED = _VoiceTuning().voice_agc2_vad_feedback_enabled
"""Phase 4 / T4.52 — when True, every VAD inference publishes its
verdict to :mod:`sovyx.voice.health._vad_feedback` so AGC2's next
frame can gate the speech-level estimator update on the result."""

# ---------------------------------------------------------------------------
# O3 frame-drop detection tuning
# ---------------------------------------------------------------------------
#
# Pre-O3 the pipeline only checked a relative ``gap > 2× expected``
# threshold. That hides the "all frames consistently late" failure mode:
# if every frame arrives at 1.5× the expected interval (e.g., system
# load, USB renegotiation backpressure, capture-thread priority drift),
# the relative check NEVER fires even though cumulative latency is
# audibly degrading the response loop. O3 keeps the per-frame absolute
# budget (perceptually meaningful — anything beyond 64 ms is audible in
# a real-time voice loop regardless of nominal frame size) and adds a
# rolling-window cumulative-drift detector so sustained-degradation
# conditions surface independently of any single-frame violation.

_FRAME_DROP_ABSOLUTE_BUDGET_S = _VoiceTuning().pipeline_frame_drop_absolute_budget_seconds
"""Absolute per-frame inter-arrival budget — see
``VoiceTuningConfig.pipeline_frame_drop_absolute_budget_seconds``
for the canonical schema with bound-validators. Module-level
binding captures the value at import for the per-frame hot path."""

_FRAME_DROP_DRIFT_WINDOW_FRAMES = _VoiceTuning().pipeline_frame_drop_drift_window_frames
"""Rolling window for the cumulative-drift detector — see
``VoiceTuningConfig.pipeline_frame_drop_drift_window_frames``."""

_FRAME_DROP_DRIFT_RATIO = _VoiceTuning().pipeline_frame_drop_drift_ratio
"""Cumulative-drift firing threshold — see
``VoiceTuningConfig.pipeline_frame_drop_drift_ratio``."""

_FRAME_DROP_DRIFT_RATE_LIMIT_S = _VoiceTuning().pipeline_frame_drop_drift_rate_limit_seconds
"""Minimum gap between cumulative-drift emissions — see
``VoiceTuningConfig.pipeline_frame_drop_drift_rate_limit_seconds``."""


# ── Band-aid #50 — VAD inference timeout guard ──────────────────────
#
# Pre-band-aid #50: ``feed_frame`` awaited
# ``asyncio.to_thread(self._vad.process_frame, ...)`` with no timeout.
# A pathologically slow VAD inference (ONNX runtime stall, CPU
# thrashing under contention, kernel paging) would hang the entire
# consumer loop on that one frame — every subsequent frame from the
# capture queue stays buffered, the queue fills, frame drops cascade.
# The existing O3 detector observes the symptom (frame drops) but
# not the source (VAD-specific latency).
#
# Spec (F1 #50): "Timeout-guard VAD". The fix wraps the VAD await
# in ``asyncio.wait_for`` with a generous ceiling (250 ms = ~10×
# typical Silero VAD latency on a modern CPU). On timeout:
#   * Emit ``voice.vad.inference_timeout`` WARN with frame metadata
#     so operators get DIRECT attribution (vs. the downstream O3
#     "frames are being dropped, cause unknown").
#   * Skip this frame's VAD result; subsequent frames are unaffected.
#     The ``to_thread`` worker keeps running — its result is silently
#     discarded — but the next ``feed_frame`` call proceeds with a
#     fresh inference rather than waiting for the wedged one.
#   * Rate-limit the WARN per ``_VAD_INFERENCE_TIMEOUT_WARN_INTERVAL_S``
#     so a sustained slow-VAD condition produces a drumbeat, not a
#     flood (matches the band-aid #9 pattern for sustained-underrun).
_VAD_INFERENCE_TIMEOUT_S = _VoiceTuning().pipeline_vad_inference_timeout_seconds
"""Per-frame VAD inference budget — see
``VoiceTuningConfig.pipeline_vad_inference_timeout_seconds``."""

_VAD_INFERENCE_TIMEOUT_WARN_INTERVAL_S = (
    _VoiceTuning().pipeline_vad_inference_timeout_warn_interval_seconds
)
"""Rate-limit window for ``voice.vad.inference_timeout`` WARN logs
— see
``VoiceTuningConfig.pipeline_vad_inference_timeout_warn_interval_seconds``."""


# ---------------------------------------------------------------------------
# T1 Ring 5 atomic cancellation chain
# ---------------------------------------------------------------------------
#
# Pre-T1 the barge-in path stopped the output queue but left in-flight
# TTS synthesis running (wasted CPU + the next chunk would still appear)
# and didn't signal upstream LLM token streams to stop. The user heard
# silence after barge-in but the next turn would start mid-old-thought
# because the LLM kept producing tokens. T1 introduces a transactional
# four-step cancellation chain executed under a single asyncio.Lock so
# concurrent barge-ins serialise, and per-step success/failure is
# surfaced on a structured ``voice.tts.cancellation_chain`` event for
# dashboard attribution.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.4, T1.

_CANCELLATION_TASK_TIMEOUT_S = _VoiceTuning().pipeline_cancellation_task_timeout_seconds
"""T1 atomic-cancellation chain — per-task timeout for cancelled
in-flight TTS tasks. See
``VoiceTuningConfig.pipeline_cancellation_task_timeout_seconds``."""


_CONSECUTIVE_TTS_FAILURE_THRESHOLD = _VoiceTuning().pipeline_consecutive_tts_failure_threshold
"""Mission Phase 1 / T1.21 — streaming TTS abort threshold. See
``VoiceTuningConfig.pipeline_consecutive_tts_failure_threshold`` for
the canonical schema with bound-validators."""


# ``_COORDINATOR_PENDING_TIMEOUT_S`` moved to ``_bypass_coordinator_mixin.py``
# alongside the methods that consume it (T1.14 watchdog deadline). Tests
# that monkeypatch this constant target the bypass mixin module directly.

_SPEAKER_CONSISTENCY_ENABLED = _VoiceTuning().pipeline_speaker_consistency_enabled
"""T1.39 — gate for the spectral-centroid drift detector. See
``VoiceTuningConfig.pipeline_speaker_consistency_enabled``."""

_SPEAKER_DRIFT_WINDOW_SIZE = _VoiceTuning().pipeline_speaker_drift_window_size
"""T1.39 — rolling-window size. See
``VoiceTuningConfig.pipeline_speaker_drift_window_size``."""

_SPEAKER_DRIFT_RATIO_THRESHOLD = _VoiceTuning().pipeline_speaker_drift_ratio_threshold
"""T1.39 — relative-drift threshold. See
``VoiceTuningConfig.pipeline_speaker_drift_ratio_threshold``."""


class VoicePipeline(
    PublicAccessorsMixin,
    BypassCoordinatorMixin,
    FrameRecordingMixin,
    ListenerWireupMixin,
    UtteranceIdentityMixin,
    WakeWordRouterMixin,
    HeartbeatMixin,
):
    """Orchestrates the complete voice pipeline.

    Lifecycle (SPE-010 §8):
        1. Start audio capture (external — frames fed via :meth:`feed_frame`)
        2. Run VAD on every frame
        3. When speech detected → run wake word detector
        4. When wake word detected → beep → start recording utterance
        5. When speech ends (silence) → STT
        6. STT result → invoke ``on_perception`` callback
        7. Response text → Jarvis Illusion → TTS → speaker

    The pipeline does **not** own audio capture or the event loop.
    Frames are pushed in via :meth:`feed_frame` (pull-based designs
    require hardware; push-based is testable).

    Args:
        config: Pipeline configuration.
        vad: Voice activity detector.
        wake_word: Wake word detector.
        stt: Speech-to-text engine.
        tts: Text-to-speech engine.
        event_bus: System event bus for emitting voice events.
        on_perception: Callback invoked with transcribed text.
    """

    def __init__(
        self,
        config: VoicePipelineConfig,
        vad: SileroVAD,
        wake_word: WakeWordDetector,
        stt: STTEngine,
        tts: TTSEngine,
        event_bus: EventBus | None = None,
        on_perception: Callable[[str, str], Awaitable[None]] | None = None,
        on_deaf_signal: Callable[[], Awaitable[Sequence[BypassOutcome]]] | None = None,
        *,
        voice_clarity_active: bool = False,
        auto_bypass_enabled: bool = False,
        auto_bypass_threshold: int = _DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY,
        self_feedback_gate: SelfFeedbackGate | None = None,
        mm_notification_listener_enabled: bool = False,
        audio_driver_update_listener_enabled: bool = False,
        audio_driver_update_recascade_enabled: bool = False,
        wake_word_router: WakeWordRouter | None = None,
    ) -> None:
        validate_config(config)
        self._config = config
        self._vad = vad
        self._wake_word = wake_word
        self._stt = stt
        self._tts = tts
        self._event_bus = event_bus
        self._on_perception = on_perception
        self._on_deaf_signal = on_deaf_signal
        # Phase 8 / T8.10 — multi-mind wake-word routing. When set, the
        # IDLE path routes detection through the router instead of the
        # single ``self._wake_word`` instance. Default ``None`` preserves
        # v0.30.0 single-mind behaviour bit-exactly. Operators wire the
        # router by registering one detector per ENABLED mind via
        # :meth:`WakeWordRouter.register_mind` before passing it to
        # the orchestrator. The matched mind_id flows through to
        # :class:`WakeWordDetectedEvent` so downstream cognitive
        # dispatch can switch context within the documented ≤ 50 ms
        # gate (the dispatch wall clock is recorded in the
        # :data:`sovyx.voice.wake_word.router.dispatch_latency`
        # histogram emitted at every router-driven detection).
        self._wake_word_router = wake_word_router
        # Per-turn matched mind_id — defaults to the orchestrator's
        # config mind_id (single-mind backward-compat); router match
        # overrides per turn; reset on IDLE return.
        self._current_mind_id = config.mind_id

        # v0.32.3 Phase 3.B.1 — track the THINKING phase entry so
        # :class:`LLMFullResponseEndFrame` can carry an accurate
        # ``elapsed_ms`` field at the THINKING → SPEAKING transition.
        # Set at the IDLE/RECORDING → THINKING boundary (the same
        # site that emits :class:`LLMFullResponseStartFrame`); cleared
        # after the End frame fires at ``speak()`` / ``flush_stream()``.
        # ``None`` for the proactive-speak path (cogloop calling
        # ``speak()`` without a preceding wake/STT turn): in that
        # case the End frame is suppressed since the THINKING phase
        # never happened on the pipeline side.
        self._llm_thinking_start_monotonic: float | None = None

        # Mission `MISSION-voice-runtime-listener-wireup-2026-04-30.md`
        # Phase 1b — runtime listener wire-up. Captured at construction
        # time; ``start()`` reads them when building/registering
        # listeners, ``stop()`` reads ``self._listeners`` to unregister.
        # Per the mission's failure-isolation contract, each listener
        # registers in its own try/except so one failing doesn't block
        # the other; failed registrations are NOT appended to the list,
        # so a partial-success start has a partial-listener teardown.
        self._mm_notification_listener_enabled = mm_notification_listener_enabled
        self._audio_driver_update_listener_enabled = audio_driver_update_listener_enabled
        self._audio_driver_update_recascade_enabled = audio_driver_update_recascade_enabled
        self._listeners: list[MMNotificationListener | DriverUpdateListener] = []

        # Phase 1 APO-bypass context. ``voice_clarity_active`` is the
        # boot-time hint from :mod:`sovyx.voice._apo_detector` — retained
        # purely for logs / dashboard attribution (so operators can tell
        # a Voice Clarity-driven bypass from a generic OS-agnostic one).
        # ``auto_bypass_enabled`` is the master kill switch
        # (``VoiceTuningConfig.voice_clarity_autofix``).
        # ``auto_bypass_threshold`` is the number of back-to-back deaf
        # heartbeats required before the orchestrator invokes
        # ``on_deaf_signal`` — the callback delegates to the
        # :class:`~sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`,
        # which owns its own ``is_resolved`` latch. Once a non-empty
        # outcome list comes back we set :attr:`_coordinator_terminated`
        # so subsequent deaf warnings don't re-enter the coordinator.
        self._voice_clarity_active = voice_clarity_active
        self._auto_bypass_enabled = auto_bypass_enabled
        self._auto_bypass_threshold = max(1, auto_bypass_threshold)
        self._deaf_warnings_consecutive = 0
        self._coordinator_terminated = False
        self._coordinator_invocation_pending = False
        # T1.14 — monotonic invocation counter used by the watchdog
        # (``_reset_coordinator_pending_after_timeout``) to distinguish
        # "my invocation is wedged" from "a SUBSEQUENT invocation
        # legitimately set the flag". A fired-late watchdog whose
        # captured count differs from the live count must NOT clear
        # the flag — it belongs to a newer invocation.
        self._coordinator_invocation_count = 0
        # O2 (Ring 6 atomic deaf-signal handling): explicit asyncio.Lock
        # serialises the deaf-signal flow. The pre-O2 design relied on
        # asyncio's single-threaded execution + a sync flag check-and-set
        # in :meth:`_maybe_trigger_bypass_coordinator` to prevent double
        # spawn — defensible but fragile (any future ``await`` introduced
        # into the sync path silently breaks the invariant). Wrapping
        # the entire ``_invoke_deaf_signal`` flow in a Lock makes the
        # critical-section contract explicit and refactor-resistant. The
        # lock is also where the counter snapshot + reset happen, which
        # eliminates the tight-retry loop the previous implementation
        # exhibited when the coordinator returned empty ``outcomes``: the
        # counter would have accumulated during the ``await callback()``
        # window and immediately re-cross the threshold on the next
        # heartbeat. See MISSION-voice-mixer-enterprise-refactor §3.6
        # and the O2 task for the full rationale.
        self._coordinator_lock = asyncio.Lock()
        self._coordinator_dedup_count = 0
        """Number of times the lock-protected re-validation rejected a
        spawned invocation because the world had changed since the sync
        guard fired (terminal latch set by a concurrent task, threshold
        no longer met after a healthy heartbeat). Surfaced via the
        ``voice.deaf.coordinator_invocation_deduplicated`` event for
        operator observability — non-zero means the lock is doing real
        work, which validates the defense-in-depth pattern even when
        asyncio's single-threaded model would technically suffice."""

        # Self-feedback isolation (ADR §4.4.6). Structural half-duplex
        # gating is encoded directly in the state machine (wake-word
        # only runs in IDLE, barge-in only in SPEAKING with a 5-frame
        # sustained threshold); this optional component adds mic
        # ducking around TTS. ``None`` means the factory didn't wire
        # a gate (tests, push-to-talk fallback) — the pipeline still
        # works, it just lacks the ducking layer.
        self._self_feedback_gate = self_feedback_gate

        # State — initialized via the backing attribute directly so the
        # _state property setter doesn't fire for the first IDLE assignment
        # (no saga to open or close at construction time).
        self._voice_saga: SagaHandle | None = None
        self._state_value: VoicePipelineState = VoicePipelineState.IDLE
        # O1 — observability-grade transition validator + dwell watchdog.
        # Lenient mode (strict=False, the default) means invalid
        # transitions log a structured WARN
        # (``pipeline.state.invalid_transition``) but do NOT raise —
        # zero behavioural risk for adoption. Once the orchestrator's
        # transition sites are audited and the canonical table is
        # confirmed exhaustive, a follow-up commit can flip to
        # ``strict=True`` for hard enforcement.
        self._state_machine = PipelineStateMachine()
        # TS3 chaos injector — opt-in invalid-transition simulation
        # at the PIPELINE_INVALID_TRANSITION site. Disabled by
        # default; chaos test matrix sets the env vars to validate
        # that the O1 PipelineStateMachine WARN
        # (pipeline.state.invalid_transition) fires correctly when
        # a forbidden transition is observed. The actual orchestrator
        # state remains intact — chaos only exercises the validator
        # via a synthetic record_transition(IDLE, THINKING) call.
        self._pipeline_chaos = ChaosInjector(
            site_id=ChaosSite.PIPELINE_INVALID_TRANSITION.value,
        )
        self._utterance_frames: list[npt.NDArray[np.int16]] = []
        self._silence_counter = 0
        self._recording_counter = 0
        self._text_buffer = ""

        # Sub-components
        self._output = AudioOutputQueue()
        jarvis_cfg = JarvisConfig(
            fillers_enabled=config.fillers_enabled,
            filler_delay_ms=config.filler_delay_ms,
            confirmation_tone=config.confirmation_tone,
        )
        self._jarvis = JarvisIllusion(jarvis_cfg, tts)
        self._barge_in = BargeInDetector(vad, self._output, config.barge_in_threshold)

        # Tasks
        self._filler_task: asyncio.Task[bool] | None = None
        self._first_token_event = asyncio.Event()
        self._running = False

        # ── T1 atomic cancellation chain state ───────────────────────
        # In-flight TTS synthesis tasks tracked here so the barge-in
        # path can cancel them transactionally (not just stop the
        # output queue). Each ``speak`` / ``stream_text`` /
        # ``flush_stream`` call wraps its TTS work in a
        # ``create_task`` and registers the task into this set; the
        # task removes itself in its ``finally``. ``cancel_speech_chain``
        # iterates and cancels every entry under
        # :attr:`_cancellation_lock` so concurrent barge-ins serialise.
        self._in_flight_tts_tasks: set[asyncio.Task[Any]] = set()
        # T1.13 — guard ``_in_flight_tts_tasks`` mutations + the
        # cancel-chain snapshot. Pre-T1.13 the set was mutated via bare
        # ``.add()`` / ``.discard()`` calls; CPython's GIL makes those
        # atomic at HEAD, but a future refactor that introduced an
        # await between read-and-write inside the mutation path would
        # silently lose atomicity. The lock makes the contract
        # explicit. Snapshot at ``cancel_speech_chain`` step 2 also
        # acquires briefly so a concurrent ``_track_tts_task`` can't
        # land mid-snapshot. The iteration over the snapshot runs
        # OUTSIDE the lock — see the residual-race note in
        # ``cancel_speech_chain``'s docstring.
        self._task_tracking_lock = asyncio.Lock()
        self._cancellation_lock = asyncio.Lock()
        # Optional upstream LLM cancellation hook. Cognitive layer
        # registers an awaitable that signals the LLM client to stop
        # generating; without it, barge-in still cancels output + TTS
        # but the LLM keeps producing tokens that flow into the next
        # turn (the pre-T1 silent failure mode).
        self._llm_cancel_hook: Callable[[], Awaitable[None]] | None = None
        # v0.31.7 T3.2 (M5) — cogloop bridge tasks tracked here so the
        # cancel_speech_chain has a fallback path when ``_llm_cancel_hook``
        # is None (CR1 race window — closed in T1.1, but belt-and-
        # suspenders defends against any future regression). Each
        # ``dashboard/routes/voice.py::_on_perception`` task registers
        # itself via :meth:`register_cogloop_task`; the task removes
        # itself on completion via the done-callback wired up there.
        # Step 2.5 of :meth:`cancel_speech_chain` cancels every task in
        # this set so the bridge tears down cleanly even when the
        # upstream LLM hook is unwired.
        self._in_flight_cogloop_tasks: set[asyncio.Task[Any]] = set()

        # Observability — feed_frame updates these on every frame so
        # _maybe_emit_heartbeat can answer "is VAD seeing real audio"
        # without per-frame log spam. Reset after each heartbeat.
        self._max_vad_prob_since_heartbeat: float = 0.0
        self._vad_frames_since_heartbeat: int = 0
        self._last_heartbeat_monotonic: float = 0.0

        # v0.31.7 CR3 — heartbeat decoupling from feed_frame.
        #
        # Pre-CR3 the heartbeat fired ONLY from inside
        # ``_track_vad_for_heartbeat``, which was called from
        # ``feed_frame``. ``feed_frame`` is awaited serially by the
        # capture-side ``_consume_loop`` — one frame at a time. When
        # ``_handle_recording → _end_recording → await stt.transcribe``
        # parked on STT (Moonshine ONNX, 200-2000 ms) or
        # ``_on_perception → bridge.process`` parked on the LLM
        # (1-30 s), no further frames were drained from the queue, so
        # ``voice_pipeline_heartbeat`` STOPPED for that whole window.
        # Operators interpreted a healthy pipeline as wedged.
        #
        # CR3 fix: emit on a wall-clock timer regardless of consumer-
        # loop progress. Per-frame ``_track_vad_for_heartbeat``
        # continues to update the window stats below — the snapshot
        # fields plus ``_max_vad_prob_since_heartbeat`` /
        # ``_vad_frames_since_heartbeat`` — and a background
        # ``_heartbeat_loop`` task spawned from :meth:`start` calls
        # :meth:`_emit_heartbeat` every ``_HEARTBEAT_INTERVAL_S``.
        # The per-frame call site keeps a back-compat interval check
        # so a frame arriving exactly when the window expires still
        # triggers an emission — both triggers converge on the same
        # idempotent :meth:`_emit_heartbeat` body.
        #
        # Snapshot fields below let the timer-driven emission carry
        # the freshest VAD probability observation even when the
        # window-max happens to be stale (e.g. STT parked for 5 s,
        # the timer fires 2.5x and reports the snapshot from the last
        # frame fed before parking).
        self._last_vad_probability_snapshot: float = 0.0
        self._last_vad_probability_snapshot_at: float = 0.0
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Phase 4 / T4.35 — SNR low-alert de-flap counter. Counts
        # consecutive heartbeats whose drained SNR p50 sits below
        # the configured floor; once it reaches the consecutive-
        # heartbeats tuning, a single structured WARN fires +
        # counter increments. Resets to zero on the first clean
        # heartbeat (mirrors the deaf-warning pattern).
        self._snr_low_consecutive_heartbeats: int = 0
        self._snr_low_alert_active: bool = False

        # Phase 4 / T4.38 — noise-floor drift de-flap counter +
        # latch. Same pattern as the SNR low-alert above.
        self._noise_floor_drift_consecutive_heartbeats: int = 0
        self._noise_floor_drift_alert_active: bool = False

        # §5.14 KPI — time-to-first-utterance. Wall-clock captured at
        # WakeWordDetectedEvent emission, measured at SpeechStartedEvent
        # emission. Only the wake-word path contributes (barge-in uses a
        # different SpeechStartedEvent site in _transition_to_recording).
        self._wake_detected_monotonic: float | None = None

        # T1.39 — spectral-centroid drift detector. Per-pipeline state
        # so each pipeline instance has its own rolling window;
        # ``reset()`` is called at every WAKE_DETECTED transition so a
        # voice swap across sessions doesn't false-trigger on the first
        # chunk of the new session. ``None`` when the gate is disabled
        # via ``pipeline_speaker_consistency_enabled=False`` —
        # downstream call sites guard with ``is not None`` so the
        # disabled path is fully cost-free.
        self._speaker_consistency: SpeakerConsistencyMonitor | None = (
            SpeakerConsistencyMonitor(
                window_size=_SPEAKER_DRIFT_WINDOW_SIZE,
                drift_ratio_threshold=_SPEAKER_DRIFT_RATIO_THRESHOLD,
            )
            if _SPEAKER_CONSISTENCY_ENABLED
            else None
        )

        # Frame inter-arrival tracking. O3 splits the pre-existing
        # single relative threshold into two complementary detectors:
        # an ABSOLUTE per-frame budget (catastrophic single-frame
        # stalls — USB renegotiation, capture-thread priority loss)
        # and a ROLLING-WINDOW cumulative-drift check (the failure mode
        # the pre-O3 relative threshold silently masked: if every frame
        # arrives 1.5× late the relative check never fires while
        # cumulative latency degrades audibly). The drift window is
        # bounded so memory stays constant for long-running daemons.
        from collections import deque

        self._last_frame_monotonic: float | None = None
        self._expected_frame_interval_s: float = _FRAME_SAMPLES / _SAMPLE_RATE
        self._recent_frame_intervals: deque[float] = deque(
            maxlen=_FRAME_DROP_DRIFT_WINDOW_FRAMES,
        )
        self._last_drift_warning_monotonic: float | None = None

        # Band-aid #50 — VAD inference timeout-guard state. Lifetime
        # cumulative count of timed-out VAD inferences (visible via
        # :attr:`vad_inference_timeout_count`); rate-limit timestamp
        # for the WARN.
        self._vad_inference_timeouts: int = 0
        self._last_vad_timeout_warning_monotonic: float | None = None

        # Mission Phase 1 / T1.21 — consecutive per-segment TTS
        # failure counter for the streaming path. Reset on any
        # successful synthesize-and-enqueue; triggers an abort when
        # it crosses :data:`_CONSECUTIVE_TTS_FAILURE_THRESHOLD`.
        self._consecutive_tts_segment_failures: int = 0
        # v0.31.7 T3.5 (LOW.4) — lock around all read+write to the
        # counter above. CPython's GIL makes a single ``+= 1`` atomic
        # at HEAD, but a future refactor that introduced parallel
        # ``stream_text`` calls (e.g. multi-mind voice in v0.32+) would
        # silently lose atomicity at the read-modify-write boundary,
        # producing stuck-counters or premature aborts. The lock makes
        # the contract explicit + survives the refactor.
        self._tts_segment_failure_lock = asyncio.Lock()

        # Mission Phase 1 / T1.18 — frame-drop counter for the
        # ``not_running`` early-return path in :meth:`feed_frame`.
        # Pre-T1.18 frames arriving while ``_running`` was False were
        # silently discarded — a stale audio producer (capture task
        # not yet stopped or restarted out-of-order) could push frames
        # at 50 Hz with zero observability. The counter accumulates
        # over a single pipeline lifetime; a structured
        # ``voice.pipeline.frame_dropped_not_running`` event fires
        # rate-limited once per
        # :data:`_FRAME_DROP_DRIFT_RATE_LIMIT_S` window so dashboards
        # see the misuse without log spam.
        self._frames_dropped_not_running: int = 0
        self._last_frame_drop_warning_monotonic: float | None = None

        # Band-aid #46 — false-wake rejection counter. Lifetime count
        # of utterances dropped by the STT-confidence gate (visible
        # via :attr:`false_wake_rejected_count`). Each rejection also
        # emits a structured ``voice.wake.false_positive_rejected``
        # WARN — the counter survives WARN suppression / log
        # rotation so dashboards can attribute over time.
        self._false_wake_rejected_count: int = 0

        # ── Step 13 Ring 6 frame instrumentation ────────────────────
        # Helper that stamps timestamp_monotonic + utterance_id on
        # frame emissions so call sites stay short. The state machine
        # records the frame in its bounded ring buffer; the dashboard's
        # GET /api/voice/frame-history (Step 15) exposes the snapshot.

        # ── Per-utterance trace ID (Ring 6 trace contract) ──────────
        #
        # Mission §2.6 / §9.4.6 — every event in the capture → VAD →
        # STT → LLM → TTS chain stamps the same UUID4 so dashboards
        # and log search reconstruct the full per-turn span set with
        # one filter. Minted at every utterance boundary (wake-word
        # fire, no-wake recording start, external proactive ``speak``)
        # via :func:`_mint_new_utterance_id`; cleared at every
        # terminal transition back to IDLE via
        # :func:`_clear_utterance_id`. Empty string between
        # utterances — the empty-default contract on the event
        # dataclasses keeps tests and legacy bridges that construct
        # events without a trace context working unchanged.
        self._current_utterance_id: str = ""

    # ── Step 13 Ring 6 frame instrumentation helper ─────────────────
    #
    # ``_record_frame`` + ``record_capture_restart`` extracted to
    # :class:`sovyx.voice.pipeline._frame_recording_mixin.FrameRecordingMixin`,
    # mounted via the multi-mixin host above. Methods stay accessible
    # via instance dispatch through MRO for the 11 internal callers
    # + 1 external caller (``voice/capture/_restart_mixin.py`` T32).
    # Anti-pattern #16 — Phase 5.F.22.

    def reset_coordinator_after_failover(self) -> None:
        """Clear the deaf-detection latch after a runtime device-rebind.

        Mission ``MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
        §Phase 2 T2.6. Called by
        :func:`sovyx.voice.health._runtime_failover._try_runtime_failover`
        after :meth:`AudioCaptureTask.request_device_change_restart`
        returns ``engaged=True``. Without this reset, the orchestrator
        keeps ``_coordinator_terminated=True`` (latched at
        ``_invoke_deaf_signal`` line ~2839 when the bypass coordinator
        ran out of strategies on the OLD endpoint) and the new
        endpoint never gets its own deaf-detection cycle — silent
        regression on the failover target would never re-trigger the
        coordinator.

        Resets the four counters that drive deaf detection:

        * ``_coordinator_terminated`` — latched at terminal "ineffective"
          on the previous endpoint; the new endpoint deserves a fresh
          chance to engage strategies.
        * ``_deaf_warnings_consecutive`` — accumulated deaf-warning
          count on the OLD endpoint; irrelevant for the new device.
        * ``_max_vad_prob_since_heartbeat`` /
          ``_vad_frames_since_heartbeat`` — heartbeat-window stats; the
          old window's data does not represent the new endpoint and
          would otherwise immediately re-trigger the warning at the
          next heartbeat.

        Anti-pattern #29 compliance: this is observability-aware state
        mutation (clears flags so downstream observability emits make
        sense for the new substrate). It does NOT touch the
        :class:`VoicePipelineState` machine — the authoritative
        conversational state (IDLE / RECORDING / SPEAKING / THINKING)
        is unaffected, only the deaf-detection counters that live
        alongside it.

        Safe to call from any thread (no asyncio primitives touched —
        every mutation is a primitive assignment). Best-effort: catches
        AttributeError so a future refactor that renames a counter
        won't crash the failover helper before the reset method itself
        is updated.
        """
        try:
            self._coordinator_terminated = False
            self._deaf_warnings_consecutive = 0
            self._max_vad_prob_since_heartbeat = 0.0
            self._vad_frames_since_heartbeat = 0
        except AttributeError as exc:
            logger.warning(
                "voice.failover.coordinator_reset_attribute_error",
                **{
                    "voice.error": str(exc),
                    "voice.action_required": (
                        "VoicePipeline counter renamed without updating "
                        "reset_coordinator_after_failover — see Mission "
                        "MISSION-voice-linux-silent-mic-remediation-"
                        "2026-05-04 §Phase 2 T2.6"
                    ),
                },
            )
            return
        logger.warning(
            "voice.failover.coordinator_reset",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.reason": "runtime_failover_succeeded",
            },
        )

    # -- State property + saga lifecycle ------------------------------------

    @property
    def _state(self) -> VoicePipelineState:
        """Current internal state — backed by ``_state_value``.

        Defined as a property so every assignment (``self._state = X``)
        flows through the setter, which manages the ``voice_turn`` saga
        lifecycle. Reads have zero overhead beyond an attribute lookup.
        """
        return self._state_value

    @_state.setter
    def _state(self, new: VoicePipelineState) -> None:
        old = self._state_value
        self._state_value = new
        # O1 — every state mutation flows through the validator.
        # Lenient mode: invalid transitions emit a structured WARN
        # without raising, so zero behavioural risk vs the pre-O1
        # codepath. The bounded transition history is queryable via
        # self._state_machine.history() for post-incident forensics.
        # Note: we record EVERY mutation including self-loops — the
        # canonical table allows IDLE/THINKING/SPEAKING self-loops
        # explicitly, and recording them keeps the dwell watchdog
        # accurate (each self-loop resets the dwell clock).
        self._state_machine.record_transition(old, new)
        # TS3 chaos: opt-in invalid-transition simulation. When
        # chaos fires, push a synthetic IDLE→THINKING through the
        # validator (a known-invalid edge per the canonical table).
        # Exercises the lenient-mode WARN path
        # (pipeline.state.invalid_transition) under realistic
        # operating conditions. The orchestrator's actual state
        # remains intact — only the validator's history + invalid-
        # transition counter are touched.
        if self._pipeline_chaos.should_inject():
            self._state_machine.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.THINKING,
            )
        if old is new:
            return
        if old is VoicePipelineState.IDLE and new is not VoicePipelineState.IDLE:
            self._open_voice_turn_saga()
        elif new is VoicePipelineState.IDLE and old is not VoicePipelineState.IDLE:
            self._close_voice_turn_saga()
            # Step 13 frame emission — terminal IDLE transition closes
            # the per-utterance trace. Hooking into the state setter
            # (rather than 9 separate call sites) keeps the wire-up
            # exhaustive: every path back to IDLE produces an EndFrame.
            # The reason field captures the prior state so dashboards
            # can attribute "where the trace ended".
            self._record_frame(
                EndFrame(
                    frame_type="End",
                    timestamp_monotonic=time.monotonic(),
                    reason=f"from_{old.name.lower()}",
                ),
            )

    def _open_voice_turn_saga(self) -> None:
        """Open the per-turn voice saga (called on IDLE→active transitions).

        If a previous turn's saga is somehow still open (defensive: a
        crash path that bypassed the IDLE transition), close it as
        ``saga.failed`` with a synthetic exception describing the
        anomaly before opening the new one. This keeps the dashboard's
        timeline clean rather than letting handles dangle indefinitely.
        """
        if self._voice_saga is not None:
            end_saga(
                self._voice_saga,
                exc=RuntimeError("voice_turn saga abandoned by state machine"),
            )
        self._voice_saga = begin_saga("voice_turn", kind="voice")

    def _close_voice_turn_saga(self) -> None:
        """Close the active voice turn saga (called on active→IDLE transitions).

        Idempotent — safe to call when no saga is open. The
        ``saga.completed`` entry carries the duration measured by
        :func:`begin_saga`, so the dashboard can render turn-latency
        directly from the saga lifecycle without joining other
        instrumentation.
        """
        if self._voice_saga is None:
            return
        end_saga(self._voice_saga)
        self._voice_saga = None

    # -- Properties ----------------------------------------------------------

    # ── Public read-only accessors extracted to _public_accessors_mixin.py ──
    # 13 properties + 1 method (state / config / output /
    # set_render_buffer / jarvis / is_running / frame_history /
    # vad / stt / tts / wake_word / vad_inference_timeout_count /
    # false_wake_rejected_count) now live on
    # :class:,
    # mounted via the multi-mixin host above. Pure delegators — every accessor
    # stays accessible via self.<name> through MRO; no caller-side change.
    # Anti-pattern #16 — Phase 5.F.25.


    # ── Utterance-ID identity methods extracted to ``_utterance_id_mixin.py`` ──
    # ``current_utterance_id`` (property) + ``_mint_new_utterance_id``
    # + ``_clear_utterance_id`` now live on
    # :class:`sovyx.voice.pipeline._utterance_id_mixin.UtteranceIdentityMixin`,
    # mounted via the multi-mixin host above. Methods stay resolvable
    # via ``self.current_utterance_id`` / ``self._mint_new_utterance_id()``
    # / ``self._clear_utterance_id()`` through MRO. Anti-pattern #16 —
    # Phase 5.F.21.

    # ``_notify_wake_word_false_fire`` extracted to
    # ``_wake_word_mixin.py`` (see WakeWordRouterMixin docstring +
    # the carve-out comment under the class header above). Resolves
    # via MRO; no caller-side change.

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the pipeline and pre-cache fillers.

        Call this before feeding frames. Double-start is a no-op
        — every existing in-flight task / pre-cached filler / state
        from the prior :meth:`start` is preserved and the second
        invocation logs ``voice.pipeline.start_already_running_ignored``
        so dashboards see the misuse without a crash. Mission Phase 1
        T1.11 — guards against orphaned filler tasks + duplicated
        pre-cache work that the spec's "start() called twice orphans
        first saga + tasks" finding documented.
        """
        if self._running:
            logger.info(
                "voice.pipeline.start_already_running_ignored",
                mind_id=self._config.mind_id,
                state=self._state.name,
            )
            return
        await self._jarvis.pre_cache()
        self._running = True
        self._state = VoicePipelineState.IDLE
        self._last_heartbeat_monotonic = time.monotonic()

        # v0.31.7 CR3 — spawn the wall-clock heartbeat loop. Decouples
        # ``voice_pipeline_heartbeat`` emission from ``feed_frame`` so
        # dashboards keep seeing liveness signals during STT / LLM /
        # TTS parking (the consumer loop blocks on those awaits and
        # would otherwise stop calling per-frame heartbeat trigger).
        # See :meth:`_heartbeat_loop` for the full rationale.
        self._heartbeat_task = spawn(
            self._heartbeat_loop(),
            name="voice-pipeline-heartbeat",
        )

        # Mission Phase 1b — register runtime listeners (MM notification
        # + driver-update). Each listener registers in its own
        # try/except via ``_register_listeners`` so one failing doesn't
        # block the other. Failed registrations are not added to
        # ``self._listeners`` so the symmetric ``_unregister_listeners``
        # in ``stop()`` only sees successful registrations.
        self._register_listeners()

        logger.info(
            "VoicePipeline started",
            mind_id=self._config.mind_id,
            wake_word=self._config.wake_word_enabled,
        )

    async def stop(self) -> None:
        """Stop the pipeline and drain in-flight work before returning.

        Mission Phase 1 T1.10 — pre-fix the call set ``_running=False``
        and returned immediately, leaving any in-flight TTS synthesis
        task to push audio onto a closed pipeline (the user heard
        stale audio after explicit stop). Post-fix sequence:

        1. Emit ``voice.pipeline.stop_begin`` so dashboards see the
           tear-down boundary.
        2. Set ``_running=False`` so :meth:`feed_frame` short-circuits
           with ``"not_running"`` for any concurrent producer.
        3. Snapshot ``_filler_task`` BEFORE :meth:`_cancel_filler`
           nulls it out, then await the cancellation with a
           ``_CANCELLATION_TASK_TIMEOUT_S`` budget.
        4. Interrupt the output queue (idempotent).
        5. Snapshot ``_in_flight_tts_tasks``, cancel each, await with
           the same per-task budget. ``CancelledError`` + ``TimeoutError``
           both count as "drained" so a wedged TTS backend doesn't
           stall :meth:`stop`; unexpected exceptions log a structured
           WARN but don't propagate.
        6. Reset state, release the self-feedback duck, emit
           ``voice.pipeline.stop_complete`` with drain counters.
        """
        logger.info("voice.pipeline.stop_begin", mind_id=self._config.mind_id)

        self._running = False

        # Snapshot the filler task BEFORE _cancel_filler() nulls it.
        filler_task = self._filler_task
        filler_was_active = filler_task is not None and not filler_task.done()
        self._cancel_filler()
        if filler_was_active and filler_task is not None:
            # CancelledError is the expected outcome of cancel();
            # TimeoutError means the filler ignored cancellation within
            # budget — tracked via filler_was_active so the
            # stop_complete log surfaces it. Both terminate the wait
            # without propagating; see AP-27 for the suppress pattern.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(filler_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
            logger.debug(
                "voice.pipeline.stop_filler_drain_attempted",
                reason="best-effort wait for filler cancellation",
            )

        # v0.31.7 CR3 — cancel + drain the wall-clock heartbeat task.
        # Same drain pattern as the filler task above: cancel, await
        # with bounded timeout, treat CancelledError + TimeoutError as
        # success (a wedged heartbeat must NOT stall pipeline stop).
        # The task self-exits when ``_running`` flips False at the
        # next sleep boundary, but we still cancel for liveness:
        # without cancel a 2 s sleep would hold stop for up to one
        # full heartbeat interval.
        heartbeat_task = self._heartbeat_task
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(heartbeat_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
        self._heartbeat_task = None

        # Interrupt active playback (idempotent).
        self._output.interrupt()

        # Snapshot the in-flight TTS set so iteration is stable while
        # tasks self-remove via _untrack_tts_task in their finally
        # blocks (same pattern as cancel_speech_chain step 2).
        #
        # T1.13 — snapshot acquires ``_task_tracking_lock`` briefly to
        # serialize against concurrent ``_track_tts_task``; iteration
        # outside the lock for the same reason as cancel_speech_chain.
        async with self._task_tracking_lock:
            tts_snapshot = tuple(self._in_flight_tts_tasks)
        tts_drained = 0
        for task in tts_snapshot:
            if task.done():
                tts_drained += 1
                continue
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
                tts_drained += 1
            except (asyncio.CancelledError, TimeoutError):
                # Both count as drained — CancelledError is the
                # success path; TimeoutError means we asked nicely
                # within budget and the task didn't honour it, but
                # we still leave the orchestrator in a quiesced state.
                tts_drained += 1
            except Exception as exc:  # noqa: BLE001 — stop must never raise
                logger.warning(
                    "voice.pipeline.stop_tts_task_unexpected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        self._state = VoicePipelineState.IDLE
        self._utterance_frames.clear()
        if self._self_feedback_gate is not None:
            # Release the duck so a mid-TTS stop doesn't leave the
            # capture normalizer attenuated for the next session.
            self._self_feedback_gate.on_tts_end()

        # Mission Phase 1b — unregister runtime listeners. Each
        # unregister is best-effort + logged on failure so a wedged
        # WMI service or COM marshalling glitch doesn't block pipeline
        # shutdown.
        self._unregister_listeners()

        logger.info(
            "voice.pipeline.stop_complete",
            mind_id=self._config.mind_id,
            tts_tasks_drained=tts_drained,
            tts_tasks_total=len(tts_snapshot),
            filler_was_active=filler_was_active,
        )
        logger.info("VoicePipeline stopped", mind_id=self._config.mind_id)

    # -- Runtime listener wire-up (mission Phase 1b) -------------------------
    #
    # The MM notification listener + driver-update listener are
    # constructed + registered here from ``start()`` and torn down
    # from ``stop()``. The contract:
    #
    # * Each listener registers in its OWN try/except. A failure to
    #   register one does NOT block the other — pipeline starts
    #   nominally with degraded device-change awareness rather than
    #   crashing.
    # * Successful registrations are appended to ``self._listeners``;
    #   ``_unregister_listeners`` iterates that list, so partial
    #   successes have partial teardowns (no attempt to unregister a
    #   listener that never registered).
    # * The listeners take an asyncio loop at construction. We capture
    #   it via ``asyncio.get_running_loop()`` inside ``start()`` (which
    #   is always called from an async context, so the loop is
    #   guaranteed to be running). If somehow there's no running loop,
    #   listener registration is skipped with a structured WARN —
    #   pipeline still works without device-change awareness.
    # * The MM listener's 3 callbacks (default-capture / device-state /
    #   property-changed) currently emit structured events ONLY. The
    #   downstream wire-up that turns these events into capture-task
    #   restart triggers is OUT OF SCOPE for Phase 1b per mission
    #   Part 4.2 — that's a follow-up commit.

    # ── Listener wire-up extracted to ``_listener_wireup_mixin.py`` ──
    # ``_register_listeners`` + ``_unregister_listeners`` +
    # ``_on_default_capture_changed`` + ``_on_device_state_changed``
    # now live on
    # :class:`sovyx.voice.pipeline._listener_wireup_mixin.ListenerWireupMixin`,
    # mounted via the multi-mixin host above. The 4 methods stay
    # accessible via instance dispatch through MRO; the 2 callbacks
    # are passed by reference to the listener factories which invoke
    # them via the bound-method form (MRO-stable). Anti-pattern #16 —
    # Phase 5.F.23.

    # -- Frame processing (main loop) ----------------------------------------

    async def feed_frame(self, frame: npt.NDArray[np.int16]) -> dict[str, Any]:
        """Process one audio frame (512 samples, 16-bit, 16kHz).

        This is the main entry point.  Call this for every audio frame
        from the microphone.  The pipeline handles state transitions,
        VAD, wake word, recording, STT, and emits events.

        Args:
            frame: Audio frame — 512 int16 samples at 16kHz.

        Returns:
            Dict with ``state`` and optional ``event`` / ``transcription`` keys.
        """
        if not self._running:
            # Mission Phase 1 / T1.18 — a frame arriving while
            # ``_running`` is False indicates a stale producer (capture
            # task not yet stopped after pipeline.stop, or restarted
            # mid-cycle out of order). Count + rate-limited structured
            # WARN so dashboards see the producer's misuse without
            # per-frame log spam (the producer can hit this path at
            # 50 Hz indefinitely).
            self._frames_dropped_not_running += 1
            now_drop = time.monotonic()
            if (
                self._last_frame_drop_warning_monotonic is None
                or now_drop - self._last_frame_drop_warning_monotonic
                >= _FRAME_DROP_DRIFT_RATE_LIMIT_S
            ):
                self._last_frame_drop_warning_monotonic = now_drop
                logger.warning(
                    "voice.pipeline.frame_dropped_not_running",
                    **{
                        "voice.mind_id": self._config.mind_id,
                        "voice.dropped_count": self._frames_dropped_not_running,
                        "voice.last_state": self._state.name,
                        "voice.action_required": (
                            "Audio producer is feeding frames after "
                            "pipeline.stop() returned. Either stop the "
                            "producer first or call pipeline.start() "
                            "before feeding."
                        ),
                    },
                )
            return {"state": self._state.name, "event": "not_running"}

        # O3 frame-drop detection — see _check_frame_drop_signals for
        # the per-frame absolute budget + rolling-window drift contract.
        now_monotonic = time.monotonic()
        self._check_frame_drop_signals(now_monotonic)
        self._last_frame_monotonic = now_monotonic

        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        # ONNX inference is CPU-bound and was blocking the event loop —
        # move it to a worker thread so the dashboard / HTTP / other async
        # tasks remain responsive while VAD runs. Wrapped in a
        # band-aid #50 timeout guard: a wedged inference doesn't stall
        # the whole audio pipeline; the frame is dropped with a
        # rate-limited WARN attributing the cause to VAD specifically
        # (vs. the downstream O3 frame-drop signal).
        try:
            vad_event = await asyncio.wait_for(
                asyncio.to_thread(self._vad.process_frame, audio_f32),
                timeout=_VAD_INFERENCE_TIMEOUT_S,
            )
        except TimeoutError:
            self._vad_inference_timeouts += 1
            now = time.monotonic()
            if (
                self._last_vad_timeout_warning_monotonic is None
                or now - self._last_vad_timeout_warning_monotonic
                >= _VAD_INFERENCE_TIMEOUT_WARN_INTERVAL_S
            ):
                self._last_vad_timeout_warning_monotonic = now
                logger.warning(
                    "voice.vad.inference_timeout",
                    **{
                        "voice.timeout_s": _VAD_INFERENCE_TIMEOUT_S,
                        "voice.lifetime_timeout_count": self._vad_inference_timeouts,
                        "voice.state": self._state.name,
                        "voice.action_required": (
                            "VAD inference exceeded the per-frame budget. "
                            "Likely causes: host CPU saturation starving the "
                            "ONNX runtime, ONNX session corruption (rerun "
                            "preflight: `sovyx doctor voice`), or kernel "
                            "memory pressure (paging). Check `top` / "
                            "Activity Monitor for CPU pinning on the Sovyx "
                            "process. Subsequent frames are unaffected — "
                            "this frame is dropped from VAD analysis."
                        ),
                    },
                )
                # Mission Phase 1 / T1.19 — emit PipelineErrorEvent so
                # dashboards see the timeout in the structured event
                # stream alongside the WARN (the WARN-only signal was
                # invisible to widgets that key off the event bus).
                # Gated on the rate-limit window so the per-frame 50 Hz
                # storm under sustained CPU pressure produces one event
                # per ``_VAD_INFERENCE_TIMEOUT_WARN_INTERVAL_S`` window
                # rather than 50 events/sec on the bus.
                await self._emit(
                    PipelineErrorEvent(
                        mind_id=self._config.mind_id,
                        error=(
                            f"vad_inference_timeout (timeout_s="
                            f"{_VAD_INFERENCE_TIMEOUT_S}, "
                            f"lifetime_count={self._vad_inference_timeouts})"
                        ),
                        utterance_id=self._current_utterance_id,
                    )
                )
            return {"state": self._state.name, "event": "vad_timeout"}

        self._track_vad_for_heartbeat(vad_event.probability)

        # Phase 4 / T4.52 — publish the VAD verdict to the
        # AGC2 feedback channel. AGC2 consumes the freshest
        # verdict on each subsequent frame's process() call,
        # gating speech-level estimator updates so ambient
        # noise above the RMS silence floor (door slams,
        # keyboard, HVAC bursts) can't pump up the gain.
        if _AGC2_VAD_FEEDBACK_ENABLED:
            from sovyx.voice.health._vad_feedback import set_last_verdict

            set_last_verdict(is_speech=vad_event.is_speech)

        if self._state == VoicePipelineState.IDLE:
            return await self._handle_idle(frame, vad_event)

        if self._state == VoicePipelineState.WAKE_DETECTED:
            return await self._handle_wake_detected(frame, vad_event)

        if self._state == VoicePipelineState.RECORDING:
            return await self._handle_recording(frame, vad_event)

        if self._state == VoicePipelineState.SPEAKING:
            return await self._handle_speaking(frame, vad_event)

        # TRANSCRIBING / THINKING — just pass through
        return {"state": self._state.name}

    # -- State handlers -------------------------------------------------------

    async def _handle_idle(
        self,
        frame: npt.NDArray[np.int16],
        vad_event: VADEvent,
    ) -> dict[str, Any]:
        """IDLE: listen for wake word (only when VAD detects speech)."""
        if not vad_event.is_speech:
            return {"state": "IDLE"}

        if not self._config.wake_word_enabled:
            # No wake word required — go straight to recording
            return await self._transition_to_recording(frame)

        # Run wake word detector — wrap ONNX inference in to_thread so the
        # detection pass does not block the loop while audio chunks queue up.
        # Phase 8 / T8.10: when ``self._wake_word_router`` is set, route
        # through the multi-mind router; otherwise fall back to the single
        # detector path (v0.30.0 backward-compat).
        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        matched_mind_id: str | None = None
        dispatch_t0 = time.monotonic()
        if self._wake_word_router is not None:
            from sovyx.voice.wake_word import (  # noqa: PLC0415 — lazy: only used on the multi-mind path
                WakeWordEvent,
                WakeWordState,
            )

            router_event = await asyncio.to_thread(
                self._wake_word_router.process_frame,
                audio_f32,
            )
            if router_event is not None:
                matched_mind_id = str(router_event.mind_id)
                ww_event = router_event.event
            else:
                # No router match → synthesize an unmatched event so the
                # downstream conditional uses a uniform shape.
                ww_event = WakeWordEvent(
                    detected=False,
                    score=0.0,
                    state=WakeWordState.IDLE,
                )
        else:
            ww_event = await asyncio.to_thread(self._wake_word.process_frame, audio_f32)

        if ww_event.detected:
            # Phase 8 / T8.10 — record router dispatch latency. Wall-clock
            # from process_frame entry to detection-confirmed boundary;
            # the v0.30.0 GA gate target is ≤ 50 ms (the cognitive layer
            # then has its own budget for switching context). Only emit
            # when the router is wired (single-detector path doesn't
            # have the multi-mind dispatch concept).
            if self._wake_word_router is not None:
                dispatch_ms = (time.monotonic() - dispatch_t0) * 1000.0
                logger.info(
                    "voice.wake_word.router.dispatch",
                    **{
                        "voice.matched_mind_id": matched_mind_id or "",
                        "voice.dispatch_ms": round(dispatch_ms, 2),
                        "voice.score": round(ww_event.score, 4),
                    },
                )
                # T04 of pre-wake-word-hardening mission (2026-05-02):
                # also record the dispatch latency as an OTel histogram
                # with the matched mind_id attribute. This makes the
                # T8.10 ≤50 ms SLA contract operator-verifiable in
                # dashboards (previously log-only — required parsing
                # ``voice.dispatch_ms`` out of structured logs).
                from sovyx.observability.metrics import get_metrics

                histogram = getattr(
                    get_metrics(),
                    "voice_wake_word_router_dispatch_latency",
                    None,
                )
                if histogram is not None:
                    histogram.record(
                        dispatch_ms,
                        attributes={"mind_id": matched_mind_id or "unknown"},
                    )
            # T8.10 — switch the per-turn mind context. Downstream
            # WakeWordDetectedEvent + STT + perception path emissions
            # carry this mind_id; reset to config.mind_id at the IDLE
            # return so the next turn starts clean.
            if matched_mind_id is not None:
                self._current_mind_id = matched_mind_id
            else:
                self._current_mind_id = self._config.mind_id
            self._state = VoicePipelineState.WAKE_DETECTED
            self._wake_detected_monotonic = time.monotonic()
            # T1.39 — reset the spectral-centroid baseline on every
            # session boundary. A new session may legitimately use a
            # different voice (operator switched mind / cognitive
            # layer picked a different persona); carrying the prior
            # session's baseline across the boundary would surface a
            # false-positive drift on the first chunk of the new
            # session. Cheap (deque.clear()); no-op when the gate
            # is disabled.
            if self._speaker_consistency is not None:
                self._speaker_consistency.reset()
            # Mission §2.6 Ring 6 — mint trace id BEFORE the first
            # event so WakeWordDetectedEvent is the head of the
            # per-utterance span set. Every downstream emission
            # (SpeechStarted, SpeechEnded, TranscriptionCompleted,
            # TTSStarted, TTSCompleted) stamps the same id until
            # _clear_utterance_id resets at the IDLE return.
            utterance_id = self._mint_new_utterance_id()
            # Step 13 frame emission — wake-word fire is the canonical
            # head of the per-utterance span. Mirrors Pipecat's
            # UserStartedSpeakingFrame contract.
            self._record_frame(
                UserStartedSpeakingFrame(
                    frame_type="UserStartedSpeaking",
                    timestamp_monotonic=time.monotonic(),
                    source="wake_word",
                ),
            )
            await self._emit(
                WakeWordDetectedEvent(
                    mind_id=self._current_mind_id,
                    utterance_id=utterance_id,
                )
            )
            logger.info(
                "Wake word detected",
                mind_id=self._current_mind_id,
                **{"voice.utterance_id": utterance_id},
            )

            # Play confirmation beep
            if self._config.confirmation_tone == "beep":
                await self._jarvis.play_beep(self._output)

            await self._emit(
                SpeechStartedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=utterance_id,
                )
            )
            if self._wake_detected_monotonic is not None:
                ttfu_ms = (time.monotonic() - self._wake_detected_monotonic) * 1000.0
                record_time_to_first_utterance(duration_ms=ttfu_ms)
                self._wake_detected_monotonic = None
            self._utterance_frames.clear()
            self._silence_counter = 0
            self._recording_counter = 0
            return {"state": "WAKE_DETECTED", "event": "wake_word_detected"}

        return {"state": "IDLE"}

    async def _handle_wake_detected(
        self,
        frame: npt.NDArray[np.int16],
        vad_event: VADEvent,
    ) -> dict[str, Any]:
        """WAKE_DETECTED: transition to recording immediately."""
        # Beep already played — start recording this frame
        self._state = VoicePipelineState.RECORDING
        self._utterance_frames = [frame]
        self._silence_counter = 0 if vad_event.is_speech else 1
        self._recording_counter = 1
        return {"state": "RECORDING", "event": "recording_started"}

    async def _handle_recording(
        self,
        frame: npt.NDArray[np.int16],
        vad_event: VADEvent,
    ) -> dict[str, Any]:
        """RECORDING: buffer audio frames until silence or timeout."""
        self._utterance_frames.append(frame)
        self._recording_counter += 1

        if vad_event.is_speech:
            self._silence_counter = 0
        else:
            self._silence_counter += 1

        # Timeout — 10s max recording
        if self._recording_counter >= self._config.max_recording_frames:
            logger.info("Recording timeout", frames=self._recording_counter)
            return await self._end_recording()

        # Silence threshold → end utterance
        if self._silence_counter >= self._config.silence_frames_end:
            return await self._end_recording()

        return {"state": "RECORDING", "frames": self._recording_counter}

    async def _handle_speaking(
        self,
        frame: npt.NDArray[np.int16],
        vad_event: VADEvent,
    ) -> dict[str, Any]:
        """SPEAKING: monitor for barge-in while TTS plays."""
        if not self._config.barge_in_enabled:
            return {"state": "SPEAKING"}

        if (
            vad_event.is_speech
            and self._output.is_playing
            and await self._barge_in.check_frame_async(frame)
        ):
            # T1 atomic cancellation chain — single transactional
            # surface replacing the pre-T1 inline cleanup. Stops
            # output, cancels in-flight TTS, signals LLM, releases
            # filler + self-feedback gate, all under a single Lock
            # so concurrent barge-ins serialise. Per-step verdicts
            # surface on ``voice.tts.cancellation_chain``.
            #
            # Snapshot the trace id BEFORE the cancellation chain
            # because the next recording (transition_to_recording
            # below) will mint a fresh id; the BargeIn event needs to
            # stamp the *interrupted* utterance, not the new one.
            interrupted_utterance_id = self._current_utterance_id
            await self.cancel_speech_chain(reason="barge_in")
            await self._emit(
                BargeInEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=interrupted_utterance_id,
                )
            )
            logger.info(
                "Barge-in detected",
                mind_id=self._config.mind_id,
                **{"voice.utterance_id": interrupted_utterance_id},
            )
            logger.warning(
                "voice.barge_in.detected",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.frames_sustained": self._config.barge_in_threshold,
                    "voice.prob": round(float(vad_event.probability), 3),
                    "voice.threshold_frames": self._config.barge_in_threshold,
                    "voice.output_was_playing": True,
                    "voice.utterance_id": interrupted_utterance_id,
                },
            )
            # Clear the interrupted id so _transition_to_recording
            # mints fresh for the new utterance instead of inheriting
            # the cancelled one.
            self._clear_utterance_id()
            return await self._transition_to_recording(frame)

        if not self._output.is_playing:
            # Playback finished
            completed_utterance_id = self._current_utterance_id
            self._state = VoicePipelineState.IDLE
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(
                TTSCompletedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=completed_utterance_id,
                )
            )
            self._clear_utterance_id()
            return {"state": "IDLE", "event": "tts_completed"}

        return {"state": "SPEAKING"}

    # -- Transitions ---------------------------------------------------------

    async def _transition_to_recording(
        self,
        frame: npt.NDArray[np.int16],
    ) -> dict[str, Any]:
        """Transition to RECORDING state (skip wake word — already engaged)."""
        self._state = VoicePipelineState.RECORDING
        self._utterance_frames = [frame]
        self._silence_counter = 0
        self._recording_counter = 1
        # Step 13 frame emission — barge-in / no-wake recording start
        # is also a "user started speaking" event; the source field
        # discriminates from the wake-word path.
        self._record_frame(
            UserStartedSpeakingFrame(
                frame_type="UserStartedSpeaking",
                timestamp_monotonic=time.monotonic(),
                source="barge_in_or_no_wake",
            ),
        )
        # Mission §2.6 Ring 6 — covers two paths:
        #   1. wake-word disabled (continuous listen) — no prior mint,
        #      so this is the head of the trace.
        #   2. barge-in replay — the previous utterance's id was just
        #      cleared by cancel_speech_chain, so a fresh id is the
        #      head of the *next* utterance's trace.
        # Either way an empty id at this point means "new utterance
        # boundary"; an already-set id means "wake-word path already
        # minted" and we keep that one (wake → recording is a single
        # logical utterance).
        utterance_id = self._current_utterance_id or self._mint_new_utterance_id()
        logger.info(
            "voice_recording_started",
            mind_id=self._config.mind_id,
            wake_word_enabled=self._config.wake_word_enabled,
            **{"voice.utterance_id": utterance_id},
        )
        await self._emit(
            SpeechStartedEvent(
                mind_id=self._config.mind_id,
                utterance_id=utterance_id,
            )
        )
        return {"state": "RECORDING", "event": "barge_in_recording"}

    async def _end_recording(self) -> dict[str, Any]:
        """End recording and transcribe the utterance."""
        import numpy as np

        # Mission Phase 1 / T1.16 — Pipecat-aligned UserStoppedSpeaking
        # frame at the RECORDING → TRANSCRIBING boundary. Mirrors the
        # UserStartedSpeakingFrame emitted at WAKE_DETECTED → RECORDING
        # (line 924) and at the no-wake / barge-in transition
        # (line 1088), so the per-utterance frame_history span is
        # bracketed on both ends. Emitted BEFORE the state mutation so
        # the frame's monotonic timestamp lines up with the moment
        # silence-end was detected (the trailing frames have already
        # been counted in self._utterance_frames at this point). The
        # silero_prob_snapshot carries the last observed VAD probability
        # so dashboards can correlate the transition with the VAD curve.
        self._record_frame(
            UserStoppedSpeakingFrame(
                frame_type="UserStoppedSpeaking",
                timestamp_monotonic=time.monotonic(),
                silero_prob_snapshot=self._max_vad_prob_since_heartbeat,
            ),
        )

        self._state = VoicePipelineState.TRANSCRIBING
        utterance_id = self._current_utterance_id

        # Concatenate all frames
        if not self._utterance_frames:
            self._clear_utterance_id()
            self._state = VoicePipelineState.IDLE
            return {"state": "IDLE", "event": "empty_recording"}

        utterance = np.concatenate(self._utterance_frames)
        duration_ms = len(utterance) / _SAMPLE_RATE * 1000

        logger.info(
            "voice_recording_ended",
            mind_id=self._config.mind_id,
            frames=self._recording_counter,
            duration_ms=round(duration_ms, 1),
            silence_counter=self._silence_counter,
            **{"voice.utterance_id": utterance_id},
        )
        await self._emit(
            SpeechEndedEvent(
                mind_id=self._config.mind_id,
                duration_ms=duration_ms,
                utterance_id=utterance_id,
            )
        )
        self._utterance_frames.clear()

        # Transcribe
        start = time.monotonic()
        try:
            result = await self._stt.transcribe(utterance)
        except (VoiceError, RuntimeError, OSError) as exc:
            # VoiceError: typed STT subsystem failures (CloudSTTError
            # and siblings). RuntimeError: ONNX inference errors from
            # on-device backends (Moonshine). OSError: audio-device /
            # temp-file I/O. The pipeline transitions to IDLE so a
            # single bad utterance doesn't wedge the loop.
            logger.error(
                "STT failed",
                error=str(exc),
                exc_info=True,
                **{"voice.utterance_id": utterance_id},
            )
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"STT failed: {exc}",
                    utterance_id=utterance_id,
                )
            )
            self._clear_utterance_id()
            self._state = VoicePipelineState.IDLE
            return {"state": "IDLE", "event": "stt_error", "error": str(exc)}

        latency_ms = (time.monotonic() - start) * 1000

        logger.info(
            "voice_stt_completed",
            mind_id=self._config.mind_id,
            text_length=len(result.text),
            has_text=bool(result.text.strip()),
            language=result.language,
            latency_ms=round(latency_ms, 1),
            **{"voice.utterance_id": utterance_id},
        )
        # Phase 4 / T4.36 — query the rolling SNR window so
        # downstream consumers can downgrade STT confidence on
        # noisy capture conditions. The factor is a strict
        # downgrade ([0, 1] multiplier); empty buffer ⇒ 1.0
        # (no SNR data, leave confidence unmodified).
        # Phase 5.A.2 — read under the same configured-at-startup
        # mind_id the FrameNormalizer producer writes under, so
        # multi-mind hosts get per-mind utterance confidence
        # factors instead of a merged buffer.
        from sovyx.voice.health._recent_snr import window_summary

        snr_summary = window_summary(mind_id=self._config.mind_id)
        if snr_summary.count > 0:
            snr_p50_db: float | None = snr_summary.p50_db
            snr_factor = max(0.0, min(1.0, snr_summary.p50_db / 17.0))
        else:
            snr_p50_db = None
            snr_factor = 1.0
        if snr_factor < 1.0:
            logger.info(
                "voice.stt.confidence_gated",
                mind_id=self._config.mind_id,
                **{
                    "voice.utterance_id": utterance_id,
                    "voice.confidence_raw": round(result.confidence, 4),
                    "voice.snr_p50_db": (round(snr_p50_db, 2) if snr_p50_db is not None else None),
                    "voice.snr_confidence_factor": round(snr_factor, 4),
                    "voice.confidence_effective": round(result.confidence * snr_factor, 4),
                },
            )
        await self._emit(
            TranscriptionCompletedEvent(
                text=result.text,
                confidence=result.confidence,
                language=result.language,
                latency_ms=latency_ms,
                utterance_id=utterance_id,
                snr_p50_db=snr_p50_db,
                snr_confidence_factor=snr_factor,
            )
        )
        # Step 13 frame emission — STT decode boundary. The frame is
        # emitted only after the S1/S2 + hallucination + logprob
        # guards in the engine have run, so its text/confidence/
        # language are validated values (not raw STT output).
        self._record_frame(
            TranscriptionFrame(
                frame_type="Transcription",
                timestamp_monotonic=time.monotonic(),
                text=result.text[:512],  # bounded to keep ring memory predictable
                confidence=float(result.confidence),
                language=result.language or "",
            ),
        )

        if not result.text.strip():
            # S1/S2 wire-up: distinguish "user genuinely said nothing"
            # from "STT engine rejected the transcript" (hallucination
            # filter, compression-ratio reject, timeout). Pre-S1/S2
            # both paths produced the same silent IDLE transition,
            # masking sustained STT degradation as normal silence.
            from sovyx.voice.health._metrics import (  # noqa: PLC0415
                record_wake_word_false_fire,
            )

            rejection_reason = getattr(result, "rejection_reason", None)
            if rejection_reason is not None:
                # T7.7 — STT engine rejected the transcript; the wake
                # fired but no real command followed. Counts toward
                # the false-fire rate alongside empty-transcription
                # and sub-confidence reasons.
                record_wake_word_false_fire(reason="rejected_transcription")
                # T7.8 — feed the false-fire signal to the wake-word
                # detector so its adaptive cooldown can extend on
                # dense recent false-fires. No-op when the detector
                # has cooldown_adaptive_enabled=False.
                self._notify_wake_word_false_fire()
                logger.warning(
                    "voice.stt.transcription_dropped",
                    **{
                        "voice.mind_id": self._config.mind_id,
                        "voice.rejection_reason": rejection_reason,
                        "voice.latency_ms": round(latency_ms, 1),
                        "voice.confidence": result.confidence,
                        "voice.utterance_id": utterance_id,
                        "voice.action_required": ("user_did_not_get_a_response_check_stt_health"),
                    },
                )
                self._clear_utterance_id()
                self._state = VoicePipelineState.IDLE
                return {
                    "state": "IDLE",
                    "event": "transcription_dropped",
                    "rejection_reason": rejection_reason,
                }
            # T7.7 — wake fired but STT returned empty text → the
            # user never spoke (generic false-wake signal).
            record_wake_word_false_fire(reason="empty_transcription")
            # T7.8 — feed the false-fire signal to the wake-word detector.
            self._notify_wake_word_false_fire()
            logger.debug(
                "Empty transcription — discarding",
                **{"voice.utterance_id": utterance_id},
            )
            self._clear_utterance_id()
            self._state = VoicePipelineState.IDLE
            return {"state": "IDLE", "event": "empty_transcription"}

        # Band-aid #46 — false-wake recovery via STT confidence gate.
        # When ``false_wake_min_confidence`` is opted-in (> 0.0) and
        # the STT engine returns text BELOW that threshold, treat the
        # recording as a likely false wake (background noise that
        # pattern-matched the wake word but didn't carry a real
        # command). Pre-#46 the orchestrator forwarded ANY non-empty
        # text to perception → spurious LLM calls trying to respond
        # to "kjlsdf askdjf". The gate is opt-in (0.0 default = no
        # behaviour change pre-adoption) because Moonshine returns
        # hardcoded fixed values (0.7-0.95) that would render any
        # non-zero default inert there but actively breaking on
        # cloud STT engines that expose honest 0-1 confidence.
        if (
            self._config.false_wake_min_confidence > 0.0
            and result.confidence < self._config.false_wake_min_confidence
        ):
            from sovyx.voice.health._metrics import (  # noqa: PLC0415
                record_wake_word_false_fire,
            )

            self._false_wake_rejected_count += 1
            # T7.7 — STT confidence below threshold = wake fired on
            # noise. Counts toward the false-fire rate alongside
            # empty-transcription and rejected-transcription paths.
            record_wake_word_false_fire(reason="sub_confidence")
            # T7.8 — feed the false-fire signal to the wake-word detector.
            self._notify_wake_word_false_fire()
            logger.warning(
                "voice.wake.false_positive_rejected",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.confidence": round(float(result.confidence), 4),
                    "voice.threshold": self._config.false_wake_min_confidence,
                    "voice.text_length": len(result.text),
                    "voice.lifetime_rejected_count": self._false_wake_rejected_count,
                    "voice.latency_ms": round(latency_ms, 1),
                    "voice.utterance_id": utterance_id,
                    "voice.action_required": (
                        "Wake-word triggered but the resulting transcription "
                        "had sub-threshold confidence — most likely a false "
                        "positive (noise that pattern-matched the wake word). "
                        "If real commands are being rejected, lower "
                        "false_wake_min_confidence; if false wakes still "
                        "leak through, raise it. Trace: this rejection "
                        "happened AFTER STT but BEFORE the LLM was called."
                    ),
                },
            )
            self._clear_utterance_id()
            self._state = VoicePipelineState.IDLE
            return {
                "state": "IDLE",
                "event": "false_wake_rejected",
                "confidence": float(result.confidence),
                "threshold": self._config.false_wake_min_confidence,
            }

        # Feed perception
        self._state = VoicePipelineState.THINKING
        # Step 13 frame emission — LLM dispatch boundary. The model +
        # request_id will be filled by the cognitive bridge when it
        # registers the LLM cancel hook; here we mark the start with
        # the utterance_id so dashboards see the THINKING boundary.
        # v0.32.3 Phase 3.B.1 — capture the THINKING entry timestamp
        # so the matching :class:`LLMFullResponseEndFrame` (emitted at
        # ``speak()`` or ``flush_stream()``) can compute ``elapsed_ms``
        # against this anchor. Use ``time.monotonic()`` consistently
        # across both frame timestamps so dashboards rendering against
        # the same clock get a coherent THINKING span.
        thinking_start_monotonic = time.monotonic()
        self._llm_thinking_start_monotonic = thinking_start_monotonic
        self._record_frame(
            LLMFullResponseStartFrame(
                frame_type="LLMFullResponseStart",
                timestamp_monotonic=thinking_start_monotonic,
                model="",  # filled by cognitive bridge per-call
                request_id="",
            ),
        )
        if self._on_perception is not None:
            # v0.32.2 Phase 3.A Layer C — anti-pattern #35 cluster P0.A2.
            # Pre-fix this dispatched ``self._config.mind_id`` to the
            # cogloop. ``_config.mind_id`` is the configured-at-startup
            # mind id (or the literal ``"default"`` sentinel when a non-
            # resolver caller missed the wire-up); ``_current_mind_id``
            # is the per-turn authoritative value, set by:
            #   * ``_start`` to ``_config.mind_id`` (init default), AND
            #   * the wake-word router to the matched mind id when a
            #     multi-mind topology fires a per-mind wake word.
            # Using ``_config.mind_id`` here meant: even when the
            # operator spoke ``Hey Aria`` and the router matched mind
            # ``aria``, the cogloop received the perception under the
            # configured-at-startup mind (typically ``default`` if the
            # factory caller missed the resolver). Multi-mind operation
            # was effectively broken end-to-end. Use ``_current_mind_id``
            # so the cogloop dispatches to the mind that actually owned
            # this turn.
            logger.info(
                "voice_perception_invoked",
                mind_id=self._current_mind_id,
                text_length=len(result.text),
                **{"voice.utterance_id": utterance_id},
            )
            try:
                await self._on_perception(result.text, self._current_mind_id)
            except Exception as exc:  # noqa: BLE001 — perception callback isolation
                logger.error(
                    "Perception callback failed",
                    error=str(exc),
                    **{"voice.utterance_id": utterance_id},
                )
                # Mission Phase 1 / T1.20 — emit PipelineErrorEvent so
                # the dashboard's error-banner widget surfaces the
                # cognitive-layer failure (the log-only signal was
                # invisible to bus-keyed widgets). The callback
                # isolation contract still holds: the exception is
                # swallowed so a buggy cognitive layer can't take down
                # the voice pipeline; the event is the structured
                # observability trail.
                #
                # v0.32.2 Phase 3.A Layer C — same fix: report the
                # actual per-turn mind id so the dashboard error banner
                # tells operators which mind's cogloop blew up.
                await self._emit(
                    PipelineErrorEvent(
                        mind_id=self._current_mind_id,
                        error=f"perception_callback_failed: {exc}",
                        utterance_id=utterance_id,
                    )
                )
        else:
            # No callback wired — transcription has nowhere to go. This
            # is the "voice enabled but cognitive loop not registered"
            # misconfiguration; surface it so operators see why the
            # assistant is silent.
            #
            # v0.32.2 Phase 3.A Layer C — same fix: per-turn mind id.
            logger.warning(
                "voice_perception_skipped_no_callback",
                mind_id=self._current_mind_id,
                text_length=len(result.text),
                **{"voice.utterance_id": utterance_id},
            )

        return {
            "state": "THINKING",
            "event": "transcription_complete",
            "text": result.text,
            "confidence": result.confidence,
            "latency_ms": latency_ms,
        }

    # -- TTS / speaking interface (called by CogLoop) -----------------------

    def _emit_llm_full_response_end_frame(self, output_chars: int) -> None:
        """Emit :class:`LLMFullResponseEndFrame` at the THINKING → SPEAKING boundary.

        v0.32.3 Phase 3.B.1 — closes audit gap P0.B2. Pre-fix the End
        frame was defined in :mod:`_frame_types` but had zero emit
        sites; dashboards never saw the THINKING-span close so LLM-
        side latency couldn't be rendered without correlating against
        the LLM router's own logs.

        Emit semantics:
            * ``output_chars`` — rough character length of the LLM
              response handed to TTS (full text for ``speak()``;
              ``len(text_buffer)`` for ``flush_stream()``).
            * ``elapsed_ms`` — clock-monotonic delta between the
              matching :class:`LLMFullResponseStartFrame` (set by
              ``_llm_thinking_start_monotonic`` at the THINKING entry)
              and the End frame's emission. ``0`` when no THINKING
              phase preceded the speak (proactive cogloop initiative).
            * Suppressed entirely when ``_llm_thinking_start_monotonic``
              is ``None`` to avoid emitting a misleading "End" frame
              for a turn that had no observable "Start" — matches the
              ring buffer's start-end-frame pairing contract that
              dashboards rely on for the SPEAKING-after-THINKING span.

        Once emitted, ``_llm_thinking_start_monotonic`` is cleared so
        a follow-up speak/flush in the same turn doesn't double-emit.
        The next THINKING entry resets the anchor for the next turn.
        """
        if self._llm_thinking_start_monotonic is None:
            return
        now_monotonic = time.monotonic()
        elapsed_ms = max(
            0,
            int((now_monotonic - self._llm_thinking_start_monotonic) * 1000),
        )
        self._record_frame(
            LLMFullResponseEndFrame(
                frame_type="LLMFullResponseEnd",
                timestamp_monotonic=now_monotonic,
                output_chars=output_chars,
                elapsed_ms=elapsed_ms,
            ),
        )
        self._llm_thinking_start_monotonic = None

    async def speak(self, text: str) -> None:
        """Synthesize and play text (called by CogLoop.act).

        Args:
            text: Text to speak.
        """
        # v0.32.3 Phase 3.B.1 — emit the LLMFullResponseEndFrame BEFORE
        # the SPEAKING transition so the frame ring buffer reflects
        # THINKING-end → SPEAKING-start in temporal order. Helper is a
        # no-op when no preceding THINKING phase fired (proactive
        # cogloop speak), preserving the start-end pairing contract.
        self._emit_llm_full_response_end_frame(output_chars=len(text))
        self._state = VoicePipelineState.SPEAKING
        # Step 13 frame emission — TTS speak boundary. Per-chunk
        # OutputAudioRawFrame frames will be emitted as chunks land
        # in the output queue (subsequent commit). Here we record
        # the speak entry as a chunk_index=0 marker so the
        # frame_history reflects the full SPEAKING span.
        self._record_frame(
            OutputAudioRawFrame(
                frame_type="OutputAudioRaw",
                timestamp_monotonic=time.monotonic(),
                chunk_index=0,
                pcm_bytes=0,
                sample_rate=0,
                synthesis_health="speak_started",
            ),
        )
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_start()
        # External proactive ``speak`` (e.g. cognitive layer's
        # initiative) without a preceding wake/recording mints its
        # own trace id so dashboards still get a per-turn span set;
        # an existing id from the wake → STT → think chain is
        # preserved (single logical utterance).
        utterance_id = self._current_utterance_id or self._mint_new_utterance_id()
        await self._emit(
            TTSStartedEvent(
                mind_id=self._config.mind_id,
                utterance_id=utterance_id,
            )
        )

        try:
            chunk = await self._synthesize_tracked(text)
            await self._output.play_immediate(chunk)
        except (VoiceError, RuntimeError, OSError) as exc:
            # TTS backends (Piper, Kokoro, cloud) share the same
            # failure profile as STT — typed subsystem errors, ONNX
            # runtime failures, and I/O. Emit a pipeline error event
            # so the cognitive loop knows the utterance didn't speak.
            logger.error(
                "TTS failed",
                error=str(exc),
                exc_info=True,
                **{"voice.utterance_id": utterance_id},
            )
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"TTS failed: {exc}",
                    utterance_id=utterance_id,
                )
            )
        finally:
            self._state = VoicePipelineState.IDLE
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(
                TTSCompletedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=utterance_id,
                )
            )
            self._clear_utterance_id()

    async def stream_text(self, text_chunk: str) -> None:
        """Stream text from LLM to TTS for speculative synthesis.

        Called by CogLoop as LLM tokens arrive.  Accumulates text and
        synthesizes at sentence boundaries (Jarvis Illusion §3).

        Args:
            text_chunk: Partial LLM output text.
        """
        if self._state != VoicePipelineState.SPEAKING:
            self._state = VoicePipelineState.SPEAKING
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_start()
            # Streaming path mints a trace id only when the cognitive
            # layer fed text without a preceding wake/STT chain. The
            # common path is wake → STT → THINKING → stream_text,
            # where ``_current_utterance_id`` is already set from
            # the wake-word mint.
            utterance_id = self._current_utterance_id or self._mint_new_utterance_id()
            await self._emit(
                TTSStartedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=utterance_id,
                )
            )

        # Cancel filler if still pending
        if not self._first_token_event.is_set():
            self._first_token_event.set()

        self._text_buffer += text_chunk
        segments = split_at_boundaries(self._text_buffer)

        # Synthesize all complete segments
        for segment in segments[:-1]:
            try:
                chunk = await self._synthesize_tracked(segment)
                await self._output.enqueue(chunk)
                # T1.39 — observe the spectral-centroid drift on every
                # successfully-emitted chunk. The DSP runs in a worker
                # thread (CLAUDE.md anti-pattern #14 — keep CPU-bound
                # work off the asyncio loop, even sub-millisecond
                # bursts). On drift the WARN + PipelineErrorEvent
                # mirror the T1.36 / T1.19 / T1.20 pattern; no
                # automatic fallback (operator-disruptive without
                # explicit opt-in). Skipped entirely when the gate is
                # disabled so resource-constrained deployments pay
                # zero DSP cost.
                await self._observe_speaker_drift(chunk)
                # Mission Phase 1 / T1.21 — successful segment resets
                # the consecutive-failure counter so a transient
                # hiccup mid-stream doesn't poison the rest of the
                # response. Inlined here (rather than in a try-else
                # clause) because the try block also has an
                # ``except asyncio.CancelledError`` clause and Python
                # forbids ``else`` between ``except`` clauses.
                # v0.31.7 T3.5 (LOW.4) — guard with the failure-counter
                # lock so a future parallel stream_text caller can't
                # race the read+write across awaits.
                async with self._tts_segment_failure_lock:
                    self._consecutive_tts_segment_failures = 0
            except (VoiceError, RuntimeError, OSError) as exc:
                # Per-segment resilience during streaming: skip the
                # bad segment, keep speaking the rest. Traceback
                # preserved so persistent TTS failures don't hide.
                logger.warning(
                    "Stream TTS failed",
                    error=str(exc),
                    exc_info=True,
                )
                # Mission Phase 1 / T1.21 — track consecutive failures
                # and abort the stream when the TTS backend is wedged
                # (model corrupt, runtime OOM, infinite-loop bug).
                # Pre-T1.21 the loop kept iterating forever burning
                # compute on every incoming LLM segment with no
                # audible output. ``_consecutive_tts_segment_failures``
                # resets on the first successful segment below.
                # v0.31.7 T3.5 (LOW.4) — guard increment + threshold
                # check with the failure-counter lock so a future
                # parallel stream_text caller can't race the read+write.
                async with self._tts_segment_failure_lock:
                    self._consecutive_tts_segment_failures += 1
                    threshold_crossed = (
                        self._consecutive_tts_segment_failures
                        >= _CONSECUTIVE_TTS_FAILURE_THRESHOLD
                    )
                if threshold_crossed:
                    buffered_chars = len(self._text_buffer)
                    self._text_buffer = ""
                    logger.error(
                        "voice.tts.stream_aborted_consecutive_failures",
                        **{
                            "voice.mind_id": self._config.mind_id,
                            "voice.consecutive_failures": (self._consecutive_tts_segment_failures),
                            "voice.threshold": _CONSECUTIVE_TTS_FAILURE_THRESHOLD,
                            "voice.last_error": str(exc)[:200],
                            "voice.last_error_type": type(exc).__name__,
                            "voice.buffered_text_chars_dropped": buffered_chars,
                            "voice.action_required": (
                                "TTS backend produced consecutive errors. "
                                "Check the engine state (Piper model file "
                                "integrity, Kokoro ONNX session, or cloud "
                                "endpoint reachability via `sovyx doctor "
                                "voice`). Stream aborted to release the "
                                "cognitive layer; the next utterance will "
                                "rebuild from a clean state."
                            ),
                        },
                    )
                    await self._emit(
                        PipelineErrorEvent(
                            mind_id=self._config.mind_id,
                            error=(
                                f"stream_aborted_consecutive_failures "
                                f"(count={self._consecutive_tts_segment_failures}, "
                                f"last={type(exc).__name__})"
                            ),
                            utterance_id=self._current_utterance_id,
                        )
                    )
                    # Reset counter so the next stream_text call
                    # starts clean — the abort already broke the
                    # current stream's contract with the caller.
                    # v0.31.7 T3.5 (LOW.4) — same lock as above.
                    async with self._tts_segment_failure_lock:
                        self._consecutive_tts_segment_failures = 0
                    return
            except asyncio.CancelledError:
                # T1 barge-in cancelled this segment via
                # cancel_speech_chain. Stop iterating and let the
                # next turn re-establish the LLM stream — the
                # remaining segments belong to a discarded utterance.
                #
                # Mission Phase 1 / T1.15 — clear ``_text_buffer``
                # directly in this handler. Pre-T1.15 the cleanup was
                # assumed via cancel_speech_chain step 5, but this
                # path can be reached without the chain running
                # (cognitive-layer task cancellation, event-loop
                # shutdown). Clearing locally makes the cleanup
                # invariant hold regardless of which cancel source
                # fired. ``cancel_speech_chain`` step 5 stays as the
                # belt-and-suspenders cleanup for paths that don't
                # touch ``stream_text`` at all.
                buffered_chars = len(self._text_buffer)
                self._text_buffer = ""
                logger.info(
                    "voice.tts.stream_text_cancelled",
                    mind_id=self._config.mind_id,
                    buffered_text_chars=buffered_chars,
                )
                return

        # Keep incomplete segment in buffer
        self._text_buffer = segments[-1] if segments else ""

    async def flush_stream(self) -> None:
        """Flush remaining buffered text to TTS.

        Call when the LLM stream ends to synthesize the last segment.

        T1.34 — every cancellation path in this method now interrupts
        the output queue before exiting. Pre-T1.34 the
        ``except asyncio.CancelledError`` in the synthesize block
        cleared the text buffer but left any audio already enqueued by
        prior chunks of the streaming session sitting in the output
        queue, and a cancellation landing on the final
        ``await self._output.drain()`` likewise leaked queued audio.
        ``cancel_speech_chain`` always interrupts the output queue at
        step 1 BEFORE it cancels in-flight tasks (step 2), so the
        normal barge-in path was already covered transitively. T1.34
        closes the off-path cases — asyncio loop teardown during
        daemon shutdown, an external task cancelling the flush via
        ``task.cancel()`` without going through ``cancel_speech_chain``
        — by making the interrupt explicit here. Belt + suspenders;
        ``interrupt()`` is idempotent so the upstream
        ``cancel_speech_chain`` path is unaffected.

        v0.32.3 Phase 3.B.1 — emit :class:`LLMFullResponseEndFrame`
        on entry. This is the canonical "LLM done generating" signal
        for the streaming path: ``stream_text`` is called per-chunk
        and can't tell which chunk is the last, so the cognitive
        bridge invokes ``flush_stream`` once the LLM router signals
        completion. The frame's ``output_chars`` reports the residual
        buffer length (the chunks already synthesised landed in the
        per-chunk ``OutputAudioRawFrame`` ring); dashboards correlate
        the End frame's ``elapsed_ms`` with the matching Start frame
        to render LLM-side latency.
        """
        # v0.32.3 Phase 3.B.1 — close the THINKING-span observability
        # by emitting the matching End frame even if the residual
        # buffer is empty (LLM streamed to clean sentence boundaries).
        # Helper is a no-op when no THINKING phase preceded
        # (proactive flush from cogloop initiative).
        self._emit_llm_full_response_end_frame(
            output_chars=len(self._text_buffer),
        )
        if self._text_buffer.strip():
            try:
                chunk = await self._synthesize_tracked(self._text_buffer)
                await self._output.enqueue(chunk)
            except asyncio.CancelledError:
                # T1: cancelled by cancel_speech_chain mid-flush —
                # discard the tail (the user already barged in) and
                # let the next turn rebuild from a clean buffer.
                self._text_buffer = ""
                # T1.34 — clear any audio already enqueued during this
                # flush_stream call so the next utterance starts with
                # an empty output queue. ``interrupt()`` is idempotent
                # against the cancel_speech_chain step-1 interrupt that
                # routed us here in the barge-in case; on off-path
                # cancellations (loop teardown, direct task.cancel())
                # this is the ONLY interrupt that runs.
                with contextlib.suppress(Exception):
                    self._output.interrupt()
                logger.info(
                    "voice.tts.flush_cancelled",
                    mind_id=self._config.mind_id,
                )
                return
            except (VoiceError, RuntimeError, OSError) as exc:
                # Final-segment flush — losing this tail means the
                # user hears an abrupt cut, but the loop advances.
                # Traceback on warning so a broken TTS config surfaces.
                logger.warning(
                    "Flush TTS failed",
                    error=str(exc),
                    exc_info=True,
                )
        self._text_buffer = ""

        # Drain all queued audio
        try:
            await self._output.drain()
        except asyncio.CancelledError:
            # T1.34 — drain was cancelled mid-flight. Clear remaining
            # audio (drain WAITS for playback to finish; it does not
            # itself empty the queue, so a cancel here leaves the
            # queue non-empty). Interrupt + re-raise so the cancellation
            # still propagates to the caller.
            with contextlib.suppress(Exception):
                self._output.interrupt()
            raise

        completed_utterance_id = self._current_utterance_id
        self._state = VoicePipelineState.IDLE
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_end()
        await self._emit(
            TTSCompletedEvent(
                mind_id=self._config.mind_id,
                utterance_id=completed_utterance_id,
            )
        )
        self._clear_utterance_id()

    async def start_thinking(self) -> None:
        """Start the thinking phase — initiate filler timer.

        Call this when CogLoop begins processing (before LLM tokens arrive).
        If the LLM doesn't respond within ``filler_delay_ms``, a filler
        phrase is played.

        v0.31.7 T3.1 (M4) — cancels any previous ``_filler_task`` BEFORE
        spawning the new one. Pre-T3.1 a rapid double-call (proactive
        cogloop initiative racing a wake-driven turn) overwrote the
        attribute without cancelling the prior task; the orphan kept
        running and its ``play_filler_after_delay`` could enqueue audio
        during the next turn. ``_cancel_filler`` is idempotent — safe
        to call when no task is in flight.
        """
        self._state = VoicePipelineState.THINKING
        self._first_token_event.clear()

        # v0.31.7 T3.1 — guard against a rapid second start_thinking
        # before the prior filler completed. The cancel is sync + cheap
        # (just task.cancel()); the cancelled task drains on the next
        # event loop tick.
        self._cancel_filler()

        if self._config.fillers_enabled:
            self._filler_task = spawn(
                self._jarvis.play_filler_after_delay(self._output, self._first_token_event),
                name="voice-pipeline-filler",
            )

    # -- Internal helpers ----------------------------------------------------

    def _check_frame_drop_signals(self, now_monotonic: float) -> None:
        """O3 frame-drop monitor: absolute budget + cumulative-drift detector.

        Two complementary signals fire from this single helper:

        * **Absolute per-frame budget** — when the inter-arrival gap
          exceeds :data:`_FRAME_DROP_ABSOLUTE_BUDGET_S` (64 ms,
          perceptual threshold for real-time voice). One WARNING per
          violating frame; the gap value is reported verbatim so
          dashboards can rank stalls.
        * **Rolling-window cumulative drift** — once
          :data:`_FRAME_DROP_DRIFT_WINDOW_FRAMES` samples have
          accumulated, the mean inter-arrival is compared against the
          expected cadence. When the ratio exceeds
          :data:`_FRAME_DROP_DRIFT_RATIO` (10% sustained drift), a
          WARNING fires — rate-limited by
          :data:`_FRAME_DROP_DRIFT_RATE_LIMIT_S` so a sustained
          condition produces one event per second, not one per
          window. This is the failure mode the pre-O3 single relative
          threshold silently masked.

        Both events carry ``voice.threshold_kind`` so dashboards can
        distinguish absolute (catastrophic) vs drift (cumulative)
        signals without parsing the message string.
        """
        if self._last_frame_monotonic is None:
            # First frame in the session — no inter-arrival yet.
            return
        gap_s = now_monotonic - self._last_frame_monotonic
        self._recent_frame_intervals.append(gap_s)

        # ── Absolute per-frame budget ──────────────────────────────
        if gap_s > _FRAME_DROP_ABSOLUTE_BUDGET_S:
            logger.warning(
                "voice.frame.drop_detected",
                **{
                    "voice.threshold_kind": "absolute_budget",
                    "voice.gap_ms": round(gap_s * 1000.0, 1),
                    "voice.budget_ms": round(_FRAME_DROP_ABSOLUTE_BUDGET_S * 1000.0, 1),
                    "voice.expected_interval_ms": round(
                        self._expected_frame_interval_s * 1000.0, 1
                    ),
                    "voice.state": self._state.name,
                    "voice.mind_id": self._config.mind_id,
                },
            )

        # ── Rolling-window cumulative-drift ────────────────────────
        if len(self._recent_frame_intervals) < _FRAME_DROP_DRIFT_WINDOW_FRAMES:
            return  # not enough samples yet
        mean_interval = sum(self._recent_frame_intervals) / len(self._recent_frame_intervals)
        drift_ratio = mean_interval / self._expected_frame_interval_s
        if drift_ratio < _FRAME_DROP_DRIFT_RATIO:
            return
        # Rate-limit so sustained drift doesn't spam — one event per
        # _FRAME_DROP_DRIFT_RATE_LIMIT_S window. Fresh start uses
        # ``None`` sentinel (distinct from ``0.0`` which is a valid
        # monotonic clock value in tests with injected clocks).
        if (
            self._last_drift_warning_monotonic is not None
            and (now_monotonic - self._last_drift_warning_monotonic)
            < _FRAME_DROP_DRIFT_RATE_LIMIT_S
        ):
            return
        self._last_drift_warning_monotonic = now_monotonic
        logger.warning(
            "voice.frame.cumulative_drift_detected",
            **{
                "voice.threshold_kind": "rolling_window_drift",
                "voice.mean_interval_ms": round(mean_interval * 1000.0, 2),
                "voice.expected_interval_ms": round(self._expected_frame_interval_s * 1000.0, 2),
                "voice.drift_ratio": round(drift_ratio, 3),
                "voice.drift_ratio_threshold": _FRAME_DROP_DRIFT_RATIO,
                "voice.window_frames": len(self._recent_frame_intervals),
                "voice.state": self._state.name,
                "voice.mind_id": self._config.mind_id,
            },
        )

    # ── Heartbeat methods extracted to ``_heartbeat_mixin.py`` ──────
    # ``_track_vad_for_heartbeat`` + ``_emit_heartbeat`` +
    # ``_heartbeat_loop`` now live on
    # :class:`sovyx.voice.pipeline._heartbeat_mixin.HeartbeatMixin`,
    # mounted via ``class VoicePipeline(HeartbeatMixin):`` above. The
    # methods are still resolvable via ``self._track_vad_for_heartbeat``
    # / ``self._emit_heartbeat`` / ``self._heartbeat_loop`` through
    # MRO. Instance state stays initialised in ``__init__`` because
    # init order matters across the rest of the pipeline. Anti-pattern
    # #16 god-file split — Phase 5.F.19 / Finding 5 first extraction.

    # ── Bypass coordinator extracted to ``_bypass_coordinator_mixin.py`` ──
    # ``_maybe_trigger_bypass_coordinator`` + ``_invoke_deaf_signal`` +
    # ``_reset_coordinator_pending_after_timeout`` +
    # ``_record_coordinator_dedup`` now live on
    # :class:`sovyx.voice.pipeline._bypass_coordinator_mixin.BypassCoordinatorMixin`,
    # mounted via the multi-mixin host above. Methods stay accessible
    # via instance dispatch through MRO. The HeartbeatMixin's
    # ``self._maybe_trigger_bypass_coordinator()`` call resolves
    # through MRO to the new mixin (anti-pattern #32 case (b) contract
    # — the TYPE_CHECKING-only stub on HeartbeatMixin is type-check-
    # only and erased at runtime). Anti-pattern #16 — Phase 5.F.24.

    async def _emit(self, event: object) -> None:
        """Emit an event via the event bus (if available)."""
        if self._event_bus is not None:
            try:
                await self._event_bus.emit(event)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 — event bus emission isolation
                logger.warning("Event emission failed", event_type=type(event).__name__)

    def _cancel_filler(self) -> None:
        """Cancel pending filler task."""
        if self._filler_task is not None and not self._filler_task.done():
            self._filler_task.cancel()
            self._filler_task = None
        self._first_token_event.set()

    async def _observe_speaker_drift(self, chunk: Any) -> None:  # noqa: ANN401 — TTS chunk type varies by engine
        """Observe spectral-centroid drift on the freshly-emitted chunk.

        T1.39 — runs the centroid DSP in a worker thread (CLAUDE.md
        anti-pattern #14) and observes the result against the per-
        session rolling-window baseline. On drift exceeding
        :data:`_SPEAKER_DRIFT_RATIO_THRESHOLD` emits a structured
        WARN + :class:`PipelineErrorEvent` and continues — no
        automatic voice swap (too disruptive without operator opt-in;
        operators wanting fallback wire it via the existing
        ``synthesis_health`` field per T1.36).

        No-op when the speaker-consistency gate is disabled, when the
        chunk has no audio (zero-energy synthesis already covered by
        T1.36's ``synthesis_health="zero_energy"`` path), or when the
        rolling window is still warming up (first
        ``window_size - 1`` chunks of every session).

        The chunk type is engine-specific (``AudioChunk`` from
        ``tts_kokoro`` / ``tts_piper``; the orchestrator works with
        any value that has ``audio: npt.NDArray[np.int16]`` +
        ``sample_rate: int``).
        """
        if self._speaker_consistency is None:
            return
        audio = getattr(chunk, "audio", None)
        sample_rate = getattr(chunk, "sample_rate", 0)
        if audio is None or sample_rate <= 0:
            return
        centroid = await asyncio.to_thread(
            compute_spectral_centroid,
            audio,
            sample_rate,
        )
        drift, baseline, ratio = self._speaker_consistency.observe(centroid)
        if not drift:
            return
        logger.warning(
            "voice.tts.speaker_drift_detected",
            **{
                "voice.centroid_hz": round(centroid, 1),
                "voice.baseline_hz": round(baseline, 1),
                "voice.drift_ratio": round(ratio, 3),
                "voice.threshold_ratio": _SPEAKER_DRIFT_RATIO_THRESHOLD,
                "voice.window_size": _SPEAKER_DRIFT_WINDOW_SIZE,
                "voice.utterance_id": self._current_utterance_id,
                "voice.action_required": (
                    "Spectral centroid drifted >"
                    f"{int(_SPEAKER_DRIFT_RATIO_THRESHOLD * 100)}% from the "
                    "rolling-window baseline. Likely causes: voice file "
                    "partial download, ONNX session corruption, or a "
                    "buggy caller passing a different voice_id mid-"
                    "session. Check the TTS engine logs and run "
                    "`sovyx doctor voice` to verify model integrity."
                ),
            },
        )
        await self._emit(
            PipelineErrorEvent(
                mind_id=self._config.mind_id,
                error=(f"speaker_drift_detected (ratio={ratio:.3f}, baseline={baseline:.1f})"),
                utterance_id=self._current_utterance_id,
            )
        )

    async def _synthesize_tracked(self, text: str) -> Any:  # noqa: ANN401 — TTS chunk type varies
        """Synthesise ``text`` via a tracked task so T1 can cancel it.

        The pre-T1 pattern was ``await self._tts.synthesize(text)`` —
        the calling coroutine WAS the synthesis task, so an external
        observer (the barge-in path) had no way to cancel just the
        synth without cancelling its caller. T1 wraps each call in
        ``asyncio.create_task`` and registers it into
        :attr:`_in_flight_tts_tasks` so :meth:`cancel_speech_chain`
        can iterate and cancel transactionally.

        The task self-removes from the in-flight set in its own
        ``finally`` so the set stays bounded. CancelledError
        propagates so the caller (speak / stream_text / flush_stream)
        sees the cancellation and can take its own cleanup path.
        """
        task: asyncio.Task[Any] = asyncio.create_task(
            self._tts.synthesize(text),
            name=f"voice-tts-synth-{id(self) & 0xFFFF}",
        )
        await self._track_tts_task(task)
        try:
            return await task
        finally:
            await self._untrack_tts_task(task)

    # -- T1 atomic cancellation chain ---------------------------------------

    def register_llm_cancel_hook(
        self,
        hook: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Wire (or unwire) the upstream LLM cancellation hook (T1).

        The cognitive layer registers an awaitable that signals its LLM
        client to stop generating tokens. Called by the orchestrator's
        :meth:`cancel_speech_chain` (step 3 of the transactional chain)
        so a barge-in stops not just the audio output and TTS work but
        also the LLM upstream that's still producing the rest of the
        utterance.

        Pass ``None`` to unwire (e.g. when the cognitive layer tears
        down). Replacing a non-``None`` hook with another non-``None``
        hook is allowed (one cognitive layer hands off to another).

        The hook MUST be idempotent — :meth:`cancel_speech_chain` may
        invoke it multiple times across barge-in events and the chain
        contract requires the hook to never raise (catch + log
        internally) so chain-step accounting stays meaningful.
        """
        self._llm_cancel_hook = hook

    async def _track_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Register an in-flight TTS synthesis task for T1 cancellation.

        Called by :meth:`speak`, :meth:`stream_text`, and
        :meth:`flush_stream` whenever they spawn a TTS coroutine. The
        task removes itself in its own ``finally`` via
        :meth:`_untrack_tts_task` so the set stays bounded by the
        in-flight set, not the lifetime of the daemon.

        T1.13 — async + lock-guarded. The mutation itself is GIL-atomic
        in CPython, but the lock makes the atomicity guarantee
        explicit + survives a future refactor that would introduce an
        await between read-and-write. Same lock as
        :meth:`cancel_speech_chain`'s step-2 snapshot.
        """
        async with self._task_tracking_lock:
            self._in_flight_tts_tasks.add(task)

    async def _untrack_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Remove ``task`` from the in-flight set. Safe to call multiple times.

        T1.13 — async + lock-guarded; same lock as :meth:`_track_tts_task`
        and :meth:`cancel_speech_chain`'s step-2 snapshot.
        """
        async with self._task_tracking_lock:
            self._in_flight_tts_tasks.discard(task)

    def register_cogloop_task(self, task: asyncio.Task[Any]) -> None:
        """Register an in-flight cogloop bridge task for T3.2 cancellation.

        v0.31.7 T3.2 (M5) — called by
        ``dashboard/routes/voice.py::_on_perception`` immediately after
        ``asyncio.create_task(_run_bridge_isolated())`` so the
        orchestrator's :meth:`cancel_speech_chain` (step 2.5) can fall
        back to task cancellation when ``_llm_cancel_hook`` is None.
        Without this, a barge-in during the CR1 race window
        (turn N's bridge ``finally`` already nulled the hook before
        turn N+1's ``register`` ran) had no fallback to stop the bridge.

        The task removes itself on completion via a done-callback so
        the set stays bounded by the in-flight count, not the daemon
        lifetime. Synchronous — no lock needed (the set is mutated
        only from the loop thread + the done-callback runs on the
        loop thread too).
        """
        self._in_flight_cogloop_tasks.add(task)
        task.add_done_callback(self._in_flight_cogloop_tasks.discard)

    async def cancel_speech_chain(self, *, reason: str = "barge_in") -> None:
        """Run the four-step transactional cancellation chain (T1).

        Steps in order, each recorded with a ``"ok"`` / ``"failed"`` /
        ``"timeout"`` verdict on the structured
        ``voice.tts.cancellation_chain`` event:

        1. **Output queue flush** — interrupt active playback so the
           user hears barge-in immediately. Synchronous + always
           succeeds (idempotent ``interrupt()``).
        2. **In-flight TTS task cancellation** — every task in
           :attr:`_in_flight_tts_tasks` is cancelled and awaited with
           :data:`_CANCELLATION_TASK_TIMEOUT_S` budget. A wedged task
           that doesn't honour CancelledError within the budget is
           recorded as ``cancellation_timeout`` so operators can spot
           a buggy TTS backend.
        3. **Upstream LLM cancellation** — the registered
           :attr:`_llm_cancel_hook` is awaited if present. Without
           this step, the LLM keeps producing tokens that flow into
           the next turn (the pre-T1 silent failure mode).
        4. **Filler + self-feedback gate cleanup** — cancel the
           pending filler task and release the mic-ducking gate.

        The entire chain runs under :attr:`_cancellation_lock` so
        concurrent barge-ins serialise; the second acquirer observes
        the post-first-chain state (empty in-flight set, output
        already stopped) and short-circuits naturally with all-ok
        verdicts.

        ``reason`` is recorded on the event so the dashboard can
        attribute the chain to its trigger (``"barge_in"`` from
        :meth:`_handle_speaking`, or future callers like
        ``"shutdown"``, ``"manual_cancel"``).
        """
        async with self._cancellation_lock:
            chain_started = time.monotonic()
            step_results: dict[str, str] = {}

            # Step 1: output queue flush. Idempotent — calling
            # interrupt() on a quiescent queue is a no-op.
            try:
                self._output.interrupt()
                step_results["output_flush"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                step_results["output_flush"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="output_flush",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            # Step 2: cancel + await in-flight TTS tasks. Snapshot the
            # set so iteration is stable while tasks remove themselves
            # via _untrack_tts_task in their own finally blocks.
            #
            # T1.13 — snapshot acquires ``_task_tracking_lock`` briefly
            # so a concurrent ``_track_tts_task`` cannot mutate the set
            # mid-snapshot. Iteration runs OUTSIDE the lock so the
            # awaits below don't block new TTS tasks indefinitely (the
            # residual race — new tasks created during iteration are
            # caught by the cognitive layer's LLM-cancel hook in
            # step 3, not by this snapshot).
            async with self._task_tracking_lock:
                tasks_snapshot = tuple(self._in_flight_tts_tasks)
            cancelled_count = 0
            timeout_count = 0
            for task in tasks_snapshot:
                if task.done():
                    continue
                task.cancel()
                cancelled_count += 1
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    # CancelledError is the EXPECTED outcome of
                    # cancelling — don't treat it as failure. Timeout
                    # means the task didn't honour the cancellation
                    # within budget; record separately so dashboards
                    # can spot wedged TTS backends.
                    if not task.done():
                        timeout_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "voice.tts.cancellation_task_unexpected_exception",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            step_results["tts_tasks_cancel"] = "ok" if timeout_count == 0 else "timeout"

            # Step 2.5 (v0.31.7 T3.2 / M5): cogloop bridge task cancel.
            # Belt-and-suspenders against the CR1 race window — when
            # ``_llm_cancel_hook`` is None (turn N's bridge ``finally``
            # ran after turn N+1's hook register, OR a future
            # regression), step 3 below records ``no_hook_registered``
            # and the LLM keeps producing tokens. Cancelling the
            # cogloop task here propagates CancelledError into the
            # cancel-hook-aware bridge body, which awaits the LLM
            # client's stream-cancellation in its CancelledError
            # handler. Snapshot to a tuple so the iteration is stable
            # while the done-callback removes tasks from the set.
            cogloop_snapshot = tuple(self._in_flight_cogloop_tasks)
            cogloop_cancelled = 0
            cogloop_timeout = 0
            for task in cogloop_snapshot:
                if task.done():
                    continue
                task.cancel()
                cogloop_cancelled += 1
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    if not task.done():
                        cogloop_timeout += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "voice.tts.cancellation_cogloop_unexpected_exception",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            step_results["cogloop_tasks_cancel"] = "ok" if cogloop_timeout == 0 else "timeout"

            # Step 3: upstream LLM cancellation. Best-effort — the hook
            # contract says "never raise", but we shield anyway so a
            # buggy hook can't take down the chain.
            if self._llm_cancel_hook is None:
                step_results["llm_cancel"] = "no_hook_registered"
            else:
                try:
                    await asyncio.wait_for(
                        self._llm_cancel_hook(),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                    step_results["llm_cancel"] = "ok"
                except TimeoutError:
                    step_results["llm_cancel"] = "timeout"
                except Exception as exc:  # noqa: BLE001 — hook isolation
                    step_results["llm_cancel"] = "failed"
                    logger.warning(
                        "voice.tts.cancellation_step_failed",
                        step="llm_cancel",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )

            # Step 4: filler + self-feedback gate cleanup. Synchronous
            # + idempotent, so wrap defensively but expect success.
            try:
                self._cancel_filler()
                if self._self_feedback_gate is not None:
                    self._self_feedback_gate.on_tts_end()
                step_results["filler_and_gate"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                step_results["filler_and_gate"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="filler_and_gate",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            # Step 5: text-buffer cleanup (band-aid #15 final fix).
            # Pre-step-5 the cancel_speech_chain left ``_text_buffer``
            # untouched on barge-in. If the LLM had streamed
            # "Hello, this is a long respo" before the user barged in,
            # the buffer kept "Hello, this is a long respo" and the
            # NEXT utterance's stream_text would prepend that residue
            # — the user heard "Hello, this is a long respo[NEW
            # TURN]" instead of the new turn cleanly. The T1 commit
            # cleaned the buffer in stream_text's CancelledError path,
            # but the broader cancel_speech_chain (called from
            # barge_in, shutdown, manual_cancel) never touched it.
            # This step closes that gap unconditionally — buffer is
            # always empty after a chain run, regardless of which
            # path triggered the chain.
            try:
                buffer_chars_dropped = len(self._text_buffer)
                self._text_buffer = ""
                step_results["text_buffer_cleanup"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                buffer_chars_dropped = 0
                step_results["text_buffer_cleanup"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="text_buffer_cleanup",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            chain_duration_ms = (time.monotonic() - chain_started) * 1000.0
            logger.info(
                "voice.tts.cancellation_chain",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.reason": reason,
                    "voice.chain_duration_ms": round(chain_duration_ms, 2),
                    "voice.tasks_cancelled": cancelled_count,
                    "voice.tasks_timed_out": timeout_count,
                    "voice.has_llm_hook": self._llm_cancel_hook is not None,
                    "voice.text_buffer_chars_dropped": buffer_chars_dropped,
                    "voice.cogloop_tasks_cancelled": cogloop_cancelled,
                    "voice.cogloop_tasks_timed_out": cogloop_timeout,
                    "voice.step_output_flush": step_results["output_flush"],
                    "voice.step_tts_tasks_cancel": step_results["tts_tasks_cancel"],
                    "voice.step_cogloop_tasks_cancel": step_results["cogloop_tasks_cancel"],
                    "voice.step_llm_cancel": step_results["llm_cancel"],
                    "voice.step_filler_and_gate": step_results["filler_and_gate"],
                    "voice.step_text_buffer_cleanup": step_results["text_buffer_cleanup"],
                },
            )
            # Step 14 frame emission — the most semantically important
            # frame in the entire mission. Captures the T1 atomic
            # cancellation chain contract in one frozen object so
            # post-incident forensics can answer "what failed during
            # the barge-in" without crawling 5 separate
            # voice.tts.cancellation_step_failed log lines.
            #
            # Recorded AT CHAIN EXIT with all 5 step verdicts populated.
            # The frame is recorded INSIDE the cancellation lock so
            # observers see a consistent (chain-complete, frame-emitted)
            # state — concurrent barge-ins serialise on the lock + each
            # produces its own terminal frame.
            self._record_frame(
                BargeInInterruptionFrame(
                    frame_type="BargeInInterruption",
                    timestamp_monotonic=time.monotonic(),
                    reason=reason,
                    step_results=dict(step_results),
                ),
            )

    def reset(self) -> None:
        """Reset the pipeline to IDLE state (for testing or error recovery)."""
        self._state = VoicePipelineState.IDLE
        self._utterance_frames.clear()
        self._silence_counter = 0
        self._recording_counter = 0
        self._text_buffer = ""
        self._cancel_filler()
        self._output.clear()
