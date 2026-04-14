"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

from dataclasses import dataclass

from sovyx.voice.pipeline._constants import (
    _BARGE_IN_THRESHOLD_FRAMES,
    _FILLER_DELAY_MS,
    _MAX_RECORDING_FRAMES,
    _SILENCE_FRAMES_END,
)


@dataclass(frozen=True, slots=True)
class VoicePipelineConfig:
    """Configuration for the VoicePipeline orchestrator.

    Attributes:
        mind_id: Owning mind identifier.
        wake_word_enabled: Whether to require wake word before recording.
        barge_in_enabled: Whether user can interrupt TTS by speaking.
        fillers_enabled: Whether to play filler phrases during LLM thinking.
        filler_delay_ms: Milliseconds to wait before playing a filler.
        silence_frames_end: Consecutive silent frames to end utterance (~32ms each).
        max_recording_frames: Maximum frames before force-ending recording.
        barge_in_threshold: Consecutive speech frames to trigger barge-in.
        confirmation_tone: Type of tone on wake word (``"beep"`` or ``"none"``).
        filler_phrases: Phrases used during LLM thinking time.
    """

    mind_id: str = "default"
    wake_word_enabled: bool = True
    barge_in_enabled: bool = True
    fillers_enabled: bool = True
    filler_delay_ms: int = _FILLER_DELAY_MS
    silence_frames_end: int = _SILENCE_FRAMES_END
    max_recording_frames: int = _MAX_RECORDING_FRAMES
    barge_in_threshold: int = _BARGE_IN_THRESHOLD_FRAMES
    confirmation_tone: str = "beep"
    filler_phrases: tuple[str, ...] = (
        "Let me think about that...",
        "Hmm...",
        "One moment...",
        "Let me check...",
        "Sure, let me look into that...",
    )


def validate_config(config: VoicePipelineConfig) -> None:
    """Validate pipeline configuration.

    Raises:
        ValueError: If any parameter is out of range.
    """
    if config.filler_delay_ms < 0:
        msg = f"filler_delay_ms must be >= 0, got {config.filler_delay_ms}"
        raise ValueError(msg)
    if config.silence_frames_end < 1:
        msg = f"silence_frames_end must be >= 1, got {config.silence_frames_end}"
        raise ValueError(msg)
    if config.max_recording_frames < 1:
        msg = f"max_recording_frames must be >= 1, got {config.max_recording_frames}"
        raise ValueError(msg)
    if config.barge_in_threshold < 1:
        msg = f"barge_in_threshold must be >= 1, got {config.barge_in_threshold}"
        raise ValueError(msg)
    if config.confirmation_tone not in ("beep", "none"):
        msg = f"confirmation_tone must be 'beep' or 'none', got {config.confirmation_tone!r}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# AudioOutputQueue — managed playback with interruption
# ---------------------------------------------------------------------------
