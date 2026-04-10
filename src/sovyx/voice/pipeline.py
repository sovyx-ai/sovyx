"""VoicePipeline orchestrator: mic → VAD → wake → STT → LLM → TTS → speaker.

State-machine-driven pipeline that chains voice components into a continuous
listening/speaking loop.  Supports barge-in (user interrupts TTS), Jarvis-style
filler injection, and streaming TTS from LLM token output.

Ref: SPE-010 §8 (VoicePipeline), §13 (state machine), IMPL-004 §1.7 (timing)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.events import EventBus
    from sovyx.voice.stt import STTEngine
    from sovyx.voice.tts_piper import AudioChunk, TTSEngine
    from sovyx.voice.vad import SileroVAD, VADEvent
    from sovyx.voice.wake_word import WakeWordDetector

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32ms at 16kHz
_SILENCE_FRAMES_END = 22  # ~700ms silence → end of utterance
_MAX_RECORDING_FRAMES = 312  # ~10s max recording
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160ms sustained speech → barge-in
_FILLER_DELAY_MS = 800  # Play filler if no LLM token within this
_TEXT_MIN_WORDS = 3  # Min words before TTS synthesis


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class VoicePipelineState(Enum):
    """Pipeline state machine.

    Transitions (SPE-010 §13):
        IDLE → WAKE_DETECTED → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE
    Barge-in: SPEAKING → RECORDING (skip wake word — already engaged).
    Timeout:  RECORDING → IDLE (10s max).
    Empty:    TRANSCRIBING → IDLE (empty transcription).
    """

    IDLE = auto()
    WAKE_DETECTED = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()


# ---------------------------------------------------------------------------
# Events (emitted via EventBus)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WakeWordDetectedEvent:
    """Emitted when the wake word is detected."""

    mind_id: str = ""


@dataclass(frozen=True, slots=True)
class SpeechStartedEvent:
    """Emitted when speech recording begins (after wake word)."""

    mind_id: str = ""


@dataclass(frozen=True, slots=True)
class SpeechEndedEvent:
    """Emitted when speech recording ends (silence detected)."""

    mind_id: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class TranscriptionCompletedEvent:
    """Emitted when STT produces a transcription."""

    text: str = ""
    confidence: float = 0.0
    language: str | None = None
    latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class TTSStartedEvent:
    """Emitted when TTS playback begins."""

    mind_id: str = ""


@dataclass(frozen=True, slots=True)
class TTSCompletedEvent:
    """Emitted when TTS playback finishes."""

    mind_id: str = ""


@dataclass(frozen=True, slots=True)
class BargeInEvent:
    """Emitted when the user interrupts TTS playback."""

    mind_id: str = ""


@dataclass(frozen=True, slots=True)
class PipelineErrorEvent:
    """Emitted on unrecoverable pipeline errors."""

    mind_id: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoicePipelineConfig:
    """Configuration for the VoicePipeline orchestrator.

    Attributes:
        mind_id: Owning mind identifier.
        wake_word_enabled: Whether to require wake word before recording.
        barge_in_enabled: Whether user can interrupt TTS by speaking.
        fillers_enabled: Whether to play filler phrases during LLM thinking.
        filler_delay_ms: Milliseconds to wait before playing a filler.
        silence_frames_end: Consecutive silent frames to end utterance (~32ms each).
        max_recording_frames: Maximum frames before force-ending recording.
        barge_in_threshold: Consecutive speech frames to trigger barge-in.
        confirmation_tone: Type of tone on wake word (``"beep"`` or ``"none"``).
        filler_phrases: Phrases used during LLM thinking time.
    """

    mind_id: str = "default"
    wake_word_enabled: bool = True
    barge_in_enabled: bool = True
    fillers_enabled: bool = True
    filler_delay_ms: int = _FILLER_DELAY_MS
    silence_frames_end: int = _SILENCE_FRAMES_END
    max_recording_frames: int = _MAX_RECORDING_FRAMES
    barge_in_threshold: int = _BARGE_IN_THRESHOLD_FRAMES
    confirmation_tone: str = "beep"
    filler_phrases: tuple[str, ...] = (
        "Let me think about that...",
        "Hmm...",
        "One moment...",
        "Let me check...",
        "Sure, let me look into that...",
    )


def validate_config(config: VoicePipelineConfig) -> None:
    """Validate pipeline configuration.

    Raises:
        ValueError: If any parameter is out of range.
    """
    if config.filler_delay_ms < 0:
        msg = f"filler_delay_ms must be >= 0, got {config.filler_delay_ms}"
        raise ValueError(msg)
    if config.silence_frames_end < 1:
        msg = f"silence_frames_end must be >= 1, got {config.silence_frames_end}"
        raise ValueError(msg)
    if config.max_recording_frames < 1:
        msg = f"max_recording_frames must be >= 1, got {config.max_recording_frames}"
        raise ValueError(msg)
    if config.barge_in_threshold < 1:
        msg = f"barge_in_threshold must be >= 1, got {config.barge_in_threshold}"
        raise ValueError(msg)
    if config.confirmation_tone not in ("beep", "none"):
        msg = f"confirmation_tone must be 'beep' or 'none', got {config.confirmation_tone!r}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# AudioOutputQueue — managed playback with interruption
# ---------------------------------------------------------------------------


class AudioOutputQueue:
    """Queue-based audio output with interruption support.

    Manages a FIFO of :class:`AudioChunk` objects and plays them
    sequentially.  :meth:`interrupt` clears the queue and stops
    current playback (used for barge-in).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self._playing = False
        self._interrupted = False

    @property
    def is_playing(self) -> bool:
        """Whether audio is currently being played."""
        return self._playing

    async def enqueue(self, chunk: AudioChunk) -> None:
        """Add an audio chunk to the playback queue.

        Args:
            chunk: Audio data to play.
        """
        await self._queue.put(chunk)

    async def play_immediate(self, chunk: AudioChunk) -> None:
        """Play a single chunk immediately (blocking until done).

        Args:
            chunk: Audio data to play.
        """
        self._playing = True
        try:
            await _play_audio(chunk)
        finally:
            self._playing = False

    async def drain(self) -> None:
        """Play all queued chunks sequentially until queue is empty."""
        self._playing = True
        self._interrupted = False
        try:
            while not self._queue.empty() and not self._interrupted:
                chunk = self._queue.get_nowait()
                await _play_audio(chunk)
        finally:
            self._playing = False
            self._interrupted = False

    def interrupt(self) -> None:
        """Stop current playback and clear the queue (barge-in)."""
        self._interrupted = True
        # Drain queue without awaiting
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def clear(self) -> None:
        """Clear pending chunks without interrupting current playback."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


async def _play_audio(chunk: AudioChunk) -> None:
    """Play an audio chunk via sounddevice (or simulate in test).

    This is the low-level playback function.  In production it uses
    ``sounddevice``; unit tests can patch this function.

    Args:
        chunk: The audio chunk to play.
    """
    try:
        import sounddevice as sd

        sd.play(chunk.audio, chunk.sample_rate)
        sd.wait()
    except ImportError:
        # Headless / test environment — simulate playback duration
        if chunk.duration_ms > 0:
            await asyncio.sleep(chunk.duration_ms / 1000)


# ---------------------------------------------------------------------------
# BargeInDetector
# ---------------------------------------------------------------------------


class BargeInDetector:
    """Detects when the user speaks while TTS is playing (barge-in).

    Monitors the VAD while :class:`AudioOutputQueue` is playing.
    If consecutive speech frames exceed the threshold, triggers
    barge-in by interrupting the output queue.

    Args:
        vad: The voice-activity detector.
        output: The audio output queue to interrupt on barge-in.
        threshold_frames: Consecutive speech frames needed to trigger.
    """

    def __init__(
        self,
        vad: SileroVAD,
        output: AudioOutputQueue,
        threshold_frames: int = _BARGE_IN_THRESHOLD_FRAMES,
    ) -> None:
        self._vad = vad
        self._output = output
        self._threshold = threshold_frames

    def check_frame(self, frame: npt.NDArray[np.int16]) -> bool:
        """Process one audio frame and return True if barge-in detected.

        Args:
            frame: Audio frame (512 samples, 16-bit PCM, 16kHz).

        Returns:
            ``True`` if barge-in threshold was reached.
        """
        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        event = self._vad.process_frame(audio_f32)
        return event.is_speech

    async def monitor(
        self,
        get_frame: Callable[[], npt.NDArray[np.int16] | None],
    ) -> bool:
        """Monitor for barge-in while output is playing.

        Args:
            get_frame: Callable that returns the next audio frame or None.

        Returns:
            ``True`` if barge-in was detected and output was interrupted.
        """
        consecutive = 0
        while self._output.is_playing:
            frame = get_frame()
            if frame is None:
                await asyncio.sleep(0.01)
                continue
            if self.check_frame(frame):
                consecutive += 1
                if consecutive >= self._threshold:
                    self._output.interrupt()
                    return True
            else:
                consecutive = 0
            await asyncio.sleep(0)  # Yield to event loop
        return False


# ---------------------------------------------------------------------------
# JarvisIllusion — re-exported from jarvis.py (V05-24)
# ---------------------------------------------------------------------------

from sovyx.voice.jarvis import (  # noqa: E402
    JarvisConfig,
    JarvisIllusion,
    split_at_boundaries,
)

__all_jarvis__ = ["JarvisIllusion", "JarvisConfig", "split_at_boundaries"]


# ---------------------------------------------------------------------------
# VoicePipeline — main orchestrator
# ---------------------------------------------------------------------------


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
    ) -> None:
        validate_config(config)
        self._config = config
        self._vad = vad
        self._wake_word = wake_word
        self._stt = stt
        self._tts = tts
        self._event_bus = event_bus
        self._on_perception = on_perception

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

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the pipeline and pre-cache fillers.

        Call this before feeding frames.
        """
        await self._jarvis.pre_cache()
        self._running = True
        self._state = VoicePipelineState.IDLE
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
        vad_event = self._vad.process_frame(audio_f32)

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

        # Run wake word detector
        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        ww_event = self._wake_word.process_frame(audio_f32)

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

        if vad_event.is_speech and self._output.is_playing and self._barge_in.check_frame(frame):
            self._output.interrupt()
            self._cancel_filler()
            await self._emit(BargeInEvent(mind_id=self._config.mind_id))
            logger.info("Barge-in detected", mind_id=self._config.mind_id)
            return await self._transition_to_recording(frame)

        if not self._output.is_playing:
            # Playback finished
            self._state = VoicePipelineState.IDLE
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
        except Exception as exc:
            logger.error("STT failed", error=str(exc))
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"STT failed: {exc}",
                )
            )
            self._state = VoicePipelineState.IDLE
            return {"state": "IDLE", "event": "stt_error", "error": str(exc)}

        latency_ms = (time.monotonic() - start) * 1000

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
            try:
                await self._on_perception(result.text, self._config.mind_id)
            except Exception as exc:
                logger.error("Perception callback failed", error=str(exc))

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
        await self._emit(TTSStartedEvent(mind_id=self._config.mind_id))

        try:
            chunk = await self._tts.synthesize(text)
            await self._output.play_immediate(chunk)
        except Exception as exc:
            logger.error("TTS failed", error=str(exc))
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"TTS failed: {exc}",
                )
            )
        finally:
            self._state = VoicePipelineState.IDLE
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
            except Exception as exc:
                logger.warning("Stream TTS failed", error=str(exc))

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
            except Exception as exc:
                logger.warning("Flush TTS failed", error=str(exc))
        self._text_buffer = ""

        # Drain all queued audio
        await self._output.drain()

        self._state = VoicePipelineState.IDLE
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

    async def _emit(self, event: object) -> None:
        """Emit an event via the event bus (if available)."""
        if self._event_bus is not None:
            try:
                await self._event_bus.emit(event)  # type: ignore[arg-type]
            except Exception:
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
