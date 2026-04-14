"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

from enum import IntEnum, auto


class VoicePipelineState(IntEnum):
    """Pipeline state machine.

    Transitions (SPE-010 §13):
        IDLE → WAKE_DETECTED → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE
    Barge-in: SPEAKING → RECORDING (skip wake word — already engaged).
    Timeout:  RECORDING → IDLE (10s max).
    Empty:    TRANSCRIBING → IDLE (empty transcription).
    """

    IDLE = auto()
    WAKE_DETECTED = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()


# ---------------------------------------------------------------------------
# Events (emitted via EventBus)
# ---------------------------------------------------------------------------
