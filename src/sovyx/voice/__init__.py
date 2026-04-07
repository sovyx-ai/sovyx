"""voice — Voice activity detection and speech processing pipeline."""

from __future__ import annotations

from sovyx.voice.vad import SileroVAD, VADConfig, VADEvent, VADState
from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
)

__all__ = [
    "SileroVAD",
    "VADConfig",
    "VADEvent",
    "VADState",
    "VerificationResult",
    "WakeWordConfig",
    "WakeWordDetector",
    "WakeWordEvent",
    "WakeWordState",
]
