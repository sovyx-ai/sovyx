"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

from dataclasses import dataclass


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
