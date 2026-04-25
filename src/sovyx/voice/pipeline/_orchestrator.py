"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger
from sovyx.observability.saga import SagaHandle, begin_saga, end_saga
from sovyx.observability.tasks import spawn
from sovyx.voice.health._metrics import record_time_to_first_utterance
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
from sovyx.voice.pipeline._output_queue import AudioOutputQueue
from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import PipelineStateMachine

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.events import EventBus
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
_HEARTBEAT_INTERVAL_S = _VoiceTuning().pipeline_heartbeat_interval_seconds
_DEAF_MIN_FRAMES = _VoiceTuning().pipeline_deaf_min_frames
_DEAF_VAD_MAX_THRESHOLD = _VoiceTuning().pipeline_deaf_vad_max_threshold
_DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY = _VoiceTuning().deaf_warnings_before_exclusive_retry

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

_FRAME_DROP_ABSOLUTE_BUDGET_S = 0.064
"""Absolute per-frame inter-arrival budget. 64 ms = 2× the nominal
32 ms cadence at 16 kHz / 512-sample window — chosen to match the
perceptual threshold above which a real-time voice loop gains
audible latency artefacts (Bencina, "Real-Time Audio Programming
101", 2020). A single frame exceeding this budget produces a
``voice.frame.drop_detected`` WARNING with ``threshold_kind=
"absolute_budget"``."""

_FRAME_DROP_DRIFT_WINDOW_FRAMES = 32
"""Rolling window over which the cumulative-drift detector averages
inter-arrival times. 32 frames at 16 kHz / 512-sample window =
~1.024 s of audio — long enough to suppress per-frame jitter while
short enough to react to sustained drift before the user notices."""

_FRAME_DROP_DRIFT_RATIO = 1.10
"""Mean inter-arrival ÷ expected interval threshold above which the
cumulative-drift detector fires. 1.10 = 10% sustained drift; chosen
because consistent +10% scheduling jitter accumulates ~3 ms per
frame, which over 32 frames = ~100 ms of cumulative latency —
audible. Below this the drift is noise; above it the drift is
structurally problematic."""

_FRAME_DROP_DRIFT_RATE_LIMIT_S = 1.0
"""Minimum gap between successive ``voice.frame.cumulative_drift_detected``
emissions. A sustained drift produces one event per second, not one
per window — the dashboard already aggregates by minute, so per-second
firing is enough resolution to localise onset/offset."""


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

_CANCELLATION_TASK_TIMEOUT_S = 1.0
"""Maximum wall-clock seconds to wait for an individual cancelled TTS
task to actually finish (``await task`` with timeout). 1 second is
the SRE-canonical "if it isn't dead by now it's hung" budget — long
enough for a graceful CancelledError teardown (typical: <50 ms)
but short enough that a wedged task doesn't block the next turn.
On timeout the task is recorded as ``cancellation_timeout`` in
the chain event so operators can attribute the wedge."""


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

        # §5.14 KPI — time-to-first-utterance. Wall-clock captured at
        # WakeWordDetectedEvent emission, measured at SpeechStartedEvent
        # emission. Only the wake-word path contributes (barge-in uses a
        # different SpeechStartedEvent site in _transition_to_recording).
        self._wake_detected_monotonic: float | None = None

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
        if old is new:
            return
        if old is VoicePipelineState.IDLE and new is not VoicePipelineState.IDLE:
            self._open_voice_turn_saga()
        elif new is VoicePipelineState.IDLE and old is not VoicePipelineState.IDLE:
            self._close_voice_turn_saga()

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

    @property
    def jarvis(self) -> JarvisIllusion:
        """Jarvis Illusion controller."""
        return self._jarvis

    @property
    def is_running(self) -> bool:
        """Whether the pipeline is active."""
        return self._running

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

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the pipeline and pre-cache fillers.

        Call this before feeding frames.
        """
        await self._jarvis.pre_cache()
        self._running = True
        self._state = VoicePipelineState.IDLE
        self._last_heartbeat_monotonic = time.monotonic()
        logger.info(
            "VoicePipeline started",
            mind_id=self._config.mind_id,
            wake_word=self._config.wake_word_enabled,
        )

    async def stop(self) -> None:
        """Stop the pipeline and clean up."""
        self._running = False
        self._cancel_filler()
        self._output.interrupt()
        self._state = VoicePipelineState.IDLE
        self._utterance_frames.clear()
        if self._self_feedback_gate is not None:
            # Release the duck so a mid-TTS stop doesn't leave the
            # capture normalizer attenuated for the next session.
            self._self_feedback_gate.on_tts_end()
        logger.info("VoicePipeline stopped", mind_id=self._config.mind_id)

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
        # tasks remain responsive while VAD runs.
        vad_event = await asyncio.to_thread(self._vad.process_frame, audio_f32)

        self._track_vad_for_heartbeat(vad_event.probability)

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
        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        ww_event = await asyncio.to_thread(self._wake_word.process_frame, audio_f32)

        if ww_event.detected:
            self._state = VoicePipelineState.WAKE_DETECTED
            self._wake_detected_monotonic = time.monotonic()
            await self._emit(WakeWordDetectedEvent(mind_id=self._config.mind_id))
            logger.info("Wake word detected", mind_id=self._config.mind_id)

            # Play confirmation beep
            if self._config.confirmation_tone == "beep":
                await self._jarvis.play_beep(self._output)

            await self._emit(SpeechStartedEvent(mind_id=self._config.mind_id))
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
            await self.cancel_speech_chain(reason="barge_in")
            await self._emit(BargeInEvent(mind_id=self._config.mind_id))
            logger.info("Barge-in detected", mind_id=self._config.mind_id)
            logger.warning(
                "voice.barge_in.detected",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.frames_sustained": self._config.barge_in_threshold,
                    "voice.prob": round(float(vad_event.probability), 3),
                    "voice.threshold_frames": self._config.barge_in_threshold,
                    "voice.output_was_playing": True,
                },
            )
            return await self._transition_to_recording(frame)

        if not self._output.is_playing:
            # Playback finished
            self._state = VoicePipelineState.IDLE
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(TTSCompletedEvent(mind_id=self._config.mind_id))
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
        logger.info(
            "voice_recording_started",
            mind_id=self._config.mind_id,
            wake_word_enabled=self._config.wake_word_enabled,
        )
        await self._emit(SpeechStartedEvent(mind_id=self._config.mind_id))
        return {"state": "RECORDING", "event": "barge_in_recording"}

    async def _end_recording(self) -> dict[str, Any]:
        """End recording and transcribe the utterance."""
        import numpy as np

        self._state = VoicePipelineState.TRANSCRIBING

        # Concatenate all frames
        if not self._utterance_frames:
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
        )
        await self._emit(
            SpeechEndedEvent(
                mind_id=self._config.mind_id,
                duration_ms=duration_ms,
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
            logger.error("STT failed", error=str(exc), exc_info=True)
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"STT failed: {exc}",
                )
            )
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
        )
        await self._emit(
            TranscriptionCompletedEvent(
                text=result.text,
                confidence=result.confidence,
                language=result.language,
                latency_ms=latency_ms,
            )
        )

        if not result.text.strip():
            # S1/S2 wire-up: distinguish "user genuinely said nothing"
            # from "STT engine rejected the transcript" (hallucination
            # filter, compression-ratio reject, timeout). Pre-S1/S2
            # both paths produced the same silent IDLE transition,
            # masking sustained STT degradation as normal silence.
            rejection_reason = getattr(result, "rejection_reason", None)
            if rejection_reason is not None:
                logger.warning(
                    "voice.stt.transcription_dropped",
                    **{
                        "voice.mind_id": self._config.mind_id,
                        "voice.rejection_reason": rejection_reason,
                        "voice.latency_ms": round(latency_ms, 1),
                        "voice.confidence": result.confidence,
                        "voice.action_required": ("user_did_not_get_a_response_check_stt_health"),
                    },
                )
                self._state = VoicePipelineState.IDLE
                return {
                    "state": "IDLE",
                    "event": "transcription_dropped",
                    "rejection_reason": rejection_reason,
                }
            logger.debug("Empty transcription — discarding")
            self._state = VoicePipelineState.IDLE
            return {"state": "IDLE", "event": "empty_transcription"}

        # Feed perception
        self._state = VoicePipelineState.THINKING
        if self._on_perception is not None:
            logger.info(
                "voice_perception_invoked",
                mind_id=self._config.mind_id,
                text_length=len(result.text),
            )
            try:
                await self._on_perception(result.text, self._config.mind_id)
            except Exception as exc:  # noqa: BLE001 — perception callback isolation
                logger.error("Perception callback failed", error=str(exc))
        else:
            # No callback wired — transcription has nowhere to go. This
            # is the "voice enabled but cognitive loop not registered"
            # misconfiguration; surface it so operators see why the
            # assistant is silent.
            logger.warning(
                "voice_perception_skipped_no_callback",
                mind_id=self._config.mind_id,
                text_length=len(result.text),
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
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_start()
        await self._emit(TTSStartedEvent(mind_id=self._config.mind_id))

        try:
            chunk = await self._synthesize_tracked(text)
            await self._output.play_immediate(chunk)
        except (VoiceError, RuntimeError, OSError) as exc:
            # TTS backends (Piper, Kokoro, cloud) share the same
            # failure profile as STT — typed subsystem errors, ONNX
            # runtime failures, and I/O. Emit a pipeline error event
            # so the cognitive loop knows the utterance didn't speak.
            logger.error("TTS failed", error=str(exc), exc_info=True)
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"TTS failed: {exc}",
                )
            )
        finally:
            self._state = VoicePipelineState.IDLE
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(TTSCompletedEvent(mind_id=self._config.mind_id))

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
            await self._emit(TTSStartedEvent(mind_id=self._config.mind_id))

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
            except (VoiceError, RuntimeError, OSError) as exc:
                # Per-segment resilience during streaming: skip the
                # bad segment, keep speaking the rest. Traceback
                # preserved so persistent TTS failures don't hide.
                logger.warning(
                    "Stream TTS failed",
                    error=str(exc),
                    exc_info=True,
                )
            except asyncio.CancelledError:
                # T1 barge-in cancelled this segment via
                # cancel_speech_chain. Stop iterating and let the
                # next turn re-establish the LLM stream — the
                # remaining segments belong to a discarded utterance.
                logger.info(
                    "voice.tts.stream_segment_cancelled",
                    mind_id=self._config.mind_id,
                )
                return

        # Keep incomplete segment in buffer
        self._text_buffer = segments[-1] if segments else ""

    async def flush_stream(self) -> None:
        """Flush remaining buffered text to TTS.

        Call when the LLM stream ends to synthesize the last segment.
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
        await self._output.drain()

        self._state = VoicePipelineState.IDLE
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_end()
        await self._emit(TTSCompletedEvent(mind_id=self._config.mind_id))

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
        logger.info(
            "voice_pipeline_heartbeat",
            mind_id=self._config.mind_id,
            state=self._state.name,
            max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
            frames_processed=self._vad_frames_since_heartbeat,
        )
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
        callback = self._on_deaf_signal
        if callback is None:
            self._coordinator_invocation_pending = False
            return

        async with self._coordinator_lock:
            # Re-validate guards under the lock — between spawn and
            # acquisition the world may have changed.
            if self._coordinator_terminated:
                self._coordinator_invocation_pending = False
                self._record_coordinator_dedup("terminated_by_concurrent_task")
                return
            if self._deaf_warnings_consecutive < self._auto_bypass_threshold:
                self._coordinator_invocation_pending = False
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
            finally:
                self._coordinator_invocation_pending = False

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
        self._track_tts_task(task)
        try:
            return await task
        finally:
            self._untrack_tts_task(task)

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

    def _track_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Register an in-flight TTS synthesis task for T1 cancellation.

        Called by :meth:`speak`, :meth:`stream_text`, and
        :meth:`flush_stream` whenever they spawn a TTS coroutine. The
        task removes itself in its own ``finally`` via
        :meth:`_untrack_tts_task` so the set stays bounded by the
        in-flight set, not the lifetime of the daemon.
        """
        self._in_flight_tts_tasks.add(task)

    def _untrack_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Remove ``task`` from the in-flight set. Safe to call multiple times."""
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
                    "voice.step_output_flush": step_results["output_flush"],
                    "voice.step_tts_tasks_cancel": step_results["tts_tasks_cancel"],
                    "voice.step_llm_cancel": step_results["llm_cancel"],
                    "voice.step_filler_and_gate": step_results["filler_and_gate"],
                },
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
