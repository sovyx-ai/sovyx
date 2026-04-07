"""voice — Voice activity detection, wake word, speech processing, and TTS pipeline."""

from __future__ import annotations

from sovyx.voice.pipeline import (
    AudioOutputQueue,
    BargeInDetector,
    JarvisIllusion,
    VoicePipeline,
    VoicePipelineConfig,
    VoicePipelineState,
)
from sovyx.voice.stt import (
    MoonshineConfig,
    MoonshineSTT,
    PartialTranscription,
    STTEngine,
    STTState,
    TranscriptionResult,
    TranscriptionSegment,
)
from sovyx.voice.tts_kokoro import KokoroConfig, KokoroTTS
from sovyx.voice.tts_piper import AudioChunk, PiperConfig, PiperTTS, TTSEngine
from sovyx.voice.vad import SileroVAD, VADConfig, VADEvent, VADState
from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
)

__all__ = [
    "AudioChunk",
    "AudioOutputQueue",
    "BargeInDetector",
    "JarvisIllusion",
    "KokoroConfig",
    "KokoroTTS",
    "MoonshineConfig",
    "MoonshineSTT",
    "PartialTranscription",
    "PiperConfig",
    "PiperTTS",
    "STTEngine",
    "STTState",
    "SileroVAD",
    "TTSEngine",
    "TranscriptionResult",
    "TranscriptionSegment",
    "VADConfig",
    "VADEvent",
    "VADState",
    "VerificationResult",
    "VoicePipeline",
    "VoicePipelineConfig",
    "VoicePipelineState",
    "WakeWordConfig",
    "WakeWordDetector",
    "WakeWordEvent",
    "WakeWordState",
]
