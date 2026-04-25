"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports.

Every event carries an optional ``utterance_id`` field — a UUID4 string
minted by the orchestrator at each utterance boundary (wake-word fire,
external ``speak``, or no-wake recording start) and threaded across the
entire capture → VAD → STT → LLM → TTS chain. Dashboards / log search
correlate the full per-turn span set by joining on ``utterance_id``.
Default ``""`` preserves backward compatibility for callers that
construct events without the trace context (tests, legacy bridges).

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6 trace contract), §9.4.6 (acceptance gate: trace ID present in
100% of structured events).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WakeWordDetectedEvent:
    """Emitted when the wake word is detected."""

    mind_id: str = ""
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class SpeechStartedEvent:
    """Emitted when speech recording begins (after wake word)."""

    mind_id: str = ""
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class SpeechEndedEvent:
    """Emitted when speech recording ends (silence detected)."""

    mind_id: str = ""
    duration_ms: float = 0.0
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptionCompletedEvent:
    """Emitted when STT produces a transcription."""

    text: str = ""
    confidence: float = 0.0
    language: str | None = None
    latency_ms: float = 0.0
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class TTSStartedEvent:
    """Emitted when TTS playback begins."""

    mind_id: str = ""
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class TTSCompletedEvent:
    """Emitted when TTS playback finishes."""

    mind_id: str = ""
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class BargeInEvent:
    """Emitted when the user interrupts TTS playback."""

    mind_id: str = ""
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class PipelineErrorEvent:
    """Emitted on unrecoverable pipeline errors."""

    mind_id: str = ""
    error: str = ""
    utterance_id: str = ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
