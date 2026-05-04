"""MoonshineSTT — Speech-to-text using moonshine-voice library.

Wraps moonshine-voice (C++ core + ONNX Runtime) with Sovyx event system.
Supports full utterance and streaming transcription.

Ring 4 (Decode Validation) defense-in-depth: every transcript is run
through two output-side guards before reaching the orchestrator:

* **Hallucination stop-list** — Whisper-class encoder/decoder STT
  models emit a small set of canonical hallucinations on silence /
  music / unintelligible input ("thank you", "thanks for watching",
  "you", etc.). The stop-list is curated per language and rejects
  the transcript with a structured event so the orchestrator doesn't
  feed phantom turns to the LLM. Reference: openai/whisper
  discussion #679; LiveKit production stoplist.
* **Compression-ratio reject** — repetitive output ("yes yes yes
  yes...") that decompresses to a high size ratio is the signature
  of a degenerate decode loop (Whisper canonical
  ``compression_ratio_threshold = 2.4``). Reject before propagation.

Reference: SPE-010 §5 (STT), IMPL-004 §2.1 (moonshine-voice API),
MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.4 / §3.3 / S1.
"""

from __future__ import annotations

import asyncio
import gzip
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    measure_stage_duration,
    record_stage_event,
)

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


# ---------------------------------------------------------------------------
# S1 Ring 4 decode-validation tuning
# ---------------------------------------------------------------------------

_COMPRESSION_RATIO_THRESHOLD = 2.4
"""Whisper canonical reject threshold for repetitive transcripts
(``transcribe.py`` `compression_ratio_threshold`). The ratio is
``len(text_utf8) / len(gzip(text_utf8))`` — repetitive content
("yes yes yes yes...") compresses very well, producing a high
ratio. Above 2.4 indicates a degenerate decode loop and the
transcript is rejected at the Ring 4 boundary."""

_HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK = 32
"""Minimum text length below which the compression-ratio check is
skipped. Short transcripts ("ok", "yes") have unstable ratios
(small denominator inflates the ratio) and shouldn't be rejected
on this signal alone — the stop-list catches those instead."""

# Hallucination stop-lists per language. Each entry is the lowercased
# canonical form of a known degenerate Whisper-class output. The
# matcher normalises the candidate transcript (lowercase, strip
# punctuation/whitespace) before exact-match comparison so trivial
# variations ("Thank you.", "thank you") collapse to the same bucket.
#
# Curated from:
# - openai/whisper#679 discussion (English long-tail hallucinations)
# - LiveKit production stoplist (PT/ES additions)
# - Common Voice + LibriSpeech eval logs (Sovyx pilot reports)
#
# Empty strings and pure-whitespace transcripts are also treated as
# hallucinations (Moonshine's "no detection" path occasionally
# surfaces a single space or empty string).

_HALLUCINATION_STOPLIST: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "",
            ".",
            "you",
            "thank you",
            "thank you.",
            "thanks",
            "thanks.",
            "thanks for watching",
            "thanks for watching.",
            "thanks for watching!",
            "subscribe",
            "please subscribe",
            "like and subscribe",
            "bye",
            "bye.",
            "bye!",
            "okay",
            "ok",
            "uh",
            "um",
            "hmm",
            "mm",
        },
    ),
    "pt": frozenset(
        {
            "",
            ".",
            "obrigado",
            "obrigado.",
            "obrigada",
            "obrigada.",
            "valeu",
            "valeu.",
            "tchau",
            "tchau.",
            "ok",
            "okay",
            "hum",
            "hmm",
        },
    ),
    "es": frozenset(
        {
            "",
            ".",
            "gracias",
            "gracias.",
            "muchas gracias",
            "muchas gracias.",
            "adiós",
            "adios",
            "ok",
            "okay",
            "hmm",
        },
    ),
}


def _compute_compression_ratio(text: str) -> float:
    """Whisper-canonical ``len(utf8) / len(gzip(utf8))`` for ``text``.

    Returns ``0.0`` for empty input — empty strings have no
    interpretable compression ratio and the caller should treat the
    short-text path (stop-list) as authoritative for them.

    Pure function — fully unit-testable in isolation. Used by the
    Ring 4 reject path and by tests asserting stop-list interaction
    with the ratio check.
    """
    if not text:
        return 0.0
    encoded = text.encode("utf-8")
    if len(encoded) == 0:
        return 0.0
    compressed = gzip.compress(encoded)
    if len(compressed) == 0:
        return 0.0
    return len(encoded) / len(compressed)


def _normalise_for_stoplist(text: str) -> str:
    """Lowercase + strip whitespace/trailing punctuation for stop-list match.

    The stop-list keys are canonicalised; the candidate transcript
    is normalised the same way so trivial variations
    ("Thank you!", "  thank you  ") collapse to the same bucket
    without exploding the catalog with every capitalisation.
    """
    return text.strip().lower()


def _is_hallucination(text: str, language: str) -> bool:
    """Return ``True`` when the normalised transcript is in the stop-list.

    Defaults to the English stop-list for unknown language codes —
    the long-tail of cloud-LLM-class hallucinations is dominated by
    English even for non-English models, so falling back to ``en``
    is the safer default than skipping the check entirely.
    """
    catalog = _HALLUCINATION_STOPLIST.get(language, _HALLUCINATION_STOPLIST["en"])
    return _normalise_for_stoplist(text) in catalog


# Model sizes and their characteristics (Pi 5 benchmarks)
_MODEL_SPECS: dict[str, dict[str, float | int]] = {
    "tiny": {"params_m": 34, "latency_ms": 237, "wer_pct": 12.0},
    "small": {"params_m": 123, "latency_ms": 527, "wer_pct": 7.84},
    "medium": {"params_m": 245, "latency_ms": 802, "wer_pct": 6.65},
}


# Languages Moonshine v2 ships a model for. Direct probe at HEAD via
# moonshine_voice.get_model_for_language() raises ValueError for any
# code outside this set ("Language not found: <code>. Supported
# languages: ar, es, en, ja, ko, vi, uk, zh"). Kept as a frozenset so
# callers can membership-test cheaply without importing moonshine_voice
# at module load time. If a future Moonshine version adds a language,
# tests/unit/voice/test_stt.py::test_moonshine_supported_languages_constant
# breaks loudly to force this constant to be updated in lock-step.
MOONSHINE_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"ar", "en", "es", "ja", "ko", "uk", "vi", "zh"},
)


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
    """Transcribed text. Empty string when Ring 4 rejected the
    transcript (see :attr:`rejection_reason`)."""

    language: str | None = None
    """Detected or configured language code."""

    confidence: float = 0.0
    """Confidence score (0.0–1.0). Moonshine does not expose per-utterance
    confidence; we use fixed estimates per event type."""

    duration_ms: float = 0.0
    """Time spent in transcription (wall clock)."""

    segments: list[TranscriptionSegment] | None = None
    """Optional word-level or segment-level detail."""

    rejection_reason: str | None = None
    """Ring 4 reject reason when ``text`` was filtered out, ``None``
    when the transcript was accepted as-is. Stable taxonomy:

    * ``"hallucination_stoplist"`` — output matched the per-language
      degenerate-output catalog (e.g. "thank you" on silent input).
    * ``"compression_ratio_exceeded"`` — repetitive output above the
      Whisper canonical threshold (degenerate decode loop).

    Dashboards key on this string so the renaming a token is a
    breaking change for any downstream consumer (Grafana panels,
    dashboard "Voice Health" view)."""


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
        # S2 cumulative timeout counter — survives the full lifetime of
        # the engine (NOT cleared by transcribe() boundary). Surfaces
        # via :attr:`timeout_count` for circuit-breaker consumers
        # (Ring 4 → fall back to secondary STT after sustained
        # timeouts) and dashboard "Voice Health" attribution. Pre-S2
        # the timeout was logged WARNING and silently produced an
        # empty transcript that the orchestrator treated as "user said
        # nothing" — indistinguishable from real silence at the
        # caller boundary, so chronic STT degradation never surfaced.
        self._timeout_count: int = 0
        # TS3 chaos injector — opt-in failure injection at the
        # STT_TIMEOUT site. Disabled by default
        # (SOVYX_CHAOS__ENABLED=False); chaos test matrix sets the
        # env var + per-site rate to validate that the S2 timeout
        # taxonomy + M2 DROP event fire correctly under realistic
        # operating conditions.
        self._chaos = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value)

    @property
    def state(self) -> STTState:
        """Current engine lifecycle state."""
        return self._state

    @property
    def config(self) -> MoonshineConfig:
        """Active configuration."""
        return self._config

    @property
    def timeout_count(self) -> int:
        """S2 cumulative count of transcription timeouts since engine
        construction. Read-only. Non-zero on a long-running daemon is
        a structural signal that the model load is too slow for the
        configured ``transcribe_timeout`` (consider larger budget,
        smaller model, or fall back to a secondary STT). Included
        verbatim on every ``voice.stt.transcribe_timeout`` event so
        the dashboard can render a running burndown."""
        return self._timeout_count

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

        audio_ms = int(len(audio) * 1000 / sample_rate) if sample_rate > 0 else 0
        logger.info(
            "voice.stt.request",
            **{
                "voice.model": self._config.model_size,
                "voice.provider": "moonshine",
                "voice.language": self._config.language,
                "voice.audio_ms": audio_ms,
                "voice.sample_rate": sample_rate,
            },
        )

        try:
            # Ring 6 RED + USE: every STT invocation flows through M2's
            # measure_stage_duration so the dashboard sees the full
            # latency distribution split by outcome (success vs error).
            # record_stage_event below tags each return path with its
            # specific kind/error_type so error rate is queryable.
            with measure_stage_duration(VoiceStage.STT) as _stage_token:
                start = time.monotonic()
                try:
                    # TS3 chaos: opt-in TimeoutError injection.
                    # When the operator sets SOVYX_CHAOS__ENABLED=
                    # true AND SOVYX_CHAOS__INJECT_STT_TIMEOUT_PCT >
                    # 0, this randomly raises a TimeoutError before
                    # any real work happens — caught by the same
                    # except TimeoutError below as a real model
                    # timeout, exercising the full recovery path
                    # (counter bump, structured event,
                    # rejection_reason result).
                    if self._chaos.should_inject():
                        msg = "chaos-injected STT timeout"
                        raise TimeoutError(msg)
                    text = await self._transcribe_oneshot(audio, sample_rate)
                except TimeoutError:
                    # S2: timeout is its own rejection class — distinct
                    # from "user said nothing" (empty transcript through
                    # the normal path). Bump cumulative counter, emit
                    # structured event, return result with explicit
                    # ``rejection_reason`` so the orchestrator can branch
                    # (retry on secondary engine, surface to user)
                    # instead of treating it as silent input.
                    elapsed_ms = (time.monotonic() - start) * 1000
                    self._timeout_count += 1
                    logger.warning(
                        "voice.stt.transcribe_timeout",
                        **{
                            "voice.rejection_reason": "transcribe_timeout",
                            "voice.transcribe_timeout_s": self._config.transcribe_timeout,
                            "voice.elapsed_ms": round(elapsed_ms, 1),
                            "voice.audio_ms": audio_ms,
                            "voice.lifetime_timeout_count": self._timeout_count,
                            "voice.model": self._config.model_size,
                            "voice.provider": "moonshine",
                            "voice.language": self._config.language,
                            "voice.action_required": (
                                "consider_larger_timeout_or_smaller_model_or_secondary_stt"
                            ),
                        },
                    )
                    _stage_token.mark_error()
                    record_stage_event(
                        VoiceStage.STT,
                        StageEventKind.DROP,
                        error_type="transcribe_timeout",
                    )
                    return TranscriptionResult(
                        text="",
                        language=self._config.language,
                        confidence=0.0,
                        duration_ms=elapsed_ms,
                        rejection_reason="transcribe_timeout",
                    )
                elapsed_ms = (time.monotonic() - start) * 1000

                logger.debug(
                    "Transcription complete",
                    text_length=len(text),
                    duration_ms=round(elapsed_ms, 1),
                )

                stripped = text.strip()

                # ── S1 Ring 4 decode-validation guards ────────────
                # Order: hallucination stop-list FIRST (cheap, catches
                # the short-text cases the ratio check skips), then
                # compression ratio (only meaningful for longer
                # transcripts). Rejection short-circuits — we only run
                # one guard per transcript so the reason token is
                # unambiguous.
                rejection_reason = self._validate_transcript(stripped, audio_ms, elapsed_ms)
                if rejection_reason is not None:
                    # Fail-closed: drop the transcript so the orchestrator
                    # never feeds garbage to the LLM. The structured event
                    # already fired inside _validate_transcript so
                    # dashboards see the reject reason without needing
                    # to parse the log. M2: record DROP with the
                    # rejection reason as error_type — the bounded
                    # cardinality bucket caps explosion if a future
                    # validator adds many distinct reasons.
                    record_stage_event(
                        VoiceStage.STT,
                        StageEventKind.DROP,
                        error_type=rejection_reason,
                    )
                    return TranscriptionResult(
                        text="",
                        language=self._config.language,
                        confidence=0.0,
                        duration_ms=elapsed_ms,
                        rejection_reason=rejection_reason,
                    )

                logger.info(
                    "voice.stt.response",
                    **{
                        "voice.model": self._config.model_size,
                        "voice.provider": "moonshine",
                        "voice.language": self._config.language,
                        "voice.audio_ms": audio_ms,
                        "voice.latency_ms": round(elapsed_ms, 1),
                        "voice.confidence": 0.9,
                        "voice.text_chars": len(stripped),
                        "voice.transcript": stripped,
                    },
                )

                record_stage_event(VoiceStage.STT, StageEventKind.SUCCESS)
                return TranscriptionResult(
                    text=stripped,
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

    def _validate_transcript(
        self,
        text: str,
        audio_ms: int,
        elapsed_ms: float,
    ) -> str | None:
        """S1 Ring 4 guards — return reject reason or ``None`` to accept.

        Two checks, in order:

        1. Hallucination stop-list (per-language). Cheap, catches the
           short-text degenerate cases ("thank you" on silence) the
           compression-ratio check would skip.
        2. Compression ratio (Whisper canonical 2.4 threshold) for
           transcripts at or above ``_HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK``
           characters. Below that length the ratio is unstable
           (small-denominator inflation) and can't be trusted.

        Each rejection emits ``voice.stt.transcript_rejected`` at WARNING
        with the stable reason token + transcript fingerprint so
        dashboards can attribute and aggregate without re-running the
        guards client-side.
        """
        if _is_hallucination(text, self._config.language):
            logger.warning(
                "voice.stt.transcript_rejected",
                **{
                    "voice.rejection_reason": "hallucination_stoplist",
                    "voice.transcript": text,
                    "voice.text_chars": len(text),
                    "voice.language": self._config.language,
                    "voice.model": self._config.model_size,
                    "voice.provider": "moonshine",
                    "voice.audio_ms": audio_ms,
                    "voice.latency_ms": round(elapsed_ms, 1),
                },
            )
            return "hallucination_stoplist"

        if len(text) >= _HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK:
            ratio = _compute_compression_ratio(text)
            if ratio > _COMPRESSION_RATIO_THRESHOLD:
                logger.warning(
                    "voice.stt.transcript_rejected",
                    **{
                        "voice.rejection_reason": "compression_ratio_exceeded",
                        "voice.compression_ratio": round(ratio, 3),
                        "voice.compression_ratio_threshold": _COMPRESSION_RATIO_THRESHOLD,
                        "voice.text_chars": len(text),
                        "voice.transcript": text,
                        "voice.language": self._config.language,
                        "voice.model": self._config.model_size,
                        "voice.provider": "moonshine",
                        "voice.audio_ms": audio_ms,
                        "voice.latency_ms": round(elapsed_ms, 1),
                    },
                )
                return "compression_ratio_exceeded"

        return None

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
            # S2: re-raise TimeoutError so the caller (transcribe) can
            # distinguish a timeout from a real empty transcript and
            # bump its lifetime counter + emit the structured event.
            # Pre-S2 the timeout was logged WARNING here and silently
            # produced an empty string, masking sustained engine
            # degradation as "user said nothing".
            text = await asyncio.wait_for(
                result_future,
                timeout=self._config.transcribe_timeout,
            )
        finally:
            stream.close()

        return text
