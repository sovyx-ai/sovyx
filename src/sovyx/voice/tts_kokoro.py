"""KokoroTTS — near-commercial quality TTS via kokoro-onnx (82M ONNX q8).

Pipeline: text → kokoro-onnx (internal G2P + VITS2) → 24kHz float32 → int16 PCM.
Supports 60+ voices with style conditioning (speed control).

Quality tier: N100+ only — too heavy for Pi5 (~3x RT on Pi5 vs ~5x on desktop).

Ref: SPE-010 §6.2, IMPL-004 §2.3 (kokoro-onnx wrapper)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.tts_piper import AudioChunk, TTSEngine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 24000
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Model file names
_MODEL_FULL = "kokoro-v1.0.onnx"
_MODEL_Q8 = "kokoro-v1.0-q8.onnx"
_VOICES_FILE = "voices-v1.0.bin"

# Supported languages (kokoro-onnx)
_SUPPORTED_LANGUAGES = frozenset(
    {
        "en-us",
        "en-gb",
        "ja",
        "zh",
        "ko",
        "fr",
        "es",
        "hi",
        "it",
        "pt-br",
    }
)

# Speed range (kokoro-onnx enforces internally, but we validate early)
_MIN_SPEED = 0.1
_MAX_SPEED = 5.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KokoroConfig:
    """Configuration for KokoroTTS.

    Attributes:
        voice: Voice style name (e.g., ``af_bella``, ``am_adam``).
            Prefix convention: af=American Female, am=American Male,
            bf=British Female, bm=British Male, etc.
        speed: Speed multiplier. 0.5=slow, 1.0=normal, 2.0=fast.
        language: Language code (e.g., ``en-us``, ``ja``, ``fr``).
        quantized: If True, prefer q8 model (80MB vs 300MB full precision).
    """

    voice: str = "af_bella"
    speed: float = 1.0
    language: str = "en-us"
    quantized: bool = True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_config(config: KokoroConfig) -> None:
    """Validate KokoroConfig parameters.

    Raises:
        ValueError: If any parameter is out of acceptable range.
    """
    if not config.voice:
        msg = "voice must be a non-empty string"
        raise ValueError(msg)
    if config.speed < _MIN_SPEED or config.speed > _MAX_SPEED:
        msg = f"speed must be in [{_MIN_SPEED}, {_MAX_SPEED}], got {config.speed}"
        raise ValueError(msg)
    if not config.language:
        msg = "language must be a non-empty string"
        raise ValueError(msg)


def _split_sentences(text: str) -> list[str]:
    """Split text on sentence boundaries (``.``, ``!``, ``?`` followed by whitespace)."""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return parts if parts else [text]


# ---------------------------------------------------------------------------
# KokoroTTS implementation
# ---------------------------------------------------------------------------


class KokoroTTS(TTSEngine):
    """Kokoro-82M TTS via kokoro-onnx wrapper.

    Near-commercial quality, significantly better than Piper.
    Supports 60+ voices with style conditioning via voice vectors.

    Models:
    - ``kokoro-v1.0.onnx``: 300MB (full precision)
    - ``kokoro-v1.0-q8.onnx``: ~80MB (quantized, minimal quality loss)
    - ``voices-v1.0.bin``: ~15MB (all voice style vectors)

    Performance:
    - ~3x real-time on Pi 5 (q8)
    - ~5x real-time on desktop/N100+
    - ~10x real-time with CUDA

    Quality tier: N100+ recommended. Use :class:`PiperTTS` for Pi5.

    Output: 24000 Hz, 16-bit mono PCM.
    """

    def __init__(
        self,
        model_dir: Path,
        config: KokoroConfig | None = None,
    ) -> None:
        _config = config or KokoroConfig()
        _validate_config(_config)

        self._config = _config
        self._model_dir = Path(model_dir)
        self._kokoro: Any | None = None
        self._initialized = False

    # -- Properties --------------------------------------------------------

    @property
    def config(self) -> KokoroConfig:
        """Current configuration (read-only)."""
        return self._config

    @property
    def is_initialized(self) -> bool:
        """Whether the model is loaded and ready."""
        return self._initialized

    @property
    def sample_rate(self) -> int:
        """Audio sample rate — always 24000 Hz for Kokoro."""
        return _DEFAULT_SAMPLE_RATE

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Load Kokoro ONNX model and voice data.

        Prefers quantized (q8) model when ``config.quantized`` is True and
        the file exists, falling back to full-precision model.

        Raises:
            FileNotFoundError: If model or voice files are missing.
            RuntimeError: If kokoro-onnx fails to initialize.
        """
        from kokoro_onnx import Kokoro

        # Resolve model path (prefer q8 if configured)
        model_path = self._resolve_model_path()
        voices_path = self._model_dir / _VOICES_FILE

        if not voices_path.exists():
            msg = f"Kokoro voices file not found: {voices_path}"
            raise FileNotFoundError(msg)

        try:
            self._kokoro = Kokoro(str(model_path), str(voices_path))
        except Exception as exc:
            msg = f"Failed to initialize Kokoro: {exc}"
            raise RuntimeError(msg) from exc

        self._initialized = True
        logger.info(
            "KokoroTTS initialized",
            voice=self._config.voice,
            speed=self._config.speed,
            language=self._config.language,
            model=model_path.name,
            quantized=self._config.quantized,
        )

    async def close(self) -> None:
        """Release Kokoro resources."""
        self._kokoro = None
        self._initialized = False
        logger.info("KokoroTTS closed")

    # -- Internal ----------------------------------------------------------

    def _resolve_model_path(self) -> Path:
        """Resolve the ONNX model file path.

        If quantized is True, prefer q8 model. Fall back to full precision.

        Returns:
            Path to the resolved model file.

        Raises:
            FileNotFoundError: If no model file is found.
        """
        if self._config.quantized:
            q8_path = self._model_dir / _MODEL_Q8
            if q8_path.exists():
                return q8_path
            # Fall through to full precision — log warning
            full_path = self._model_dir / _MODEL_FULL
            if full_path.exists():
                logger.warning(
                    "Quantized model not found, using full precision",
                    expected=_MODEL_Q8,
                    using=_MODEL_FULL,
                )
                return full_path
        else:
            full_path = self._model_dir / _MODEL_FULL
            if full_path.exists():
                return full_path

        msg = f"Kokoro model not found in {self._model_dir}. Expected {_MODEL_Q8} or {_MODEL_FULL}"
        raise FileNotFoundError(msg)

    # -- Public API --------------------------------------------------------

    async def synthesize(self, text: str) -> AudioChunk:
        """Synthesize text to audio.

        Full pipeline: text → kokoro-onnx (G2P + VITS2) → int16 PCM audio.

        For empty text, returns an empty AudioChunk.

        Args:
            text: The text to synthesize.

        Returns:
            AudioChunk with int16 PCM audio at 24000 Hz.
        """
        import numpy as np

        if not self._initialized:
            await self.initialize()

        text = text.strip()

        if not text:
            return AudioChunk(
                audio=np.array([], dtype=np.int16),
                sample_rate=_DEFAULT_SAMPLE_RATE,
                duration_ms=0.0,
            )

        if self._kokoro is None:
            msg = "KokoroTTS not initialized"
            raise RuntimeError(msg)

        # Kokoro's `create` runs G2P + ONNX VITS2 synchronously and is
        # CPU-bound (multiple seconds for a long sentence). Offload to a
        # worker thread so the event loop stays responsive.
        samples, sample_rate = await asyncio.to_thread(
            self._kokoro.create,
            text,
            voice=self._config.voice,
            speed=self._config.speed,
            lang=self._config.language,
        )

        # Convert float32 → int16 PCM
        audio_int16: np.ndarray = np.clip(
            samples * 32768.0,
            -32768,
            32767,
        ).astype(np.int16)

        duration_ms = len(audio_int16) / sample_rate * 1000

        return AudioChunk(
            audio=audio_int16,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
        )

    async def synthesize_streaming(
        self,
        text_stream: AsyncIterator[str],
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesis — yield audio per sentence as text arrives.

        Key for Jarvis Illusion: start speaking before full LLM response.
        Splits on sentence boundaries (``.``, ``!``, ``?`` followed by whitespace).

        Args:
            text_stream: Async iterator yielding text chunks.

        Yields:
            AudioChunk per complete sentence.
        """
        if not self._initialized:
            await self.initialize()

        buffer = ""

        async for text_chunk in text_stream:
            buffer += text_chunk

            sentences = _split_sentences(buffer)

            # Synthesize complete sentences (all except the last, which may be incomplete)
            for sentence in sentences[:-1]:
                stripped = sentence.strip()
                if stripped:
                    chunk = await self.synthesize(stripped)
                    yield chunk

            # Keep last (potentially incomplete) sentence in buffer
            buffer = sentences[-1] if sentences else ""

        # Final sentence
        if buffer.strip():
            yield await self.synthesize(buffer.strip())

    def list_voices(self) -> list[str]:
        """List available voices from the loaded Kokoro model.

        Returns:
            Sorted list of voice names, or empty list if not initialized.
        """
        if self._kokoro is None:
            return []

        try:
            voices: list[str] = self._kokoro.get_voices()
        except Exception:
            logger.warning("Failed to list Kokoro voices")
            return []

        return sorted(voices)
