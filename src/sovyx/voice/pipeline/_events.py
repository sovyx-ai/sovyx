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
    snr_p50_db: float | None = None
    """Phase 4 / T4.36 — median SNR (dB) over the recent rolling
    window at transcription time. ``None`` when the rolling
    buffer was empty (typical right after boot or during a long
    silence run); downstream consumers should treat this as
    "no SNR signal, fall back to STT confidence unmodified"."""

    snr_confidence_factor: float = 1.0
    """Phase 4 / T4.36 — multiplier in ``[0, 1]`` derived from
    :attr:`snr_p50_db`. Represents how trustworthy the
    transcription is given the room-noise context. Cognitive
    layer multiplies this against :attr:`confidence` to obtain
    the effective confidence before deciding to act on the
    utterance.

    Mapping (linear ramp + clamp):
    * SNR ≥ 17 dB ("excellent" range) → 1.0
    * SNR ≤ 0 dB → 0.0
    * In between → linear interpolation (snr_db / 17.0)

    ``1.0`` when ``snr_p50_db is None`` (no SNR data) so the
    factor is a strict downgrade — never inflates confidence
    above the STT engine's reported value."""


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
