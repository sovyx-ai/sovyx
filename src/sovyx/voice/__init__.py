"""voice — Voice activity detection, wake word, and speech processing pipeline."""

from __future__ import annotations

from sovyx.voice.stt import (
    MoonshineConfig,
    MoonshineSTT,
    PartialTranscription,
    STTEngine,
    STTState,
    TranscriptionResult,
    TranscriptionSegment,
)
from sovyx.voice.vad import SileroVAD, VADConfig, VADEvent, VADState
from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
)

__all__ = [
    "MoonshineConfig",
    "MoonshineSTT",
    "PartialTranscription",
    "STTEngine",
    "STTState",
    "SileroVAD",
    "TranscriptionResult",
    "TranscriptionSegment",
    "VADConfig",
    "VADEvent",
    "VADState",
    "VerificationResult",
    "WakeWordConfig",
    "WakeWordDetector",
    "WakeWordEvent",
    "WakeWordState",
]
