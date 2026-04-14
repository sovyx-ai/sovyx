"""Sovyx voice pipeline — orchestrates wake word -> VAD -> STT -> LLM -> TTS.

This subpackage was split out of the previous monolithic
``voice/pipeline.py`` god file. Public API unchanged: every existing
``from sovyx.voice.pipeline import X`` import continues to work via the
re-exports below.

Module layout:
    _state.py         — VoicePipelineState IntEnum
    _events.py        — 8 event dataclasses emitted on the event bus
    _config.py        — VoicePipelineConfig + validate_config
    _output_queue.py  — AudioOutputQueue (priority queue + ducking)
    _barge_in.py      — BargeInDetector
    _orchestrator.py  — VoicePipeline (orchestrator class)
"""

from __future__ import annotations

from sovyx.voice.jarvis import JarvisIllusion, split_at_boundaries
from sovyx.voice.pipeline._barge_in import BargeInDetector
from sovyx.voice.pipeline._config import VoicePipelineConfig, validate_config
from sovyx.voice.pipeline._events import (
    BargeInEvent,
    PipelineErrorEvent,
    SpeechEndedEvent,
    SpeechStartedEvent,
    TranscriptionCompletedEvent,
    TTSCompletedEvent,
    TTSStartedEvent,
    WakeWordDetectedEvent,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline
from sovyx.voice.pipeline._output_queue import AudioOutputQueue, _play_audio
from sovyx.voice.pipeline._state import VoicePipelineState

__all__ = [
    "AudioOutputQueue",
    "BargeInDetector",
    "BargeInEvent",
    "JarvisIllusion",
    "PipelineErrorEvent",
    "SpeechEndedEvent",
    "SpeechStartedEvent",
    "TTSCompletedEvent",
    "TTSStartedEvent",
    "TranscriptionCompletedEvent",
    "VoicePipeline",
    "VoicePipelineConfig",
    "VoicePipelineState",
    "WakeWordDetectedEvent",
    "_play_audio",
    "split_at_boundaries",
    "validate_config",
]
