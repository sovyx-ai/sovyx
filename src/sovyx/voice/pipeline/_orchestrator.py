"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.events import EventBus
    from sovyx.voice.health._self_feedback import SelfFeedbackGate
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
# How many deaf heartbeats to tolerate *after* the exclusive-mode bypass
# was requested before declaring the bypass ineffective. Two heartbeats
# (~ 2 × heartbeat interval) is enough to absorb the device close/reopen
# round-trip without false-positiving — but short enough that operators
# learn fast when even WASAPI exclusive cannot recover the signal
# (firmware-level DSP, fixed-format virtual cable, broken element, ...).
_POST_BYPASS_DEAF_WARNINGS_BEFORE_INEFFECTIVE = 2


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
        on_capture_bypass_requested: Callable[[], Awaitable[None]] | None = None,
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
        self._on_capture_bypass_requested = on_capture_bypass_requested

        # Voice Clarity auto-bypass context. The factory resolves
        # ``voice_clarity_active`` from :mod:`sovyx.voice._apo_detector`
        # at pipeline creation; we cache it so the deaf-warning hook
        # can decide *fast* without re-reading the registry every
        # heartbeat. ``auto_bypass_enabled`` is the master kill switch
        # (``VoiceTuningConfig.voice_clarity_autofix``). ``_bypass_requested``
        # is the one-shot latch — after we ask the capture task to
        # re-open in exclusive mode (success or failure), we do not
        # ask again in this session to prevent oscillation.
        self._voice_clarity_active = voice_clarity_active
        self._auto_bypass_enabled = auto_bypass_enabled
        self._auto_bypass_threshold = max(1, auto_bypass_threshold)
        self._bypass_requested = False
        self._deaf_warnings_consecutive = 0
        # Post-bypass observability. When the orchestrator has already
        # asked the capture task to re-open in WASAPI exclusive mode and
        # we *still* see deaf heartbeats, the APO chain was not the (only)
        # cause — exclusive mode bypassed it but the post-driver signal
        # is still unusable for VAD. Emit a single
        # ``voice_apo_bypass_ineffective`` warning so the dashboard /
        # doctor can surface the actionable next step (manual disable in
        # Sound settings, swap mic, check firmware) instead of leaving the
        # operator believing the auto-fix worked.
        self._post_bypass_deaf_warnings = 0
        self._post_bypass_ineffective_emitted = False

        # Self-feedback isolation (ADR §4.4.6). Structural half-duplex
        # gating is encoded directly in the state machine (wake-word
        # only runs in IDLE, barge-in only in SPEAKING with a 5-frame
        # sustained threshold); this optional component adds mic
        # ducking around TTS. ``None`` means the factory didn't wire
        # a gate (tests, push-to-talk fallback) — the pipeline still
        # works, it just lacks the ducking layer.
        self._self_feedback_gate = self_feedback_gate

        # State
        self._state = VoicePipelineState.IDLE
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

        # Observability — feed_frame updates these on every frame so
        # _maybe_emit_heartbeat can answer "is VAD seeing real audio"
        # without per-frame log spam. Reset after each heartbeat.
        self._max_vad_prob_since_heartbeat: float = 0.0
        self._vad_frames_since_heartbeat: int = 0
        self._last_heartbeat_monotonic: float = 0.0

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
            await self._emit(WakeWordDetectedEvent(mind_id=self._config.mind_id))
            logger.info("Wake word detected", mind_id=self._config.mind_id)

            # Play confirmation beep
            if self._config.confirmation_tone == "beep":
                await self._jarvis.play_beep(self._output)

            await self._emit(SpeechStartedEvent(mind_id=self._config.mind_id))
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
            self._output.interrupt()
            self._cancel_filler()
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(BargeInEvent(mind_id=self._config.mind_id))
            logger.info("Barge-in detected", mind_id=self._config.mind_id)
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
            chunk = await self._tts.synthesize(text)
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
                chunk = await self._tts.synthesize(segment)
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

        # Keep incomplete segment in buffer
        self._text_buffer = segments[-1] if segments else ""

    async def flush_stream(self) -> None:
        """Flush remaining buffered text to TTS.

        Call when the LLM stream ends to synthesize the last segment.
        """
        if self._text_buffer.strip():
            try:
                chunk = await self._tts.synthesize(self._text_buffer)
                await self._output.enqueue(chunk)
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
            self._filler_task = asyncio.create_task(
                self._jarvis.play_filler_after_delay(self._output, self._first_token_event)
            )

    # -- Internal helpers ----------------------------------------------------

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
            self._maybe_request_capture_bypass()
            if self._bypass_requested:
                self._post_bypass_deaf_warnings += 1
                self._maybe_emit_bypass_ineffective()
        else:
            # Reset the consecutive counter so a single healthy heartbeat
            # between two deaf ones does not trigger the auto-bypass.
            self._deaf_warnings_consecutive = 0
            if self._bypass_requested:
                # A healthy heartbeat after the bypass means exclusive mode
                # restored the signal — clear the post-bypass counter so
                # transient deafness later (e.g. user yanking the headset)
                # is not erroneously reported as ineffective bypass.
                self._post_bypass_deaf_warnings = 0
        self._last_heartbeat_monotonic = now
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0

    def _maybe_request_capture_bypass(self) -> None:
        """Trigger Voice Clarity auto-bypass if the conditions are met.

        Called from the heartbeat path after a deaf warning is logged.
        All four guards must hold:

        * ``auto_bypass_enabled`` — master kill switch (tuning flag).
        * ``voice_clarity_active`` — detector confirmed a capture APO on
          the active endpoint (usually VocaEffectPack / Voice Clarity).
        * ``not _bypass_requested`` — one-shot latch per session.
        * ``_deaf_warnings_consecutive >= _auto_bypass_threshold`` —
          require multiple back-to-back deaf heartbeats so a single
          transient (e.g. device switch) does not flip us to exclusive.

        The callback (``on_capture_bypass_requested``) re-opens the
        capture stream in WASAPI exclusive mode, which bypasses the
        entire APO chain and restores a clean signal to the pipeline.
        """
        if not self._auto_bypass_enabled:
            return
        if not self._voice_clarity_active:
            return
        if self._bypass_requested:
            return
        if self._deaf_warnings_consecutive < self._auto_bypass_threshold:
            return
        if self._on_capture_bypass_requested is None:
            return

        self._bypass_requested = True
        logger.warning(
            "voice_apo_bypass_activated",
            mind_id=self._config.mind_id,
            reason="voice_clarity_deaf_pipeline",
            consecutive_deaf_warnings=self._deaf_warnings_consecutive,
            threshold=self._auto_bypass_threshold,
            action="reopen_capture_wasapi_exclusive",
        )
        # Schedule the restart on the running loop — we must not await
        # here because this helper runs on the per-frame hot path.
        asyncio.create_task(self._invoke_bypass_callback())

    def _maybe_emit_bypass_ineffective(self) -> None:
        """Emit ``voice_apo_bypass_ineffective`` once when exclusive mode didn't help.

        Called after every deaf heartbeat that happens *after* the
        bypass was requested. The emission is one-shot per session — we
        only need to tell the operator once that "exclusive mode opened
        but the signal is still dead". Repeated warnings would be noise
        on top of the per-heartbeat ``voice_pipeline_deaf_warning``.

        The dashboard's capture-diagnostics panel watches for this event
        to switch its messaging from "auto-fix in progress" to "auto-fix
        could not recover the signal — see manual remediation steps".
        """
        if self._post_bypass_ineffective_emitted:
            return
        if self._post_bypass_deaf_warnings < _POST_BYPASS_DEAF_WARNINGS_BEFORE_INEFFECTIVE:
            return
        self._post_bypass_ineffective_emitted = True
        logger.error(
            "voice_apo_bypass_ineffective",
            mind_id=self._config.mind_id,
            consecutive_post_bypass_deaf=self._post_bypass_deaf_warnings,
            voice_clarity_active=self._voice_clarity_active,
            hint=(
                "WASAPI exclusive re-open completed but VAD is still deaf. "
                "Likely causes: firmware-level DSP on the mic, a virtual "
                "audio cable with a fixed format, a damaged capture element, "
                "or a non-Voice-Clarity APO not in the detector catalog. "
                "Try manually disabling all enhancements in Windows Sound "
                "settings for the affected device, or switch capture device."
            ),
        )

    async def _invoke_bypass_callback(self) -> None:
        """Invoke the capture-bypass callback with full error isolation.

        A failure here must never crash the pipeline — if the exclusive
        re-open does not work we stay in shared mode (the latch is
        already set so we will not retry), log the failure, and let the
        user fall back to the manual APO-disable guidance in the
        dashboard / CLI doctor output.
        """
        callback = self._on_capture_bypass_requested
        if callback is None:
            return
        try:
            await callback()
        except Exception as exc:  # noqa: BLE001 — callback is user-supplied; we intentionally shield the pipeline from any failure mode
            logger.error(
                "voice_apo_bypass_failed",
                mind_id=self._config.mind_id,
                error=str(exc),
                error_type=type(exc).__name__,
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

    def reset(self) -> None:
        """Reset the pipeline to IDLE state (for testing or error recovery)."""
        self._state = VoicePipelineState.IDLE
        self._utterance_frames.clear()
        self._silence_counter = 0
        self._recording_counter = 0
        self._text_buffer = ""
        self._cancel_filler()
        self._output.clear()
