"""CloudSTT — OpenAI Whisper API fallback for speech-to-text.

BYOK (Bring Your Own Key) integration with OpenAI's Whisper API.
Used as fallback when local STT confidence is below threshold.

Ref: SPE-010 §5 (STT), IMPL-SUP-008 (cloud infra)
"""

from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.observability.logging import get_logger
from sovyx.voice.stt import (
    _DEFAULT_SAMPLE_RATE,
    PartialTranscription,
    STTEngine,
    STTState,
    TranscriptionResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WHISPER_MODEL = "whisper-1"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.6
_API_TIMEOUT_S = 30.0
_MAX_AUDIO_DURATION_S = 120.0
_WAV_SAMPLE_WIDTH = 2  # 16-bit PCM


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloudSTTConfig:
    """Configuration for CloudSTT (OpenAI Whisper API).

    Attributes:
        api_key: OpenAI API key (BYOK). Required.
        model: Whisper model name (default: whisper-1).
        language: ISO 639-1 language hint for transcription.
        confidence_threshold: Local STT confidence below this triggers cloud
            fallback. Range 0.0–1.0.
        api_timeout: Timeout for API requests in seconds.
        api_base_url: Base URL for OpenAI API (allows custom endpoints).
    """

    api_key: str = ""
    """OpenAI API key. Must be set for cloud STT to work."""

    model: str = _WHISPER_MODEL
    """Whisper model name."""

    language: str = "en"
    """ISO 639-1 language hint."""

    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD
    """Confidence below this triggers cloud fallback."""

    api_timeout: float = _API_TIMEOUT_S
    """API request timeout in seconds."""

    api_base_url: str = "https://api.openai.com/v1"
    """Base URL for OpenAI-compatible API."""

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.confidence_threshold < 0.0 or self.confidence_threshold > 1.0:
            msg = "confidence_threshold must be between 0.0 and 1.0"
            raise ValueError(msg)
        if self.api_timeout <= 0:
            msg = "api_timeout must be positive"
            raise ValueError(msg)
        if not self.model:
            msg = "model must not be empty"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audio_to_wav_bytes(
    audio: np.ndarray,
    sample_rate: int,
) -> bytes:
    """Convert numpy audio array to WAV bytes (16-bit PCM).

    Args:
        audio: Audio samples as numpy array (float32 or int16).
        sample_rate: Sample rate in Hz.

    Returns:
        WAV file contents as bytes.
    """
    import numpy as _np  # noqa: F811

    # Normalize to float32 range [-1.0, 1.0] if needed
    if audio.dtype == _np.int16:
        samples = audio.astype(_np.float32) / 32768.0
    elif audio.dtype == _np.float64:
        samples = audio.astype(_np.float32)
    else:
        samples = audio.astype(_np.float32) if audio.dtype != _np.float32 else audio

    # Clip and convert to int16
    samples = _np.clip(samples, -1.0, 1.0)
    int16_samples = (samples * 32767).astype(_np.int16)

    # Write WAV
    buf = io.BytesIO()
    num_samples = len(int16_samples)
    data_size = num_samples * _WAV_SAMPLE_WIDTH
    # RIFF header
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    # fmt chunk
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))  # chunk size
    buf.write(struct.pack("<H", 1))  # PCM format
    buf.write(struct.pack("<H", 1))  # mono
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * _WAV_SAMPLE_WIDTH))  # byte rate
    buf.write(struct.pack("<H", _WAV_SAMPLE_WIDTH))  # block align
    buf.write(struct.pack("<H", 16))  # bits per sample
    # data chunk
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(int16_samples.tobytes())

    return buf.getvalue()


def needs_cloud_fallback(
    local_result: TranscriptionResult,
    threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
) -> bool:
    """Determine whether a local transcription should be re-done via cloud.

    Args:
        local_result: Result from local STT engine.
        threshold: Confidence threshold below which cloud fallback triggers.

    Returns:
        True if cloud fallback should be used.
    """
    if not local_result.text.strip():
        return True
    return local_result.confidence < threshold


# ---------------------------------------------------------------------------
# CloudSTT implementation
# ---------------------------------------------------------------------------


class CloudSTT(STTEngine):
    """OpenAI Whisper API speech-to-text via BYOK.

    Fallback engine: when local STT (Moonshine) produces low-confidence
    results, audio is re-transcribed via the Whisper API for higher accuracy.

    Features:
    - Reuses ``llm.openai_api_key`` from Sovyx config
    - HTTP/2 via httpx for efficient API calls
    - WAV encoding (16-bit PCM mono) for upload
    - Language hint for improved accuracy
    - Configurable confidence threshold for fallback triggering

    Usage::

        cloud = CloudSTT(CloudSTTConfig(api_key="sk-..."))
        await cloud.initialize()
        result = await cloud.transcribe(audio_array)
        await cloud.close()

    Ref: SPE-010 §5, IMPL-SUP-008
    """

    def __init__(self, config: CloudSTTConfig | None = None) -> None:
        self._config = config or CloudSTTConfig()
        self._state = STTState.UNINITIALIZED
        self._client: Any = None

    @property
    def state(self) -> STTState:
        """Current engine lifecycle state."""
        return self._state

    @property
    def config(self) -> CloudSTTConfig:
        """Active configuration."""
        return self._config

    async def initialize(self) -> None:
        """Validate API key and prepare HTTP client.

        Safe to call multiple times; only initializes once.

        Raises:
            RuntimeError: If engine was previously closed.
            ValueError: If API key is not configured.
        """
        if self._state in (STTState.READY, STTState.TRANSCRIBING):
            return

        if self._state == STTState.CLOSED:
            msg = "Cannot initialize a closed CloudSTT engine"
            raise RuntimeError(msg)

        if not self._config.api_key:
            msg = "OpenAI API key is required for CloudSTT"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self._config.api_base_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
            },
            timeout=httpx.Timeout(self._config.api_timeout),
        )
        self._state = STTState.READY

        logger.info(
            "CloudSTT initialized",
            model=self._config.model,
            language=self._config.language,
        )

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> TranscriptionResult:
        """Transcribe audio via OpenAI Whisper API.

        Args:
            audio: Audio samples as numpy array.
            sample_rate: Sample rate of input audio.

        Returns:
            TranscriptionResult with transcribed text and metadata.

        Raises:
            RuntimeError: If engine not initialized or closed.
            CloudSTTError: If API request fails.
        """
        self._ensure_ready()
        self._state = STTState.TRANSCRIBING

        try:
            start = time.monotonic()

            # Validate audio duration
            duration_s = len(audio) / sample_rate
            if duration_s > _MAX_AUDIO_DURATION_S:
                msg = f"Audio too long: {duration_s:.1f}s (max {_MAX_AUDIO_DURATION_S:.0f}s)"
                raise CloudSTTError(msg)

            # Convert to WAV
            wav_bytes = _audio_to_wav_bytes(audio, sample_rate)

            # Call API
            text = await self._call_whisper_api(wav_bytes)
            elapsed_ms = (time.monotonic() - start) * 1000

            logger.debug(
                "Cloud transcription complete",
                text_length=len(text),
                duration_ms=round(elapsed_ms, 1),
                audio_duration_s=round(duration_s, 1),
            )

            return TranscriptionResult(
                text=text.strip(),
                language=self._config.language,
                confidence=0.95,  # Cloud Whisper is high-confidence
                duration_ms=elapsed_ms,
            )
        finally:
            if self._state != STTState.CLOSED:
                self._state = STTState.READY

    async def transcribe_streaming(
        self,
        audio_stream: AsyncIterator[tuple[np.ndarray, int]],
    ) -> AsyncIterator[PartialTranscription]:
        """Streaming transcription via cloud.

        Cloud Whisper does not support true streaming — this collects all
        audio chunks, then transcribes in one shot. Yields a single final
        result.

        Args:
            audio_stream: Async iterator yielding (audio_chunk, sample_rate).

        Yields:
            Single PartialTranscription with is_final=True.
        """
        from sovyx.voice.stt import PartialTranscription

        self._ensure_ready()
        self._state = STTState.TRANSCRIBING

        try:
            import numpy as np  # noqa: F811

            chunks: list[np.ndarray] = []
            last_sr = _DEFAULT_SAMPLE_RATE

            async for chunk, sr in audio_stream:
                chunks.append(chunk)
                last_sr = sr

            if not chunks:
                yield PartialTranscription(
                    text="",
                    is_final=True,
                    confidence=0.0,
                )
                return

            combined = np.concatenate(chunks)
            result = await self.transcribe(combined, last_sr)

            yield PartialTranscription(
                text=result.text,
                is_final=True,
                confidence=result.confidence,
            )
        finally:
            if self._state != STTState.CLOSED:
                self._state = STTState.READY

    async def close(self) -> None:
        """Release HTTP client resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._state = STTState.CLOSED
        logger.info("CloudSTT closed")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Raise if engine is not in a usable state."""
        if self._state == STTState.UNINITIALIZED:
            msg = "CloudSTT not initialized — call initialize() first"
            raise RuntimeError(msg)
        if self._state == STTState.CLOSED:
            msg = "CloudSTT is closed"
            raise RuntimeError(msg)

    async def _call_whisper_api(self, wav_bytes: bytes) -> str:
        """Send audio to OpenAI Whisper API and return transcription text.

        Args:
            wav_bytes: WAV file content.

        Returns:
            Transcribed text.

        Raises:
            CloudSTTError: If API returns an error.
        """
        assert self._client is not None  # noqa: S101

        files = {
            "file": ("audio.wav", wav_bytes, "audio/wav"),
        }
        data: dict[str, str] = {
            "model": self._config.model,
        }
        if self._config.language:
            data["language"] = self._config.language

        try:
            response = await self._client.post(
                "/audio/transcriptions",
                files=files,
                data=data,
            )
        except Exception as exc:
            msg = f"Whisper API request failed: {exc}"
            raise CloudSTTError(msg) from exc

        if response.status_code != 200:  # noqa: PLR2004
            msg = f"Whisper API error {response.status_code}: {response.text[:200]}"
            raise CloudSTTError(msg)

        try:
            body = response.json()
        except Exception as exc:
            msg = f"Invalid JSON from Whisper API: {response.text[:200]}"
            raise CloudSTTError(msg) from exc

        text = body.get("text", "")
        if not isinstance(text, str):
            msg = f"Unexpected response format: {body}"
            raise CloudSTTError(msg)

        return text


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CloudSTTError(Exception):
    """Error during cloud STT transcription."""
