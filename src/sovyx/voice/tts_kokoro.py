"""KokoroTTS — near-commercial quality TTS via kokoro-onnx (82M ONNX q8).

Pipeline: text → kokoro-onnx (internal G2P + VITS2) → 24kHz float32 → int16 PCM.
Supports 60+ voices with style conditioning (speed control).

Quality tier: N100+ only — too heavy for Pi5 (~3x RT on Pi5 vs ~5x on desktop).

Ref: SPE-010 §6.2, IMPL-004 §2.3 (kokoro-onnx wrapper)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    measure_stage_duration,
    record_stage_event,
    record_tts_synthesis_latency,
)
from sovyx.voice._tts_sentence_split import (
    split_sentences as _split_sentences,
)
from sovyx.voice._tts_zero_energy import (
    TTS_RMS_FLOOR_DBFS as _TTS_RMS_FLOOR_DBFS,
)
from sovyx.voice._tts_zero_energy import (
    compute_rms_dbfs as _compute_rms_dbfs,
)
from sovyx.voice.tts_piper import AudioChunk, TTSEngine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 24000

# Model file names
_MODEL_FULL = "kokoro-v1.0.onnx"
_MODEL_Q8 = "kokoro-v1.0.int8.onnx"
_VOICES_FILE = "voices-v1.0.bin"


# ---------------------------------------------------------------------------
# T2 Ring 5 output-energy validation tuning
# ---------------------------------------------------------------------------
#
# Pre-T2 ``synthesize_with`` returned whatever Kokoro emitted, including
# silent buffers from a corrupt voice file or a degenerate ONNX session.
# The user heard inexplicable silence with no signal back to the
# pipeline. T2 measures the post-synthesis RMS in dBFS and emits a
# structured ``voice.tts.zero_energy_synthesis`` event when it falls
# below the perceptual silence floor for non-empty input. The returned
# ``AudioChunk.synthesis_health`` field carries the same signal so the
# orchestrator can trigger a Piper fallback (wired separately so this
# commit stays surgical).
#
# The threshold + RMS computation now live in the shared module
# ``sovyx.voice._tts_zero_energy`` (see T1.36 foundation, commit
# `710e1f1`) so Piper TTS — which must apply the identical gate per
# the master mission — can consume the same primitives without
# copy-paste drift. The legacy underscore-prefixed names are kept as
# import aliases above so the existing test suite at
# ``tests/unit/voice/test_tts_kokoro.py`` and any downstream patches
# keep working without an import-path migration.


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


# ---------------------------------------------------------------------------
# KokoroTTS implementation
# ---------------------------------------------------------------------------


class KokoroTTS(TTSEngine):
    """Kokoro-82M TTS via kokoro-onnx wrapper.

    Near-commercial quality, significantly better than Piper.
    Supports 60+ voices with style conditioning via voice vectors.

    Models:
    - ``kokoro-v1.0.onnx``: 300MB (full precision)
    - ``kokoro-v1.0.int8.onnx``: ~88MB (int8 quantized, minimal quality loss)
    - ``voices-v1.0.bin``: ~27MB (all voice style vectors)

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
        self._chunk_counter = 0
        # TS3 chaos injector — opt-in zero-energy injection at the
        # TTS_ZERO_ENERGY site. Disabled by default; chaos test
        # matrix sets the env vars to validate that the T2 energy-
        # validation guard fires + M2 DROP event lands with
        # error_type=zero_energy + the orchestrator triggers the
        # Kokoro→Piper fallback.
        self._chaos = ChaosInjector(site_id=ChaosSite.TTS_ZERO_ENERGY.value)

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

        The ONNX session is pinned to ``CPUExecutionProvider``. ``kokoro_onnx``
        auto-selects GPU providers when ``onnxruntime-gpu`` is installed or
        ``ONNX_PROVIDER`` is set, which can trigger WDDM TDR resets on Windows
        with unstable GPU drivers. We construct the session ourselves and pass
        it via :meth:`Kokoro.from_session` to match the Piper pinning policy.

        Raises:
            FileNotFoundError: If model or voice files are missing.
            RuntimeError: If kokoro-onnx fails to initialize.
        """
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        model_path = self._resolve_model_path()
        voices_path = self._model_dir / _VOICES_FILE

        if not voices_path.exists():
            msg = f"Kokoro voices file not found: {voices_path}"
            raise FileNotFoundError(msg)

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        try:
            session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self._kokoro = Kokoro.from_session(session, str(voices_path))
        except Exception as exc:  # noqa: BLE001
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
        """Synthesize text with the voice and language baked into :attr:`config`.

        Full pipeline: text → kokoro-onnx (G2P + VITS2) → int16 PCM audio.
        For empty text, returns an empty :class:`AudioChunk`.
        """
        return await self.synthesize_with(
            text,
            voice=self._config.voice,
            language=self._config.language,
            speed=self._config.speed,
        )

    async def synthesize_with(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float | None = None,
    ) -> AudioChunk:
        """Synthesize ``text`` using an explicit voice and language.

        The underlying kokoro-onnx model accepts voice + language per call,
        so callers can pick any of the 54 shipped voices without rebuilding
        the ONNX session. Used by the voice-test flow to let the setup
        wizard sample every voice in the catalog without a ~300 MB reload.
        """
        import time

        import numpy as np

        if not self._initialized:
            await self.initialize()

        text = text.strip()

        # Ring 6 RED + USE: every synthesize invocation flows through
        # M2's measure_stage_duration so dashboards see the full
        # latency distribution split by outcome. record_stage_event
        # below tags each return path with its specific kind /
        # error_type. Implicit exceptions from kokoro.create are
        # caught by measure_stage_duration's BaseException handler
        # and recorded as ERROR with the exception class name.
        with measure_stage_duration(VoiceStage.TTS) as _stage_token:
            if not text:
                # Empty input is a structured no-op, not a failure —
                # but it still produces no audio, so DROP not SUCCESS.
                # error_type=empty_text lets the dashboard distinguish
                # "no audio because nothing to say" from real failures.
                record_stage_event(
                    VoiceStage.TTS,
                    StageEventKind.DROP,
                    error_type="empty_text",
                )
                return AudioChunk(
                    audio=np.array([], dtype=np.int16),
                    sample_rate=_DEFAULT_SAMPLE_RATE,
                    duration_ms=0.0,
                )

            if self._kokoro is None:
                msg = "KokoroTTS not initialized"
                raise RuntimeError(msg)

            resolved_speed = self._config.speed if speed is None else speed

            # Kokoro's `create` runs G2P + ONNX VITS2 synchronously and
            # is CPU-bound (multiple seconds for a long sentence).
            # Offload to a worker thread so the event loop stays
            # responsive.
            gen_start = time.monotonic()
            samples, sample_rate = await asyncio.to_thread(
                self._kokoro.create,
                text,
                voice=voice,
                speed=resolved_speed,
                lang=language,
            )
            generation_ms = (time.monotonic() - gen_start) * 1000

            # Convert float32 → int16 PCM
            audio_int16: np.ndarray = np.clip(
                samples * 32768.0,
                -32768,
                32767,
            ).astype(np.int16)

            # TS3 chaos: opt-in zero-energy injection at the
            # TTS_ZERO_ENERGY site. When SOVYX_CHAOS__ENABLED=true
            # AND SOVYX_CHAOS__INJECT_TTS_ZERO_ENERGY_PCT > 0, the
            # synthesised audio is overwritten with zeros — the T2
            # RMS-floor check below will detect the silence, mark
            # synthesis_health=zero_energy, and trigger the
            # orchestrator's Piper fallback. Validates the T2 +
            # M2-DROP + fallback path under realistic operating
            # conditions, not just the unit-test mock that returns
            # zeros deterministically.
            if self._chaos.should_inject():
                audio_int16 = np.zeros_like(audio_int16)

            duration_ms = len(audio_int16) / sample_rate * 1000

            # T2 Ring 5 output-energy validation. The synthesis pipeline
            # MUST produce audible output for non-empty input — silent
            # output is a structural failure (corrupt voice file, ONNX
            # session degeneration) and must surface explicitly so the
            # orchestrator can trigger the Piper fallback. The chunk's
            # ``synthesis_health`` field carries the verdict; structured
            # WARNING fires for dashboards.
            rms_dbfs = _compute_rms_dbfs(audio_int16)
            synthesis_health: str | None = None
            if rms_dbfs < _TTS_RMS_FLOOR_DBFS:
                synthesis_health = "zero_energy"
                logger.warning(
                    "voice.tts.zero_energy_synthesis",
                    **{
                        "voice.text_chars": len(text),
                        "voice.audio_ms": round(duration_ms, 1),
                        "voice.generation_ms": round(generation_ms, 1),
                        "voice.measured_rms_dbfs": (
                            round(rms_dbfs, 2) if rms_dbfs != float("-inf") else "-inf"
                        ),
                        "voice.rms_floor_dbfs": _TTS_RMS_FLOOR_DBFS,
                        "voice.model": "kokoro",
                        "voice.voice": voice,
                        "voice.language": language,
                        "voice.sample_rate": sample_rate,
                        "voice.action_required": (
                            "fallback_to_piper_or_re_check_voice_file_integrity"
                        ),
                    },
                )

            self._chunk_counter += 1
            logger.info(
                "voice.tts.chunk_emitted",
                **{
                    "voice.chunk_index": self._chunk_counter,
                    "voice.text_chars": len(text),
                    "voice.audio_ms": round(duration_ms, 1),
                    "voice.generation_ms": round(generation_ms, 1),
                    "voice.synthesis_latency_ms": round(generation_ms, 1),
                    "voice.model": "kokoro",
                    "voice.voice": voice,
                    "voice.language": language,
                    "voice.sample_rate": sample_rate,
                    "voice.speed": resolved_speed,
                    "voice.synthesis_health": synthesis_health or "ok",
                },
            )
            # T1.37 — bucketed-family histogram for per-language TTS
            # latency (cardinality-bounded ~25 series). Per-voice
            # detail lives on the chunk_emitted log above.
            record_tts_synthesis_latency(
                voice,
                generation_ms,
                engine="kokoro",
                error=synthesis_health == "zero_energy",
            )

            # Zero-energy synthesis is a soft failure: the caller
            # already knows to fall back to Piper, but the
            # observability layer should record it as DROP with the
            # specific error_type so the dashboard can attribute the
            # rate of structural-output failures.
            if synthesis_health == "zero_energy":
                _stage_token.mark_error()
                record_stage_event(
                    VoiceStage.TTS,
                    StageEventKind.DROP,
                    error_type="zero_energy",
                )
            else:
                record_stage_event(VoiceStage.TTS, StageEventKind.SUCCESS)

            return AudioChunk(
                audio=audio_int16,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                synthesis_health=synthesis_health,
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
        except (RuntimeError, AttributeError, OSError):
            # RuntimeError: Kokoro's internal ONNX / inference errors.
            # AttributeError: upstream API drift (get_voices removed
            # or renamed). OSError: voice-file access failure.
            # Returning [] lets the caller render "no voices" instead
            # of crashing; traceback surfaces persistent issues.
            logger.warning("Failed to list Kokoro voices", exc_info=True)
            return []

        return sorted(voices)
