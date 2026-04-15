"""JarvisIllusion — latency masking via beeps, fillers, and streaming overlap.

Three techniques combined to reduce PERCEIVED latency from 3-7s to <500ms:
    1. **Confirmation beep** — immediate audio feedback on wake word detection.
    2. **Filler phrases** — natural thinking sounds while LLM processes.
    3. **Speculative TTS** — start TTS before LLM finishes (streaming overlap).

Fillers are categorised (thinking, checking, acknowledging, confirming,
transitional) and selected based on user input type.  A repetition-avoidance
system tracks recent fillers to maintain natural variety.

Ref: SPE-010 §7, IMPL-SUP-005 §SPEC-1 (filler injection, timing)
"""

from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice.pipeline import AudioOutputQueue
    from sovyx.voice.tts_piper import AudioChunk, TTSEngine

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants (SUP-005 §1.3)
# ---------------------------------------------------------------------------

FILLER_DELAY_MS = 5
"""Milliseconds from endpointing to filler start (pre-cached = near-instant)."""

FILLER_MIN_DURATION_MS = 500
"""Minimum filler play time before crossfade to response."""

CROSSFADE_MS = 100
"""Crossfade duration between filler end and response start."""

POST_FILLER_PAUSE_MS = 200
"""Pause after filler ends if response hasn't arrived yet."""

MAX_SAME_FILLER_IN_ROW = 2
"""Don't use the exact same filler more than this many times consecutively."""

HISTORY_SIZE = 5
"""Track last N fillers for variety."""

_BEEP_FREQ_HZ = 440
_BEEP_DURATION_S = 0.05
_BEEP_SAMPLE_RATE = 22050
_BEEP_FADE_S = 0.005
_BEEP_AMPLITUDE = 16000

# Minimum words in a sentence segment for streaming TTS
_TEXT_MIN_WORDS = 3


# ---------------------------------------------------------------------------
# FillerCategory
# ---------------------------------------------------------------------------


class FillerCategory(StrEnum):
    """Category of filler phrase (SUP-005 §1.2)."""

    THINKING = "thinking"
    CHECKING = "checking"
    ACKNOWLEDGING = "ack"
    CONFIRMING = "confirm"
    TRANSITIONAL = "transition"


# ---------------------------------------------------------------------------
# Filler bank
# ---------------------------------------------------------------------------

FILLER_BANK: dict[FillerCategory, tuple[str, ...]] = {
    FillerCategory.THINKING: (
        "Let me think about that.",
        "Hmm, good question.",
        "Let me think...",
    ),
    FillerCategory.CHECKING: (
        "Let me check.",
        "One moment.",
        "Checking now.",
    ),
    FillerCategory.ACKNOWLEDGING: (
        "Got it.",
        "Sure.",
        "Alright.",
    ),
    FillerCategory.CONFIRMING: (
        "Right, so...",
        "OK, so...",
        "Yeah, so...",
    ),
    FillerCategory.TRANSITIONAL: (
        "Well...",
        "So...",
        "OK...",
    ),
}

# Selection rules mapping input type → filler category (SUP-005 §1.3)
_SELECTION_RULES: dict[str, FillerCategory] = {
    "question": FillerCategory.THINKING,
    "command": FillerCategory.ACKNOWLEDGING,
    "confirmation": FillerCategory.CONFIRMING,
    "complex": FillerCategory.CHECKING,
    "default": FillerCategory.TRANSITIONAL,
}

# Minimum word count threshold to classify input as "complex"
_COMPLEX_WORD_THRESHOLD = 10


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JarvisConfig:
    """Configuration for JarvisIllusion.

    Attributes:
        fillers_enabled: Whether to play filler phrases during LLM thinking.
        filler_delay_ms: Milliseconds to wait before playing a filler.
        filler_min_duration_ms: Minimum filler play time before crossfade.
        crossfade_ms: Crossfade duration between filler end and response.
        post_filler_pause_ms: Pause after filler if response hasn't arrived.
        confirmation_tone: Type of tone on wake word (``"beep"`` or ``"none"``).
        filler_bank: Custom filler phrases per category (uses default if omitted).
        max_same_in_row: Max consecutive uses of the same filler.
        history_size: Number of recent fillers to track for variety.
    """

    fillers_enabled: bool = True
    filler_delay_ms: int = FILLER_DELAY_MS
    filler_min_duration_ms: int = FILLER_MIN_DURATION_MS
    crossfade_ms: int = CROSSFADE_MS
    post_filler_pause_ms: int = POST_FILLER_PAUSE_MS
    confirmation_tone: str = "beep"
    filler_bank: dict[FillerCategory, tuple[str, ...]] = field(
        default_factory=lambda: dict(FILLER_BANK)
    )
    max_same_in_row: int = MAX_SAME_FILLER_IN_ROW
    history_size: int = HISTORY_SIZE


def validate_jarvis_config(config: JarvisConfig) -> None:
    """Validate JarvisConfig values.

    Args:
        config: Configuration to validate.

    Raises:
        ValueError: If any parameter is invalid.
    """
    if config.filler_delay_ms < 0:
        msg = f"filler_delay_ms must be >= 0, got {config.filler_delay_ms}"
        raise ValueError(msg)
    if config.filler_min_duration_ms < 0:
        msg = f"filler_min_duration_ms must be >= 0, got {config.filler_min_duration_ms}"
        raise ValueError(msg)
    if config.crossfade_ms < 0:
        msg = f"crossfade_ms must be >= 0, got {config.crossfade_ms}"
        raise ValueError(msg)
    if config.post_filler_pause_ms < 0:
        msg = f"post_filler_pause_ms must be >= 0, got {config.post_filler_pause_ms}"
        raise ValueError(msg)
    if config.confirmation_tone not in ("beep", "none"):
        msg = f"confirmation_tone must be 'beep' or 'none', got {config.confirmation_tone!r}"
        raise ValueError(msg)
    if config.max_same_in_row < 1:
        msg = f"max_same_in_row must be >= 1, got {config.max_same_in_row}"
        raise ValueError(msg)
    if config.history_size < 1:
        msg = f"history_size must be >= 1, got {config.history_size}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# JarvisIllusion
# ---------------------------------------------------------------------------


class JarvisIllusion:
    """Manages filler phrases and confirmation beeps to reduce perceived latency.

    Three techniques (SPE-010 §7):
        1. **Confirmation beep** — immediate feedback on wake word detection.
        2. **Filler phrases** — categorised, with repetition avoidance.
        3. **Speculative TTS** — start synthesis before LLM finishes.

    Args:
        config: Jarvis-specific configuration.
        tts: TTS engine for synthesizing fillers.
    """

    def __init__(self, config: JarvisConfig, tts: TTSEngine) -> None:
        validate_jarvis_config(config)
        self._config = config
        self._tts = tts
        self._beep_cache: AudioChunk | None = None
        self._filler_cache: dict[str, AudioChunk] = {}
        self._history: deque[str] = deque(maxlen=config.history_size)

    @property
    def config(self) -> JarvisConfig:
        """Current configuration."""
        return self._config

    @property
    def beep_cached(self) -> bool:
        """Whether the confirmation beep is cached."""
        return self._beep_cache is not None

    @property
    def cached_filler_count(self) -> int:
        """Number of cached filler phrases."""
        return len(self._filler_cache)

    @property
    def history(self) -> list[str]:
        """Recent filler history (newest last)."""
        return list(self._history)

    # -- Pre-cache -----------------------------------------------------------

    async def pre_cache(self) -> None:
        """Pre-synthesize fillers and beep at startup for instant playback.

        Total: ~20 fillers × ~1.5s avg ≈ 30s of audio, ~1.3MB PCM.
        Time: ~3s on Pi 5 (Piper), ~1s on N100.
        """
        if self._config.confirmation_tone == "beep":
            self._beep_cache = synthesize_beep()

        for _category, phrases in self._config.filler_bank.items():
            for phrase in phrases:
                try:
                    self._filler_cache[phrase] = await self._tts.synthesize(phrase)
                except (VoiceError, RuntimeError, OSError):
                    # VoiceError covers the documented TTS backend types
                    # (Kokoro, Piper, cloud). RuntimeError captures ONNX
                    # inference failures that don't inherit VoiceError.
                    # OSError catches model-file I/O. Missing from the
                    # cache just means the filler gets synthesized on
                    # demand — never-fatal degradation — but we want the
                    # traceback so TTS misconfig isn't invisible.
                    logger.warning(
                        "Failed to cache filler",
                        phrase=phrase,
                        exc_info=True,
                    )

    # -- Beep ----------------------------------------------------------------

    async def play_beep(self, output: AudioOutputQueue) -> None:
        """Play the confirmation beep immediately.

        Args:
            output: Audio output queue (must have ``play_immediate`` method).
        """
        if self._beep_cache is not None:
            await output.play_immediate(self._beep_cache)

    def get_beep(self) -> AudioChunk | None:
        """Return cached beep audio chunk (or None if not cached).

        Returns:
            Cached beep or ``None``.
        """
        return self._beep_cache

    # -- Filler selection (SUP-005 §1.3) ------------------------------------

    def select_category(self, user_input: str, intent: str | None = None) -> FillerCategory:
        """Select appropriate filler category based on user input.

        Args:
            user_input: Raw text of user's speech.
            intent: Optional classified intent (question/command/confirmation/complex).

        Returns:
            The filler category to use.
        """
        if intent:
            return _SELECTION_RULES.get(intent, FillerCategory.TRANSITIONAL)
        # Heuristics without intent classification
        if user_input.strip().endswith("?"):
            return FillerCategory.THINKING
        if len(user_input.split()) > _COMPLEX_WORD_THRESHOLD:
            return FillerCategory.CHECKING
        return FillerCategory.TRANSITIONAL

    def select_filler(
        self,
        user_input: str = "",
        intent: str | None = None,
        category: FillerCategory | None = None,
    ) -> str:
        """Select a filler phrase with repetition avoidance.

        Args:
            user_input: Raw user text (used if category is None).
            intent: Optional classified intent.
            category: Explicit category (overrides user_input heuristics).

        Returns:
            A filler phrase string.
        """
        cat = category or self.select_category(user_input, intent)
        phrases = list(self._config.filler_bank.get(cat, ()))
        if not phrases:
            # Fallback to transitional
            phrases = list(self._config.filler_bank.get(FillerCategory.TRANSITIONAL, ("...",)))

        # Repetition avoidance
        recent = list(self._history)
        candidates = self._filter_repetitions(phrases, recent)
        if not candidates:
            candidates = phrases  # All filtered — allow any

        choice = random.choice(candidates)  # noqa: S311
        self._history.append(choice)
        return choice

    def _filter_repetitions(self, phrases: list[str], recent: list[str]) -> list[str]:
        """Remove phrases that would exceed consecutive repetition limit.

        Args:
            phrases: Available phrases.
            recent: Recent history (newest last).

        Returns:
            Filtered list of phrases.
        """
        if not recent:
            return phrases

        max_in_row = self._config.max_same_in_row
        # Count how many times the most recent phrase appears consecutively
        last = recent[-1]
        consecutive = 0
        for item in reversed(recent):
            if item == last:
                consecutive += 1
            else:
                break

        if consecutive >= max_in_row:
            return [p for p in phrases if p != last]
        return phrases

    # -- Filler playback -----------------------------------------------------

    def get_cached_filler(self, phrase: str) -> AudioChunk | None:
        """Return a cached filler chunk or None.

        Args:
            phrase: The filler phrase text.

        Returns:
            Cached audio chunk or ``None`` if not cached.
        """
        return self._filler_cache.get(phrase)

    async def play_filler_after_delay(
        self,
        output: AudioOutputQueue,
        cancel_event: asyncio.Event,
        user_input: str = "",
        intent: str | None = None,
    ) -> bool:
        """Play a filler phrase if LLM hasn't responded within delay.

        Args:
            output: Audio output queue (must have ``play_immediate``).
            cancel_event: Set this event to cancel filler playback.
            user_input: User text for smart category selection.
            intent: Classified intent for category selection.

        Returns:
            ``True`` if a filler was played, ``False`` if cancelled.
        """
        delay_s = self._config.filler_delay_ms / 1000.0
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay_s)
            return False  # LLM responded in time — no filler needed
        except TimeoutError:
            # LLM still thinking — play filler
            phrase = self.select_filler(user_input=user_input, intent=intent)
            chunk = self._filler_cache.get(phrase)
            if chunk is None:
                try:
                    chunk = await self._tts.synthesize(phrase)
                except (VoiceError, RuntimeError, OSError):
                    # Same failure modes as the cache-warm path above —
                    # log with traceback, skip the filler, let the real
                    # response carry the conversation forward.
                    logger.warning(
                        "Filler synthesis failed",
                        phrase=phrase,
                        exc_info=True,
                    )
                    return False
            await output.play_immediate(chunk)
            return True

    # -- Streaming text splitting (speculative TTS) --------------------------

    def reset_history(self) -> None:
        """Clear filler history (e.g., on new conversation)."""
        self._history.clear()


# ---------------------------------------------------------------------------
# Text splitter for streaming TTS (Jarvis Illusion §3)
# ---------------------------------------------------------------------------


def split_at_boundaries(text: str) -> list[str]:
    """Split text at sentence boundaries for incremental TTS.

    Boundaries: ``. ! ? ; : —`` and newlines.
    Minimum chunk: 3 words (avoids synthesizing single words).

    Args:
        text: Text to split.

    Returns:
        List of text segments.
    """
    parts = re.split(r"(?<=[.!?;:\u2014\n])\s+", text)
    result: list[str] = []
    for part in parts:
        if len(part.split()) >= _TEXT_MIN_WORDS or part.rstrip().endswith((".", "!", "?")):
            result.append(part)
        elif result:
            result[-1] += " " + part
        else:
            result.append(part)
    return result


# ---------------------------------------------------------------------------
# Beep synthesis (standalone function for reuse)
# ---------------------------------------------------------------------------


def synthesize_beep(
    freq_hz: int = _BEEP_FREQ_HZ,
    duration_s: float = _BEEP_DURATION_S,
    sample_rate: int = _BEEP_SAMPLE_RATE,
) -> AudioChunk:
    """Generate a short sine-wave beep.

    Args:
        freq_hz: Tone frequency in Hz (default 440).
        duration_s: Duration in seconds (default 0.05).
        sample_rate: Output sample rate (default 22050).

    Returns:
        An AudioChunk with the beep.
    """
    import numpy as np

    from sovyx.voice.tts_piper import AudioChunk as _AudioChunk

    t = np.linspace(0, duration_s, int(sample_rate * duration_s), dtype=np.float32)
    sine = np.sin(2 * np.pi * freq_hz * t)

    # Fade in/out to avoid clicks
    fade_len = int(sample_rate * _BEEP_FADE_S)
    if fade_len > 0 and len(sine) >= 2 * fade_len:
        sine[:fade_len] *= np.linspace(0, 1, fade_len)
        sine[-fade_len:] *= np.linspace(1, 0, fade_len)

    audio = (sine * _BEEP_AMPLITUDE).astype(np.int16)
    return _AudioChunk(
        audio=audio,
        sample_rate=sample_rate,
        duration_ms=duration_s * 1000,
    )
