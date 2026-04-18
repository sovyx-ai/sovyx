"""MoonshineSTT — Speech-to-text using moonshine-voice library.

Wraps moonshine-voice (C++ core + ONNX Runtime) with Sovyx event system.
Supports full utterance and streaming transcription.

Ref: SPE-010 §5 (STT), IMPL-004 §2.1 (moonshine-voice API, breaking change)
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 16_000

# Defaults sourced from EngineConfig.tuning.voice; overridable via
# ``SOVYX_TUNING__VOICE__*``.
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning  # noqa: E402

_TRANSCRIBE_TIMEOUT_S = _VoiceTuning().transcribe_timeout_seconds
_STREAMING_DRAIN_S = _VoiceTuning().streaming_drain_seconds

# Model sizes and their characteristics (Pi 5 benchmarks)
_MODEL_SPECS: dict[str, dict[str, float | int]] = {
    "tiny": {"params_m": 34, "latency_ms": 237, "wer_pct": 12.0},
    "small": {"params_m": 123, "latency_ms": 527, "wer_pct": 7.84},
    "medium": {"params_m": 245, "latency_ms": 802, "wer_pct": 6.65},
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class STTState(IntEnum):
    """STT engine lifecycle states."""

    UNINITIALIZED = auto()
    READY = auto()
    TRANSCRIBING = auto()
    CLOSED = auto()


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Result of a full utterance transcription."""

    text: str
    """Transcribed text."""

    language: str | None = None
    """Detected or configured language code."""

    confidence: float = 0.0
    """Confidence score (0.0–1.0). Moonshine does not expose per-utterance
    confidence; we use fixed estimates per event type."""

    duration_ms: float = 0.0
    """Time spent in transcription (wall clock)."""

    segments: list[TranscriptionSegment] | None = None
    """Optional word-level or segment-level detail."""


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    """A segment within a transcription result."""

    text: str
    start_ms: float
    end_ms: float
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class PartialTranscription:
    """Incremental transcription from streaming mode."""

    text: str
    """Current transcription text (cumulative for the current line)."""

    is_final: bool = False
    """True when the utterance is finalized."""

    confidence: float = 0.0
    """Estimated confidence: 0.7 (started), 0.8 (changed), 0.95 (final)."""


@dataclass(frozen=True, slots=True)
class MoonshineConfig:
    """Configuration for MoonshineSTT.

    Defaults tuned for voice assistant latency on Pi 5:
    - Tiny model (34M params, 237ms/utterance, 12% WER)
    - 300ms partial update interval for streaming
    """

    language: str = "en"
    """ISO 639-1 language code."""

    model_size: str = "tiny"
    """Model variant: tiny (34M), small (123M), medium (245M)."""

    update_interval: float = 0.3
    """Seconds between partial transcription updates in streaming mode."""

    transcribe_timeout: float = _TRANSCRIBE_TIMEOUT_S
    """Timeout for full utterance transcription (seconds)."""

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.model_size not in _MODEL_SPECS:
            msg = f"Unknown model_size {self.model_size!r}; choose from {sorted(_MODEL_SPECS)}"
            raise ValueError(msg)
        if self.update_interval <= 0:
            msg = "update_interval must be positive"
            raise ValueError(msg)
        if self.transcribe_timeout <= 0:
            msg = "transcribe_timeout must be positive"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class STTEngine(ABC):
    """Abstract base for speech-to-text engines.

    Implementations:
    - MoonshineSTT (local, default)
    - CloudSTT (OpenAI Whisper API fallback — V05-25)
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Download model (if needed) and prepare for transcription."""

    @abstractmethod
    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> TranscriptionResult:
        """Transcribe a complete audio segment."""

    @abstractmethod
    def transcribe_streaming(
        self,
        audio_stream: AsyncIterator[tuple[np.ndarray, int]],
    ) -> AsyncIterator[PartialTranscription]:
        """Streaming transcription with incremental updates."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""


# ---------------------------------------------------------------------------
# Moonshine implementation
# ---------------------------------------------------------------------------


class MoonshineSTT(STTEngine):
    """Moonshine v2 STT using moonshine-voice library.

    Uses the moonshine-voice package (C++ core + embedded ONNX Runtime)
    instead of manual ONNX encoder/decoder inference. The library handles:
    - Model downloading and caching
    - Audio resampling (any rate → 16 kHz internally)
    - Feature extraction (C++ optimized)
    - Streaming inference with event-driven updates

    Performance (Pi 5, per utterance):
    - Tiny:   237ms, WER 12.0%, 34M params
    - Small:  527ms, WER 7.84%, 123M params
    - Medium: 802ms, WER 6.65%, 245M params

    Default: Tiny (best latency/accuracy tradeoff for voice assistant).

    Ref: IMPL-004 §1.1 + §2.1 (moonshine-voice API, breaking change from spec)
    """

    def __init__(self, config: MoonshineConfig | None = None) -> None:
        self._config = config or MoonshineConfig()
        self._transcriber: object | None = None
        self._state = STTState.UNINITIALIZED

    @property
    def state(self) -> STTState:
        """Current engine lifecycle state."""
        return self._state

    @property
    def config(self) -> MoonshineConfig:
        """Active configuration."""
        return self._config

    async def initialize(self) -> None:
        """Download model if needed and create transcriber.

        Safe to call multiple times; only initializes once. Model
        download + ONNX session construction are CPU/IO-bound and run
        inside :func:`asyncio.to_thread` so the factory's async call
        site does not stall the event loop — see CLAUDE.md anti-pattern
        #14 for the general rule.
        """
        if self._state in (STTState.READY, STTState.TRANSCRIBING):
            return

        if self._state == STTState.CLOSED:
            msg = "Cannot initialize a closed STT engine"
            raise RuntimeError(msg)

        logger.info(
            "Initializing MoonshineSTT",
            language=self._config.language,
            model_size=self._config.model_size,
        )

        def _load_transcriber() -> Any:  # noqa: ANN401
            from moonshine_voice import (
                Transcriber,
                get_model_for_language,
            )

            model_path, model_arch = get_model_for_language(
                self._config.language,
            )
            return Transcriber(
                model_path=model_path,
                model_arch=model_arch,
            )

        self._transcriber = await asyncio.to_thread(_load_transcriber)
        self._state = STTState.READY

        spec = _MODEL_SPECS[self._config.model_size]
        logger.info(
            "MoonshineSTT ready",
            model_size=self._config.model_size,
            params_m=spec["params_m"],
            expected_latency_ms=spec["latency_ms"],
        )

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> TranscriptionResult:
        """Transcribe a complete audio segment.

        Args:
            audio: Audio samples as numpy array (any dtype, converted internally).
            sample_rate: Sample rate of input audio (resampled to 16 kHz by library).

        Returns:
            TranscriptionResult with text and metadata.

        Raises:
            RuntimeError: If engine not initialized or closed.
        """
        import time

        self._ensure_ready()
        self._state = STTState.TRANSCRIBING

        try:
            start = time.monotonic()
            text = await self._transcribe_oneshot(audio, sample_rate)
            elapsed_ms = (time.monotonic() - start) * 1000

            logger.debug(
                "Transcription complete",
                text_length=len(text),
                duration_ms=round(elapsed_ms, 1),
            )

            return TranscriptionResult(
                text=text.strip(),
                language=self._config.language,
                confidence=0.9,
                duration_ms=elapsed_ms,
            )
        finally:
            if self._state != STTState.CLOSED:
                self._state = STTState.READY

    async def transcribe_streaming(
        self,
        audio_stream: AsyncIterator[tuple[np.ndarray, int]],
    ) -> AsyncIterator[PartialTranscription]:
        """Streaming transcription with incremental updates.

        Yields PartialTranscription events as audio arrives.
        The CogLoop can start Orient phase before utterance completes.

        Args:
            audio_stream: Async iterator yielding (audio_chunk, sample_rate) tuples.

        Yields:
            PartialTranscription with partial/final text and confidence.
        """
        self._ensure_ready()

        from moonshine_voice import TranscriptEventListener

        self._state = STTState.TRANSCRIBING

        queue: asyncio.Queue[PartialTranscription] = asyncio.Queue()

        class _StreamingListener(TranscriptEventListener):  # type: ignore[misc]
            def on_line_started(self, event: object) -> None:
                """Called when a new transcription line begins."""
                queue.put_nowait(
                    PartialTranscription(
                        text=event.line.text,  # type: ignore[attr-defined]
                        is_final=False,
                        confidence=0.7,
                    )
                )

            def on_line_text_changed(self, event: object) -> None:
                """Called when transcription text updates."""
                queue.put_nowait(
                    PartialTranscription(
                        text=event.line.text,  # type: ignore[attr-defined]
                        is_final=False,
                        confidence=0.8,
                    )
                )

            def on_line_completed(self, event: object) -> None:
                """Called when a transcription line is finalized."""
                queue.put_nowait(
                    PartialTranscription(
                        text=event.line.text,  # type: ignore[attr-defined]
                        is_final=True,
                        confidence=0.95,
                    )
                )

        assert self._transcriber is not None  # noqa: S101
        stream = self._transcriber.create_stream(  # type: ignore[attr-defined]
            update_interval=self._config.update_interval,
        )
        listener = _StreamingListener()
        stream.add_listener(listener)
        stream.start()

        try:
            async for audio_chunk, sample_rate in audio_stream:
                chunk_list = (
                    audio_chunk.tolist() if hasattr(audio_chunk, "tolist") else audio_chunk
                )
                stream.add_audio(chunk_list, sample_rate)

                while not queue.empty():
                    yield queue.get_nowait()

            stream.stop()

            # Drain remaining events after stop
            await asyncio.sleep(_STREAMING_DRAIN_S)
            while not queue.empty():
                yield queue.get_nowait()
        finally:
            stream.close()
            if self._state != STTState.CLOSED:
                self._state = STTState.READY

    async def close(self) -> None:
        """Release resources. Engine cannot be reused after close."""
        self._transcriber = None
        self._state = STTState.CLOSED
        logger.info("MoonshineSTT closed")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Raise if engine is not in a usable state."""
        if self._state == STTState.UNINITIALIZED:
            msg = "STT engine not initialized — call initialize() first"
            raise RuntimeError(msg)
        if self._state == STTState.CLOSED:
            msg = "STT engine is closed"
            raise RuntimeError(msg)

    async def _transcribe_oneshot(
        self,
        audio: np.ndarray,
        sample_rate: int,
    ) -> str:
        """Run a single full-utterance transcription via one-shot stream."""
        from moonshine_voice import TranscriptEventListener

        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[str] = loop.create_future()

        class _OneShotListener(TranscriptEventListener):  # type: ignore[misc]
            def on_line_completed(self, event: object) -> None:
                """Called when a transcription line is finalized."""
                if not result_future.done():
                    loop.call_soon_threadsafe(
                        result_future.set_result,
                        event.line.text,  # type: ignore[attr-defined]
                    )

        assert self._transcriber is not None  # noqa: S101
        stream = self._transcriber.create_stream(  # type: ignore[attr-defined]
            update_interval=0.1,
        )
        listener = _OneShotListener()
        stream.add_listener(listener)
        stream.start()

        audio_list = audio.tolist() if hasattr(audio, "tolist") else audio
        stream.add_audio(audio_list, sample_rate)
        stream.stop()

        try:
            text = await asyncio.wait_for(
                result_future,
                timeout=self._config.transcribe_timeout,
            )
        except TimeoutError:
            logger.warning("Transcription timed out")
            text = ""
        finally:
            stream.close()

        return text
