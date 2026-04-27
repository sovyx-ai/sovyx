"""PiperTTS — fast local TTS via VITS ONNX model with espeak-ng phonemizer.

Pipeline: text → espeak-ng phonemize → phoneme IDs → ONNX (VITS) → 22.05kHz int16 audio.
Supports multi-voice via model swap and multi-speaker models via speaker ID.

Ref: SPE-010 §6.2, IMPL-004 §2.2 (complete Piper pipeline)
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    measure_stage_duration,
    record_stage_event,
)
from sovyx.voice._tts_sentence_split import (
    split_sentences as _split_sentences,
)
from sovyx.voice._tts_zero_energy import TTS_RMS_FLOOR_DBFS, compute_rms_dbfs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 22050
_MAX_PHONEME_IDS = 50_000  # Safety limit for phoneme ID sequences
_BOS = "^"  # Beginning of sequence
_EOS = "$"  # End of sequence
_PAD = "_"  # Padding (between phonemes — critical for VITS alignment)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A chunk of PCM audio produced by a TTS engine.

    Attributes:
        audio: int16 PCM samples (mono).
        sample_rate: Samples per second (typically 22050 for Piper, 24000 for Kokoro).
        duration_ms: Duration in milliseconds.
        synthesis_health: T2 (Ring 5) output-energy validation token,
            ``None`` for normal output. Stable taxonomy:

            * ``"zero_energy"`` — measured RMS below the perceptual
              silence floor for non-empty input text. Indicates the
              synthesis pipeline produced effectively-silent output
              (corrupt voice file, ONNX returning zeros, model
              degeneration). The orchestrator reads this to trigger
              the Piper fallback so the user hears *something*
              instead of inexplicable silence.

            Stable across minor versions — dashboards key on it.
            Default ``None`` is backwards-compat for pre-T2 callers.
    """

    audio: np.ndarray  # int16 PCM
    sample_rate: int = _DEFAULT_SAMPLE_RATE
    duration_ms: float = 0.0
    synthesis_health: str | None = None


@dataclass(frozen=True, slots=True)
class PiperConfig:
    """Configuration for PiperTTS.

    Attributes:
        voice: Voice model name (e.g., ``en_US-lessac-medium``).
        noise_scale: Pitch variability (VITS parameter). Default 0.667.
        length_scale: Speed control — lower is faster. Default 1.0.
        noise_w: Duration variability (VITS parameter). Default 0.8.
        sentence_silence: Seconds of silence inserted between sentences.
        speaker_id: Speaker index for multi-speaker models. None = default (0).
    """

    voice: str = "en_US-lessac-medium"
    noise_scale: float = 0.667
    length_scale: float = 1.0
    noise_w: float = 0.8
    sentence_silence: float = 0.3
    speaker_id: int | None = None


# ---------------------------------------------------------------------------
# TTSEngine ABC
# ---------------------------------------------------------------------------


class TTSEngine(ABC):
    """Abstract base for text-to-speech engines.

    Streaming chunk + overlap contract (master mission Phase 1 / T1.40):

    * **Chunk boundary** — :meth:`synthesize_streaming` yields one
      :class:`AudioChunk` per *complete sentence* parsed out of the
      incoming text stream by
      :func:`sovyx.voice._tts_sentence_split.split_sentences` (greedy
      ``(?<=[.!?])\\s+`` split + abbreviation merge-back so ``Dr.``,
      ``Mr.``, ``U.S.A.``, ``e.g.``, ``Ph.D.``, etc. don't fragment a
      sentence mid-stream). Partial trailing text is buffered until
      either more text arrives with a terminator or the upstream
      stream closes (the buffered remainder is then flushed as a final
      chunk in either case).
    * **Why per-sentence and not finer** — coarser granularity
      (paragraphs) starves the Jarvis Illusion (perceived TTS-start
      latency); finer granularity (per-word or per-clause) breaks
      VITS prosody continuity, which depends on full-sentence phoneme
      sequences for natural intonation. Per-sentence is the empirical
      sweet spot validated against SPE-010 §6.2.
    * **No overlap** — chunks are independent; the orchestrator's
      output queue (:class:`AudioOutputQueue`) plays them
      back-to-back without crossfade, click suppression, or
      crossing-zero alignment. The intra-chunk
      ``sentence_silence`` constant inside :meth:`synthesize` (Piper)
      / :meth:`synthesize_with` (Kokoro) inserts a small silence
      pause between sentences when a single ``synthesize`` call
      receives multi-sentence input — in the streaming path that
      pause is implicit in the consumer's playback gap between
      yielded chunks, so no additional silence is injected here.
    * **Empty / phonemiser-rejected sentences** — engines MUST skip
      empty buffers and emit a DROP stage event with
      ``error_type="empty_text"`` (or ``"no_phonemes"`` when the
      phonemiser produced no usable phonemes for non-empty input).
      The stream MUST NOT yield an empty :class:`AudioChunk` —
      consumers rely on every yielded chunk having
      ``audio.size > 0``.
    * **Cancellation** — if the consumer stops iterating mid-stream,
      the generator's ``GeneratorExit`` propagates to the engine's
      ``async for text_chunk in text_stream`` loop. Engines MUST
      release any pending buffer state on exit (the buffered text
      is implicitly discarded; per T1.15 / `8faca52` the orchestrator
      also clears its own ``_text_buffer`` on ``CancelledError`` to
      prevent stale text from leaking into the next stream).
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Load model and prepare for synthesis."""

    @abstractmethod
    async def synthesize(self, text: str) -> AudioChunk:
        """Synthesize text to an audio chunk."""
        ...

    @abstractmethod
    async def synthesize_streaming(
        self,
        text_stream: AsyncIterator[str],
    ) -> AsyncIterator[AudioChunk]:
        """Streaming synthesis — yield audio per sentence as text arrives.

        See the :class:`TTSEngine` class docstring for the full chunk +
        overlap contract.
        """
        ...  # pragma: no cover
        # Yield required for AsyncIterator typing
        if False:  # noqa: SIM108  # pragma: no cover
            yield

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_config(config: PiperConfig) -> None:
    """Validate PiperConfig parameters.

    Raises:
        ValueError: If any parameter is out of acceptable range.
    """
    if config.noise_scale < 0.0 or config.noise_scale > 2.0:
        msg = f"noise_scale must be in [0.0, 2.0], got {config.noise_scale}"
        raise ValueError(msg)
    if config.length_scale <= 0.0 or config.length_scale > 5.0:
        msg = f"length_scale must be in (0.0, 5.0], got {config.length_scale}"
        raise ValueError(msg)
    if config.noise_w < 0.0 or config.noise_w > 2.0:
        msg = f"noise_w must be in [0.0, 2.0], got {config.noise_w}"
        raise ValueError(msg)
    if config.sentence_silence < 0.0:
        msg = f"sentence_silence must be >= 0, got {config.sentence_silence}"
        raise ValueError(msg)
    if config.speaker_id is not None and config.speaker_id < 0:
        msg = f"speaker_id must be >= 0, got {config.speaker_id}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# PiperTTS implementation
# ---------------------------------------------------------------------------


class PiperTTS(TTSEngine):
    """Piper TTS — fast local synthesis via VITS ONNX model.

    Pipeline: text → espeak-ng phonemize → phoneme IDs → ONNX → int16 audio

    Model files per voice:
    - ``{voice}.onnx`` (~15-60MB depending on quality)
    - ``{voice}.onnx.json`` (~100KB — config with phoneme map)

    Performance on Pi 5:
    - Low quality: ~20x real-time
    - Medium quality: ~10x real-time
    - High quality: ~5x real-time

    Output: 22050 Hz, 16-bit mono PCM
    """

    def __init__(
        self,
        model_dir: Path,
        config: PiperConfig | None = None,
    ) -> None:
        _config = config or PiperConfig()
        _validate_config(_config)

        self._config = _config
        self._model_dir = Path(model_dir)
        self._session: Any | None = None
        self._voice_config: dict[str, Any] | None = None
        self._initialized = False
        self._chunk_counter = 0

    # -- Properties --------------------------------------------------------

    @property
    def config(self) -> PiperConfig:
        """Current configuration (read-only)."""
        return self._config

    @property
    def is_initialized(self) -> bool:
        """Whether the model is loaded and ready."""
        return self._initialized

    @property
    def sample_rate(self) -> int:
        """Audio sample rate from voice config, or default 22050."""
        if self._voice_config is not None:
            audio_cfg = self._voice_config.get("audio", {})
            return int(audio_cfg.get("sample_rate", _DEFAULT_SAMPLE_RATE))
        return _DEFAULT_SAMPLE_RATE

    @property
    def num_speakers(self) -> int:
        """Number of speakers in the loaded model."""
        if self._voice_config is not None:
            return int(self._voice_config.get("num_speakers", 1))
        return 1

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Load ONNX model and voice configuration.

        Raises:
            FileNotFoundError: If model or config files are missing.
            RuntimeError: If ONNX session creation fails.
        """
        import onnxruntime as ort

        model_path = self._model_dir / f"{self._config.voice}.onnx"
        config_path = self._model_dir / f"{self._config.voice}.onnx.json"

        if not model_path.exists():
            msg = f"Piper model not found: {model_path}"
            raise FileNotFoundError(msg)
        if not config_path.exists():
            msg = f"Piper config not found: {config_path}"
            raise FileNotFoundError(msg)

        # Load voice configuration (phoneme map, audio settings, etc.)
        with open(config_path, encoding="utf-8") as f:
            self._voice_config = json.load(f)

        # Create optimized ONNX session
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        try:
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Failed to create Piper ONNX session: {exc}"
            raise RuntimeError(msg) from exc

        self._initialized = True
        logger.info(
            "PiperTTS initialized",
            voice=self._config.voice,
            sample_rate=self.sample_rate,
            num_speakers=self.num_speakers,
        )

    async def close(self) -> None:
        """Release ONNX session resources."""
        self._session = None
        self._voice_config = None
        self._initialized = False
        logger.info("PiperTTS closed")

    # -- Phonemization -----------------------------------------------------

    def _phonemize(self, text: str) -> list[list[str]]:
        """Convert text to phonemes grouped by sentence.

        Uses ``piper_phonemize`` which wraps espeak-ng.

        Returns:
            List of phoneme lists, one per sentence.

        Raises:
            RuntimeError: If not initialized (voice config needed for espeak voice).
        """
        if self._voice_config is None:
            msg = "PiperTTS not initialized — call initialize() first"
            raise RuntimeError(msg)

        from piper_phonemize import phonemize_espeak

        espeak_voice: str = self._voice_config["espeak"]["voice"]
        result: list[list[str]] = phonemize_espeak(text, espeak_voice)
        return result

    def _phonemes_to_ids(self, phonemes: list[str]) -> list[int]:
        """Map phonemes to numerical IDs using voice config's phoneme_id_map.

        Format: BOS + (phoneme_ids + PAD)* + EOS

        The PAD between each phoneme is critical — VITS model uses it for alignment.

        Returns:
            List of integer phoneme IDs.

        Raises:
            RuntimeError: If not initialized.
        """
        if self._voice_config is None:
            msg = "PiperTTS not initialized — call initialize() first"
            raise RuntimeError(msg)

        id_map: dict[str, list[int]] = self._voice_config["phoneme_id_map"]

        ids: list[int] = list(id_map.get(_BOS, [1]))

        for phoneme in phonemes:
            if phoneme in id_map:
                ids.extend(id_map[phoneme])
                ids.extend(id_map.get(_PAD, [0]))
            # Skip unknown phonemes silently (robust to unexpected input)

        ids.extend(id_map.get(_EOS, [2]))

        if len(ids) > _MAX_PHONEME_IDS:
            logger.warning(
                "Phoneme ID sequence truncated",
                original_len=len(ids),
                max_len=_MAX_PHONEME_IDS,
            )
            ids = ids[:_MAX_PHONEME_IDS]

        return ids

    # -- ONNX inference ----------------------------------------------------

    def _synthesize_ids(
        self,
        phoneme_ids: list[int],
        speaker_id: int | None = None,
    ) -> np.ndarray:
        """Run ONNX inference on phoneme IDs.

        Input tensors:
        - input: [1, N] int64 — phoneme IDs
        - input_lengths: [1] int64 — number of IDs
        - scales: [3] float32 — [noise_scale, length_scale, noise_w]
        - sid: [1] int64 — speaker ID (multi-speaker models only)

        Output: int16 PCM audio array.

        Raises:
            RuntimeError: If session is not loaded.
        """
        import numpy as np

        if self._session is None:
            msg = "PiperTTS ONNX session not loaded"
            raise RuntimeError(msg)

        phoneme_ids_array = np.expand_dims(
            np.array(phoneme_ids, dtype=np.int64),
            0,
        )
        phoneme_ids_lengths = np.array(
            [phoneme_ids_array.shape[1]],
            dtype=np.int64,
        )
        scales = np.array(
            [self._config.noise_scale, self._config.length_scale, self._config.noise_w],
            dtype=np.float32,
        )

        args: dict[str, np.ndarray] = {
            "input": phoneme_ids_array,
            "input_lengths": phoneme_ids_lengths,
            "scales": scales,
        }

        # Multi-speaker support
        num_speakers = self._voice_config.get("num_speakers", 1) if self._voice_config else 1
        if num_speakers > 1:
            sid = speaker_id if speaker_id is not None else 0
            args["sid"] = np.array([sid], dtype=np.int64)

        # Run inference
        audio_float = self._session.run(None, args)[0].squeeze()

        # Convert float32 → int16 PCM
        audio_int16: np.ndarray = np.clip(
            audio_float * 32768.0,
            -32768,
            32767,
        ).astype(np.int16)

        return audio_int16

    # -- Public API --------------------------------------------------------

    async def synthesize(self, text: str) -> AudioChunk:
        """Synthesize text to audio.

        Full pipeline: text → phonemes → IDs → ONNX → audio.

        For empty text, returns an empty AudioChunk.

        Args:
            text: The text to synthesize.

        Returns:
            AudioChunk with int16 PCM audio at the voice's sample rate.
        """
        import time

        import numpy as np

        if not self._initialized:
            await self.initialize()

        sr = self.sample_rate
        text = text.strip()

        # Ring 6 RED + USE: mirrors the Kokoro M2 wire-up
        # (commit 840ec69) so the dashboard sees consistent
        # voice.stage.* events whether Kokoro or its Piper fallback
        # produced the audio. Implicit error paths (ONNX exception,
        # phonemiser failure) flow through measure_stage_duration's
        # BaseException handler.
        with measure_stage_duration(VoiceStage.TTS):
            if not text:
                # Empty input → DROP not SUCCESS (no audio produced).
                # error_type=empty_text matches the Kokoro DROP label
                # so dashboards don't need per-engine special-casing.
                record_stage_event(
                    VoiceStage.TTS,
                    StageEventKind.DROP,
                    error_type="empty_text",
                )
                return AudioChunk(
                    audio=np.array([], dtype=np.int16),
                    sample_rate=sr,
                    duration_ms=0.0,
                )

            silence_samples = int(self._config.sentence_silence * sr)
            silence = np.zeros(silence_samples, dtype=np.int16)

            all_audio: list[np.ndarray] = []
            sentence_phonemes = self._phonemize(text)

            gen_start = time.monotonic()
            for phonemes in sentence_phonemes:
                if not phonemes:
                    continue
                ids = self._phonemes_to_ids(phonemes)
                # Piper ONNX inference is CPU-bound — offload to a
                # worker thread so concurrent dashboard / HTTP /
                # pipeline tasks stay responsive while a sentence is
                # being synthesized.
                audio = await asyncio.to_thread(
                    self._synthesize_ids,
                    ids,
                    speaker_id=self._config.speaker_id,
                )
                all_audio.append(audio)
                all_audio.append(silence)
            generation_ms = (time.monotonic() - gen_start) * 1000

            if not all_audio:
                # Phonemiser produced no usable phonemes — distinct
                # rejection class from empty_text (input WAS non-
                # empty, but the language layer couldn't process it,
                # e.g. emoji-only text).
                record_stage_event(
                    VoiceStage.TTS,
                    StageEventKind.DROP,
                    error_type="no_phonemes",
                )
                return AudioChunk(
                    audio=np.array([], dtype=np.int16),
                    sample_rate=sr,
                    duration_ms=0.0,
                )

            combined = np.concatenate(all_audio)
            duration_ms = len(combined) / sr * 1000

            # T2 Ring 5 output-energy validation (master mission Phase 1
            # / T1.36). Symmetric with KokoroTTS's gate at the same point
            # in synthesize_with — both engines apply the shared
            # ``voice/_tts_zero_energy`` primitives so the dashboard
            # attributes structural-output failures uniformly. Piper is
            # itself the fallback engine for Kokoro, so a sub-floor
            # reading here means the user would hear silence with no
            # audible recovery path inside the local TTS chain — the
            # chunk's ``synthesis_health`` field signals the orchestrator
            # to escalate to cloud TTS or text-only mode.
            rms_dbfs = compute_rms_dbfs(combined)
            synthesis_health: str | None = None
            if rms_dbfs < TTS_RMS_FLOOR_DBFS:
                synthesis_health = "zero_energy"
                logger.warning(
                    "voice.tts.piper_zero_energy_synthesis",
                    **{
                        "voice.text_chars": len(text),
                        "voice.audio_ms": round(duration_ms, 1),
                        "voice.generation_ms": round(generation_ms, 1),
                        "voice.measured_rms_dbfs": (
                            round(rms_dbfs, 2) if rms_dbfs != float("-inf") else "-inf"
                        ),
                        "voice.rms_floor_dbfs": TTS_RMS_FLOOR_DBFS,
                        "voice.model": "piper",
                        "voice.voice": self._config.voice,
                        "voice.sample_rate": sr,
                        "voice.action_required": ("fallback_to_cloud_tts_or_text_only"),
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
                    "voice.model": "piper",
                    "voice.voice": self._config.voice,
                    "voice.sample_rate": sr,
                    "voice.speaker_id": self._config.speaker_id,
                    "voice.synthesis_health": synthesis_health or "ok",
                },
            )

            if synthesis_health == "zero_energy":
                record_stage_event(
                    VoiceStage.TTS,
                    StageEventKind.DROP,
                    error_type="zero_energy",
                )
            else:
                record_stage_event(VoiceStage.TTS, StageEventKind.SUCCESS)

            return AudioChunk(
                audio=combined,
                sample_rate=sr,
                duration_ms=duration_ms,
                synthesis_health=synthesis_health,
            )

    async def synthesize_streaming(
        self,
        text_stream: AsyncIterator[str],
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesis — yield audio per sentence as text arrives.

        Key for Jarvis Illusion: start speaking before full LLM response.
        Splits on sentence boundaries (`.`, `!`, `?` followed by whitespace).

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
        """List available voice models in the model directory.

        Scans for ``*.onnx.json`` files and returns voice names.

        Returns:
            Sorted list of voice names found in model_dir.
        """
        if not self._model_dir.exists():
            return []

        voices = []
        for config_file in sorted(self._model_dir.glob("*.onnx.json")):
            # Remove .onnx.json suffix to get voice name
            voice_name = config_file.name.removesuffix(".onnx.json")
            # Only include if matching .onnx model exists
            if (self._model_dir / f"{voice_name}.onnx").exists():
                voices.append(voice_name)

        return voices
