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
from sovyx.voice._mm_notification_client import (
    MMNotificationListener,
)
from sovyx.voice._mm_notification_client import (
    create_listener as create_mm_notification_listener,
)
from sovyx.voice._observability_pii import mint_utterance_id
from sovyx.voice._speaker_consistency import (
    SpeakerConsistencyMonitor,
    compute_spectral_centroid,
)
from sovyx.voice.health._driver_update_handler import DriverUpdateHandler
from sovyx.voice.health._driver_update_listener_win import (
    DriverUpdateListener,
    build_driver_update_listener,
)
from sovyx.voice.health._metrics import (
    record_noise_floor_drift_alert,
    record_snr_low_alert,
    record_time_to_first_utterance,
)
from sovyx.voice.health.contract import BypassVerdict
from sovyx.voice.jarvis import JarvisConfig, JarvisIllusion, split_at_boundaries
from sovyx.voice.pipeline._barge_in import BargeInDetector
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
from sovyx.voice.pipeline._frame_types import (
    BargeInInterruptionFrame,
    CaptureRestartFrame,
    EndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    PipelineFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from sovyx.voice.pipeline._output_queue import AudioOutputQueue
from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import PipelineStateMachine

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId
    from sovyx.voice._aec import RenderPcmSink
    from sovyx.voice._wake_word_router import WakeWordRouter
    from sovyx.voice.health._self_feedback import SelfFeedbackGate
    from sovyx.voice.health.contract import BypassOutcome
    from sovyx.voice.stt import STTEngine
    from sovyx.voice.tts_piper import TTSEngine
    from sovyx.voice.vad import SileroVAD, VADEvent
    from sovyx.voice.wake_word import WakeWordConfig, WakeWordDetector

logger = get_logger(__name__)

# Pipeline tuning constants (timing/frame thresholds).
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32ms at 16kHz
_SILENCE_FRAMES_END = 22  # ~700ms silence -> end of utterance
_MAX_RECORDING_FRAMES = 312  # ~10s max recording
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160ms sustained speech -> barge-in
_FILLER_DELAY_MS = 800  # Play filler if no LLM token within this
_TEXT_MIN_WORDS = 3  # Min words before TTS synthesis
_HEARTBEAT_INTERVAL_S = _VoiceTuning().pipeline_heartbeat_interval_seconds
_DEAF_MIN_FRAMES = _VoiceTuning().pipeline_deaf_min_frames
_DEAF_VAD_MAX_THRESHOLD = _VoiceTuning().pipeline_deaf_vad_max_threshold
_DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY = _VoiceTuning().deaf_warnings_before_exclusive_retry

_SNR_LOW_ALERT_ENABLED = _VoiceTuning().voice_snr_low_alert_enabled
_SNR_LOW_ALERT_THRESHOLD_DB = _VoiceTuning().voice_snr_low_alert_threshold_db
_SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS = _VoiceTuning().voice_snr_low_alert_consecutive_heartbeats
"""Phase 4 / T4.35 — see :class:`VoiceTuningConfig` for full
docstrings. Module-level capture mirrors the existing pipeline
tuning pattern so the orchestrator's hot path doesn't re-parse
the config on every heartbeat."""

_NOISE_FLOOR_DRIFT_ALERT_ENABLED = _VoiceTuning().voice_noise_floor_drift_alert_enabled
_NOISE_FLOOR_DRIFT_THRESHOLD_DB = _VoiceTuning().voice_noise_floor_drift_threshold_db
_NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS = (
    _VoiceTuning().voice_noise_floor_drift_consecutive_heartbeats
)
"""Phase 4 / T4.38 — same module-capture rationale as the SNR
trio above."""

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


_COORDINATOR_PENDING_TIMEOUT_S = _VoiceTuning().pipeline_coordinator_pending_timeout_seconds
"""T1.14 watchdog deadline — see
``VoiceTuningConfig.pipeline_coordinator_pending_timeout_seconds``."""


_SPEAKER_CONSISTENCY_ENABLED = _VoiceTuning().pipeline_speaker_consistency_enabled
"""T1.39 — gate for the spectral-centroid drift detector. See
``VoiceTuningConfig.pipeline_speaker_consistency_enabled``."""

_SPEAKER_DRIFT_WINDOW_SIZE = _VoiceTuning().pipeline_speaker_drift_window_size
"""T1.39 — rolling-window size. See
``VoiceTuningConfig.pipeline_speaker_drift_window_size``."""

_SPEAKER_DRIFT_RATIO_THRESHOLD = _VoiceTuning().pipeline_speaker_drift_ratio_threshold
"""T1.39 — relative-drift threshold. See
``VoiceTuningConfig.pipeline_speaker_drift_ratio_threshold``."""


class VoicePipeline:
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

        # Observability — feed_frame updates these on every frame so
        # _maybe_emit_heartbeat can answer "is VAD seeing real audio"
        # without per-frame log spam. Reset after each heartbeat.
        self._max_vad_prob_since_heartbeat: float = 0.0
        self._vad_frames_since_heartbeat: int = 0
        self._last_heartbeat_monotonic: float = 0.0

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

    def _record_frame(self, frame: PipelineFrame) -> None:
        """Stamp utterance_id + record the frame on the state machine.

        Mission §1.1 Hybrid Option C: observability-only. Frame
        recording is best-effort — any exception during recording
        (e.g. state machine lock contention under chaos injection) is
        absorbed so the orchestrator's authoritative state mutation
        path is never blocked.

        Pre-condition: ``frame.timestamp_monotonic`` is set by the
        caller (typically ``time.monotonic()`` at the call site so the
        timestamp matches the real transition moment, not this helper's
        invocation moment).
        """
        try:
            # Frames are frozen dataclasses — to set utterance_id we
            # construct a copy via dataclasses.replace. The cost is one
            # allocation per recording, well below the bounded ring's
            # heartbeat budget.
            from dataclasses import replace

            stamped = replace(frame, utterance_id=self._current_utterance_id)
            self._state_machine.record_frame(stamped)
        except Exception as exc:  # noqa: BLE001 — observability isolation
            logger.debug(
                "voice.pipeline.frame_record_skipped",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def record_capture_restart(self, frame: CaptureRestartFrame) -> None:
        """Public cross-component channel for :class:`CaptureRestartFrame`.

        T32 — the capture-task restart methods (``request_*_restart``
        on :class:`RestartMixin`) live OUTSIDE the orchestrator but
        emit observability frames into the same bounded ring buffer
        the orchestrator owns. Per CLAUDE.md anti-pattern #29 the
        frame is observability-only — does NOT replace the
        boolean-flag + ``VoicePipelineState`` authoritative state.

        Method is kept narrow on purpose: it accepts ONLY
        :class:`CaptureRestartFrame` instances rather than the
        general ``PipelineFrame`` parent class, so the public
        cross-component surface stays minimal. Other frame types
        continue to flow through the orchestrator-internal
        :meth:`_record_frame` path.

        Best-effort recording per :meth:`_record_frame`'s contract —
        any exception during state-machine record (lock contention,
        ring overflow under chaos injection) is absorbed so the
        capture-task restart path is never blocked by observability.

        Args:
            frame: The :class:`CaptureRestartFrame` to record. Caller
                MUST set ``timestamp_monotonic`` at the actual
                transition moment (typically just before the
                ring-buffer epoch increment in the restart method).
        """
        self._record_frame(frame)

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

    @property
    def state(self) -> VoicePipelineState:
        """Current pipeline state."""
        return self._state

    @property
    def config(self) -> VoicePipelineConfig:
        """Pipeline configuration."""
        return self._config

    @property
    def output(self) -> AudioOutputQueue:
        """Audio output queue."""
        return self._output

    def set_render_buffer(self, buffer: RenderPcmSink | None) -> None:
        """Wire (or unwire) the AEC render-PCM sink at the orchestrator level.

        Phase 4 / T4.4.d wiring helper. Delegates to
        :meth:`AudioOutputQueue.set_render_buffer` so the factory only
        needs one call to register the shared
        :class:`~sovyx.voice._render_pcm_buffer.RenderPcmBuffer`
        instance with the playback path. The capture-side registration
        (FrameNormalizer's render_provider) is plumbed separately
        through :class:`AudioCaptureTask` at construction time —
        the same buffer instance implements both Protocols, so a
        single buffer flows producer→consumer through the queue and
        the normalizer.
        """
        self._output.set_render_buffer(buffer)

    @property
    def jarvis(self) -> JarvisIllusion:
        """Jarvis Illusion controller."""
        return self._jarvis

    @property
    def is_running(self) -> bool:
        """Whether the pipeline is active."""
        return self._running

    @property
    def frame_history(self) -> tuple[PipelineFrame, ...]:
        """Public accessor for the bounded frame ring buffer (Step 15).

        Returns a tuple snapshot (oldest-first) of every frame the
        orchestrator has recorded since boot OR the last
        :meth:`PipelineStateMachine.reset` call. The deque under the
        hood is bounded at the state machine's ``history_capacity``
        (default 256), so the snapshot is always at most that size.

        Mission §1.1 Hybrid Option C — observability surface for the
        Pipecat-aligned typed frames recorded at the 5 transition sites
        (Step 13) plus the BargeInInterruptionFrame at every
        cancel_speech_chain exit (Step 14).

        Consumers:

        * Dashboard ``GET /api/voice/frame-history`` (registered in
          ``src/sovyx/dashboard/routes/voice.py``).
        * Soak validation harness (Step 16).
        * Operator forensics ("what frames fired during this turn?").

        The snapshot is immutable — caller mutations cannot leak
        back into the deque.
        """
        return self._state_machine.frame_history()

    @property
    def vad(self) -> SileroVAD:
        """Voice activity detector used by this pipeline."""
        return self._vad

    @property
    def stt(self) -> STTEngine:
        """Speech-to-text engine used by this pipeline."""
        return self._stt

    @property
    def tts(self) -> TTSEngine:
        """Text-to-speech engine used by this pipeline."""
        return self._tts

    @property
    def wake_word(self) -> WakeWordDetector:
        """Wake word detector used by this pipeline."""
        return self._wake_word

    def register_mind_wake_word(
        self,
        mind_id: MindId,
        *,
        model_path: Path,
        config: WakeWordConfig | None = None,
    ) -> None:
        """Hot-reload a mind's wake-word detector with a new ONNX model.

        Phase 8 / T8.15 — wires the ``wake_word.register_mind`` RPC
        handler to the live :class:`~sovyx.voice._wake_word_router.WakeWordRouter`
        owned by this pipeline. Idempotent: re-registering the same
        ``mind_id`` replaces the prior detector (the prior ONNX session
        is garbage-collected normally; no manual close needed). Use case:
        the operator finishes ``sovyx voice train-wake-word ...`` and
        wants the new model active without restarting the daemon.

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).
            model_path: Filesystem path to the trained ``.onnx``
                checkpoint. The router does not validate the file;
                callers MUST verify the path exists + ends in ``.onnx``
                before invoking. The RPC handler in
                :mod:`sovyx.engine._rpc_handlers` performs that
                validation.
            config: Per-mind ``WakeWordConfig`` (cooldown, thresholds,
                etc.). Default ``None`` reuses the router's default.

        Raises:
            VoiceError: When the multi-mind ``WakeWordRouter`` is not
                configured (single-mind mode). Message includes a
                remediation hint.
            ValueError: Propagated from
                :meth:`WakeWordRouter.register_mind` when ``mind_id``
                is empty.
        """
        from sovyx.engine.errors import VoiceError  # noqa: PLC0415

        if self._wake_word_router is None:
            msg = (
                "wake-word router not configured (single-mind mode); "
                "hot-reload requires multi-mind setup. Restart the daemon "
                "to pick up the new model from "
                "``<data_dir>/wake_word_models/pretrained/``."
            )
            raise VoiceError(msg)
        self._wake_word_router.register_mind(
            mind_id,
            model_path=model_path,
            config=config,
        )

    def unregister_mind_wake_word(self, mind_id: MindId) -> bool:
        """Remove a mind's wake-word detector from the live router.

        Mission ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T2
        — the symmetric inverse of :meth:`register_mind_wake_word`.
        Wires the ``wake_word.unregister_mind`` RPC handler to the
        live :class:`~sovyx.voice._wake_word_router.WakeWordRouter`
        owned by this pipeline.

        Use case: the operator flips
        :attr:`MindConfig.wake_word_enabled` from ``True`` to ``False``
        in the dashboard. T3's toggle endpoint persists the YAML +
        calls this method so the running pipeline drops the detector
        without a daemon restart. Idempotent on unknown mind_ids
        (the router itself is idempotent — see
        :meth:`WakeWordRouter.unregister_mind`).

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).

        Returns:
            ``True`` when the mind was previously registered and got
            removed; ``False`` when no detector existed for this id
            (idempotent no-op — caller can ignore or surface it).

        Raises:
            VoiceError: When the multi-mind ``WakeWordRouter`` is not
                configured (single-mind mode). Message includes a
                remediation hint mirroring
                :meth:`register_mind_wake_word`.
        """
        from sovyx.engine.errors import VoiceError  # noqa: PLC0415

        if self._wake_word_router is None:
            msg = (
                "wake-word router not configured (single-mind mode); "
                "unregister_mind requires multi-mind setup. The "
                "single-mind pipeline owns one detector via the legacy "
                "wake_word slot, not via the router."
            )
            raise VoiceError(msg)
        return self._wake_word_router.unregister_mind(mind_id)

    @property
    def vad_inference_timeout_count(self) -> int:
        """Lifetime count of VAD inferences that exceeded
        ``_VAD_INFERENCE_TIMEOUT_S`` (band-aid #50). Non-zero means
        the host has experienced sustained CPU pressure or an ONNX
        session anomaly; pair with ``voice.vad.inference_timeout``
        WARN events for attribution."""
        return self._vad_inference_timeouts

    @property
    def false_wake_rejected_count(self) -> int:
        """Lifetime count of utterances rejected by the band-aid #46
        false-wake confidence gate. Always 0 unless the operator has
        opted-in by setting :attr:`VoicePipelineConfig.false_wake_min_confidence`
        to a non-zero threshold. Non-zero means the wake-word stage
        is firing on noise the STT engine then reported as low-
        confidence — pair with ``voice.wake.false_positive_rejected``
        WARN events for the per-rejection trace."""
        return self._false_wake_rejected_count

    @property
    def current_utterance_id(self) -> str:
        """Trace ID of the in-flight utterance, or ``""`` between turns.

        Read-only accessor for downstream components (LLM router, TTS
        engine, observability bridges) that want to stamp the same
        trace context on their own structured logs / spans without
        re-deriving it. Empty string when the pipeline is IDLE — by
        construction, the orchestrator clears the field at every
        terminal back-to-IDLE transition.
        """
        return self._current_utterance_id

    # -- Per-utterance trace ID (Ring 6 trace contract) ----------------------

    def _mint_new_utterance_id(self) -> str:
        """Mint a fresh UUID4 for the next utterance and stash it.

        Called at every utterance boundary (wake-word detected,
        no-wake recording start, external ``speak`` without prior
        context). Safe to call when an id is already set — the new
        one replaces the previous (covers the barge-in path where
        the prior utterance is being torn down at the same moment
        the new one starts). Returns the minted id for the caller's
        immediate use (event stamping, log emission), avoiding a
        second attribute read on the hot path.
        """
        new_id = mint_utterance_id()
        self._current_utterance_id = new_id
        return new_id

    def _clear_utterance_id(self) -> None:
        """Reset the current utterance id back to the empty sentinel.

        Called at every terminal back-to-IDLE transition (TTS
        completed, error path, false-wake rejection, empty
        transcription) so the next utterance is guaranteed a fresh
        mint instead of re-using the prior trace. Idempotent —
        safe to call when already empty.

        Phase 8 / T8.10 — also resets the per-turn ``_current_mind_id``
        back to the orchestrator's config default. The next IDLE
        path's wake-word detection re-resolves the matched mind via
        the router (if wired) before the next downstream emission.
        """
        self._current_utterance_id = ""
        # Reset per-turn mind context to the config default so the
        # next turn's WakeWordDetectedEvent starts clean.
        self._current_mind_id = self._config.mind_id

    def _notify_wake_word_false_fire(self) -> None:
        """Forward a false-fire signal to the wake-word detector.

        Phase 7 / T7.8 — orchestrator → detector feedback for the
        adaptive-cooldown sliding window. The detector accumulates
        timestamps and elevates cooldown to ``cooldown_max_seconds``
        when the rolling-window count crosses the threshold.

        Best-effort: a wake-word detector that doesn't expose
        ``note_false_fire`` (e.g. the factory's no-op stub when
        ``wake_word_enabled=False``) is silently skipped. The
        orchestrator's other false-fire paths (counter + log event)
        still fire regardless.
        """
        notify = getattr(self._wake_word, "note_false_fire", None)
        if notify is None:
            return
        try:
            notify()
        except Exception:  # noqa: BLE001 — observability path must not break the pipeline
            logger.exception("voice.wake_word.note_false_fire_failed")

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

    def _register_listeners(self) -> None:
        """Build + register the runtime device-monitoring listeners.

        Called once from :meth:`start`. Each listener registers
        independently — failure of one does NOT block the others.
        Successful registrations are appended to ``self._listeners``
        for symmetric teardown in :meth:`_unregister_listeners`.

        On any failure to obtain the asyncio loop (the listeners need
        it for ``call_soon_threadsafe`` marshalling), the entire
        registration step is skipped with a structured WARN. Pipeline
        keeps working with degraded device-change awareness.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            logger.warning(
                "voice.pipeline.listener_registration_skipped",
                reason="no_running_event_loop",
                error=str(exc),
            )
            return

        # MM notification listener — Windows COM device-change events.
        try:
            mm_listener = create_mm_notification_listener(
                loop=loop,
                on_default_capture_changed=self._on_default_capture_changed,
                on_device_state_changed=self._on_device_state_changed,
                enabled=self._mm_notification_listener_enabled,
            )
            mm_listener.register()
            self._listeners.append(mm_listener)
        except BaseException as exc:  # noqa: BLE001 — listener registration must NEVER block pipeline start
            logger.warning(
                "voice.pipeline.mm_listener_register_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Driver-update listener — Windows WMI subscription.
        # Independent of MM listener result per failure-isolation
        # contract.
        try:
            handler = DriverUpdateHandler(
                recascade_enabled=self._audio_driver_update_recascade_enabled,
            )
            driver_update_listener = build_driver_update_listener(
                loop=loop,
                on_driver_changed=handler.handle_driver_update,
                enabled=self._audio_driver_update_listener_enabled,
            )
            driver_update_listener.register()
            self._listeners.append(driver_update_listener)
        except BaseException as exc:  # noqa: BLE001 — see MM listener rationale above
            logger.warning(
                "voice.pipeline.driver_update_listener_register_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _unregister_listeners(self) -> None:
        """Tear down all registered runtime listeners.

        Called once from :meth:`stop`. Each unregister is wrapped in
        try/except so a wedged WMI service / COM marshalling glitch
        on one listener doesn't block the pipeline shutdown path.
        Idempotent — calling on an already-unregistered listener is
        a no-op.

        After this returns, ``self._listeners`` is empty. A subsequent
        ``start()`` call (after a stop) will re-register fresh
        listeners via :meth:`_register_listeners`.
        """
        for listener in self._listeners:
            try:
                listener.unregister()
            except BaseException as exc:  # noqa: BLE001 — shutdown must never propagate
                logger.warning(
                    "voice.pipeline.listener_unregister_failed",
                    listener_type=type(listener).__name__,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        self._listeners.clear()

    async def _on_default_capture_changed(self, device_id: str) -> None:
        """Async callback for ``IMMNotificationClient.OnDefaultDeviceChanged``
        events filtered to ``flow=eCapture, role=eCommunications``.

        Mission Phase 1b emits the structured event ONLY. The
        capture-task ``request_device_change_restart`` wire-up that
        turns this signal into an actual restart is OUT OF SCOPE per
        mission Part 4.2 — separate future commit.
        """
        logger.info(
            "voice.default_capture_changed",
            device_id=device_id,
            mind_id=self._config.mind_id,
        )

    async def _on_device_state_changed(self, device_id: str, new_state: int) -> None:
        """Async callback for ``IMMNotificationClient.OnDeviceStateChanged``.

        Same scope contract as :meth:`_on_default_capture_changed` —
        emit the structured event for now; downstream wire-up
        deferred.

        Args:
            device_id: The endpoint GUID whose state changed.
            new_state: ``DEVICE_STATE_*`` bitfield value (0x1=ACTIVE,
                0x2=DISABLED, 0x4=NOT_PRESENT, 0x8=UNPLUGGED).
        """
        logger.info(
            "voice.device_state_changed",
            device_id=device_id,
            new_state=hex(new_state & 0xFFFFFFFF),
            mind_id=self._config.mind_id,
        )

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
        from sovyx.voice.health._recent_snr import window_summary

        snr_summary = window_summary()
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
        self._record_frame(
            LLMFullResponseStartFrame(
                frame_type="LLMFullResponseStart",
                timestamp_monotonic=time.monotonic(),
                model="",  # filled by cognitive bridge per-call
                request_id="",
            ),
        )
        if self._on_perception is not None:
            logger.info(
                "voice_perception_invoked",
                mind_id=self._config.mind_id,
                text_length=len(result.text),
                **{"voice.utterance_id": utterance_id},
            )
            try:
                await self._on_perception(result.text, self._config.mind_id)
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
                await self._emit(
                    PipelineErrorEvent(
                        mind_id=self._config.mind_id,
                        error=f"perception_callback_failed: {exc}",
                        utterance_id=utterance_id,
                    )
                )
        else:
            # No callback wired — transcription has nowhere to go. This
            # is the "voice enabled but cognitive loop not registered"
            # misconfiguration; surface it so operators see why the
            # assistant is silent.
            logger.warning(
                "voice_perception_skipped_no_callback",
                mind_id=self._config.mind_id,
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

    async def speak(self, text: str) -> None:
        """Synthesize and play text (called by CogLoop.act).

        Args:
            text: Text to speak.
        """
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
                self._consecutive_tts_segment_failures += 1
                if self._consecutive_tts_segment_failures >= _CONSECUTIVE_TTS_FAILURE_THRESHOLD:
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
        """
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
        """
        self._state = VoicePipelineState.THINKING
        self._first_token_event.clear()

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

    def _track_vad_for_heartbeat(self, probability: float) -> None:
        """Accumulate per-frame VAD stats and emit a periodic heartbeat.

        Logs ``voice_pipeline_heartbeat`` every
        ``pipeline_heartbeat_interval_seconds`` with the max probability
        observed, frames processed, and current FSM state. The counters
        reset after each emission so the "max" reflects the last window,
        not the lifetime of the pipeline.

        When VAD probabilities stay far below ``onset_threshold`` (0.5)
        despite real audio (capture heartbeats show live RMS), this log
        surfaces it without requiring per-frame debug traces. Conversely,
        a heartbeat with ``max_vad_probability >= 0.5`` but the
        orchestrator still in IDLE points at an FSM configuration issue
        (onset threshold / min_onset_frames).
        """
        if probability > self._max_vad_prob_since_heartbeat:
            self._max_vad_prob_since_heartbeat = probability
        self._vad_frames_since_heartbeat += 1

        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < _HEARTBEAT_INTERVAL_S:
            return
        # Phase 4 / T4.34 — drain the per-window SNR buffer for
        # the heartbeat. The fields are conditionally added to the
        # log: when count == 0 (sustained silence or pre-first-
        # speech) we OMIT them rather than emit synthetic zeros so
        # dashboards don't graph misleading floor values.
        from sovyx.voice.health._snr_heartbeat import drain_window_stats

        snr_window = drain_window_stats()
        heartbeat_extra: dict[str, object] = {}
        if snr_window.count > 0:
            heartbeat_extra["snr_p50_db"] = round(snr_window.p50_db, 2)
            heartbeat_extra["snr_p95_db"] = round(snr_window.p95_db, 2)
            heartbeat_extra["snr_sample_count"] = snr_window.count
        logger.info(
            "voice_pipeline_heartbeat",
            mind_id=self._config.mind_id,
            state=self._state.name,
            max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
            frames_processed=self._vad_frames_since_heartbeat,
            **heartbeat_extra,
        )
        # Phase 4 / T4.35 — SNR low-alert. Only consider windows
        # with real SNR samples (count > 0); a count==0 window
        # means sustained silence, where SNR is undefined and a
        # "low" verdict would be a false alarm.
        if _SNR_LOW_ALERT_ENABLED and snr_window.count > 0:
            if snr_window.p50_db < _SNR_LOW_ALERT_THRESHOLD_DB:
                self._snr_low_consecutive_heartbeats += 1
                if (
                    not self._snr_low_alert_active
                    and self._snr_low_consecutive_heartbeats
                    >= _SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS
                ):
                    self._snr_low_alert_active = True
                    record_snr_low_alert(state="warned")
                    logger.warning(
                        "voice_pipeline_snr_low_alert",
                        mind_id=self._config.mind_id,
                        state=self._state.name,
                        snr_p50_db=round(snr_window.p50_db, 2),
                        snr_p95_db=round(snr_window.p95_db, 2),
                        snr_sample_count=snr_window.count,
                        threshold_db=_SNR_LOW_ALERT_THRESHOLD_DB,
                        consecutive_heartbeats=(self._snr_low_consecutive_heartbeats),
                        hint=(
                            "Sustained low SNR p50 below the configured "
                            "floor (Moonshine STT degrades sharply <9 dB). "
                            "Likely causes: ambient room noise raised, "
                            "speaker moved further from mic, fan / HVAC "
                            "started, or in-process NS disabled while "
                            "OS NS bypassed. Check `voice.audio.snr_db` "
                            "histogram + `voice.ns.suppression_db` for "
                            "the underlying signal."
                        ),
                    )
            else:
                # Clean heartbeat — reset the de-flap counter and,
                # if the alert was active, emit a single CLEARED
                # event so dashboards close the incident.
                if self._snr_low_alert_active:
                    self._snr_low_alert_active = False
                    record_snr_low_alert(state="cleared")
                    logger.info(
                        "voice_pipeline_snr_low_alert_cleared",
                        mind_id=self._config.mind_id,
                        state=self._state.name,
                        snr_p50_db=round(snr_window.p50_db, 2),
                        snr_sample_count=snr_window.count,
                        threshold_db=_SNR_LOW_ALERT_THRESHOLD_DB,
                    )
                self._snr_low_consecutive_heartbeats = 0

        # Phase 4 / T4.38 — noise-floor drift alert. Reads the
        # rolling buffer without clearing (drain pattern is for
        # SNR; the noise-floor trend wants a stable horizon).
        if _NOISE_FLOOR_DRIFT_ALERT_ENABLED:
            from sovyx.voice.health._noise_floor_trending import compute_drift

            drift = compute_drift()
            if drift.ready:
                if drift.drift_db > _NOISE_FLOOR_DRIFT_THRESHOLD_DB:
                    self._noise_floor_drift_consecutive_heartbeats += 1
                    if (
                        not self._noise_floor_drift_alert_active
                        and self._noise_floor_drift_consecutive_heartbeats
                        >= _NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS
                    ):
                        self._noise_floor_drift_alert_active = True
                        record_noise_floor_drift_alert(state="warned")
                        logger.warning(
                            "voice_pipeline_noise_floor_drift_warning",
                            mind_id=self._config.mind_id,
                            state=self._state.name,
                            short_avg_db=round(drift.short_avg_db, 2),
                            long_avg_db=round(drift.long_avg_db, 2),
                            drift_db=round(drift.drift_db, 2),
                            short_sample_count=drift.short_count,
                            long_sample_count=drift.long_count,
                            threshold_db=_NOISE_FLOOR_DRIFT_THRESHOLD_DB,
                            consecutive_heartbeats=(
                                self._noise_floor_drift_consecutive_heartbeats
                            ),
                            hint=(
                                "Sustained noise-floor rise vs the rolling "
                                "5-min baseline. Likely causes: HVAC / fan "
                                "started, occupancy increased, mic moved "
                                "closer to a noise source, or capture "
                                "device gain auto-raised. Check "
                                "voice.audio.snr_db to see if speech-band "
                                "SNR also dropped (room actually got noisier "
                                "for speech) vs stayed flat (only the "
                                "non-speech floor changed)."
                            ),
                        )
                else:
                    if self._noise_floor_drift_alert_active:
                        self._noise_floor_drift_alert_active = False
                        record_noise_floor_drift_alert(state="cleared")
                        logger.info(
                            "voice_pipeline_noise_floor_drift_cleared",
                            mind_id=self._config.mind_id,
                            state=self._state.name,
                            short_avg_db=round(drift.short_avg_db, 2),
                            long_avg_db=round(drift.long_avg_db, 2),
                            drift_db=round(drift.drift_db, 2),
                            threshold_db=_NOISE_FLOOR_DRIFT_THRESHOLD_DB,
                        )
                    self._noise_floor_drift_consecutive_heartbeats = 0

        is_deaf = (
            self._vad_frames_since_heartbeat >= _DEAF_MIN_FRAMES
            and self._max_vad_prob_since_heartbeat < _DEAF_VAD_MAX_THRESHOLD
        )
        if is_deaf:
            # Audio is arriving but VAD is stuck near zero — this is the
            # canonical fingerprint of "frames are not 16 kHz mono" *or*
            # a capture APO (e.g. Windows Voice Clarity / VocaEffectPack)
            # zeroing out the signal before it reaches the engine.
            self._deaf_warnings_consecutive += 1
            logger.warning(
                "voice_pipeline_deaf_warning",
                mind_id=self._config.mind_id,
                state=self._state.name,
                max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
                frames_processed=self._vad_frames_since_heartbeat,
                vad_max_threshold=_DEAF_VAD_MAX_THRESHOLD,
                consecutive_deaf_warnings=self._deaf_warnings_consecutive,
                voice_clarity_active=self._voice_clarity_active,
                hint=(
                    "Orchestrator received frames but VAD probability stayed "
                    "below threshold — check FrameNormalizer source_rate/channels "
                    "and audio_capture_resample_active logs."
                ),
            )
            self._maybe_trigger_bypass_coordinator()
        else:
            # Reset the consecutive counter so a single healthy heartbeat
            # between two deaf ones does not trigger the auto-bypass.
            self._deaf_warnings_consecutive = 0
        self._last_heartbeat_monotonic = now
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0

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
                    logger.error(
                        "voice_apo_bypass_failed",
                        mind_id=self._config.mind_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    logger.error(
                        "audio.apo.bypassed",
                        **{
                            "voice.verdict": "failure",
                            "voice.mind_id": self._config.mind_id,
                            "voice.attempts": 0,
                            "voice.strategies": [],
                            "voice.outcomes": [],
                            "voice.error": str(exc),
                            "voice.error_type": type(exc).__name__,
                            "voice.voice_clarity_active": self._voice_clarity_active,
                        },
                    )
                    return

                if not outcomes:
                    # Coordinator short-circuited (false-alarm probe or prior
                    # resolution). Don't burn the terminal flag — we may still
                    # want to retry if deafness persists after a transient
                    # clear. The counter snapshot+reset above already broke
                    # the pre-O2 tight-retry pattern.
                    return

                self._coordinator_terminated = True
                applied_healthy = next(
                    (o for o in outcomes if o.verdict is BypassVerdict.APPLIED_HEALTHY),
                    None,
                )
                if applied_healthy is not None:
                    logger.warning(
                        "voice_apo_bypass_activated",
                        mind_id=self._config.mind_id,
                        strategy_name=applied_healthy.strategy_name,
                        attempt_index=applied_healthy.attempt_index,
                        reason=applied_healthy.detail,
                        voice_clarity_active=self._voice_clarity_active,
                        consecutive_deaf_warnings=invocation_counter_snapshot,
                        threshold=self._auto_bypass_threshold,
                        action="capture_integrity_coordinator",
                    )
                    logger.warning(
                        "audio.apo.bypassed",
                        **{
                            "voice.verdict": "success",
                            "voice.mind_id": self._config.mind_id,
                            "voice.strategy_name": applied_healthy.strategy_name,
                            "voice.attempt_index": applied_healthy.attempt_index,
                            "voice.attempts": len(outcomes),
                            "voice.strategies": [o.strategy_name for o in outcomes],
                            "voice.outcomes": [o.verdict.value for o in outcomes],
                            "voice.reason": applied_healthy.detail,
                            "voice.voice_clarity_active": self._voice_clarity_active,
                            "voice.consecutive_deaf_warnings": invocation_counter_snapshot,
                            "voice.threshold": self._auto_bypass_threshold,
                        },
                    )
                    return

                # Every strategy either failed to apply or applied-but-still-dead.
                # The coordinator has already quarantined the endpoint; surface a
                # single operator-facing event so the dashboard / doctor can
                # switch their messaging to "auto-fix could not recover — see
                # manual remediation steps".
                logger.error(
                    "voice_apo_bypass_ineffective",
                    mind_id=self._config.mind_id,
                    attempts=len(outcomes),
                    strategies=[o.strategy_name for o in outcomes],
                    verdicts=[o.verdict.value for o in outcomes],
                    voice_clarity_active=self._voice_clarity_active,
                    hint=(
                        "CaptureIntegrityCoordinator exhausted every eligible "
                        "bypass strategy. Endpoint quarantined for apo_quarantine_s; "
                        "factory will fail over to an alternate capture device on "
                        "next boot. Likely causes: firmware-level DSP on the mic, "
                        "a virtual audio cable with a fixed format, a damaged "
                        "capture element, or an APO not yet covered by a "
                        "platform strategy. Try manually disabling all "
                        "enhancements in the OS sound settings or switch capture "
                        "device."
                    ),
                )
                # "partial" verdict: at least one strategy applied cleanly but
                # the post-apply re-probe still classified the signal as dead;
                # otherwise every strategy either failed-to-apply or was not
                # applicable, which is a flat failure.
                any_applied = any(o.verdict is BypassVerdict.APPLIED_STILL_DEAD for o in outcomes)
                bypass_verdict = "partial" if any_applied else "failure"
                logger.error(
                    "audio.apo.bypassed",
                    **{
                        "voice.verdict": bypass_verdict,
                        "voice.mind_id": self._config.mind_id,
                        "voice.attempts": len(outcomes),
                        "voice.strategies": [o.strategy_name for o in outcomes],
                        "voice.outcomes": [o.verdict.value for o in outcomes],
                        "voice.voice_clarity_active": self._voice_clarity_active,
                        "voice.quarantined": True,
                    },
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
                    "voice.step_output_flush": step_results["output_flush"],
                    "voice.step_tts_tasks_cancel": step_results["tts_tasks_cancel"],
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
