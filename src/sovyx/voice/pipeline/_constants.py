"""Pipeline tuning constants — shared by config, orchestrator, and barge-in."""

from __future__ import annotations

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32ms at 16kHz
_SILENCE_FRAMES_END = 22  # ~700ms silence -> end of utterance
_MAX_RECORDING_FRAMES = 312  # ~10s max recording
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160ms sustained speech -> barge-in
_FILLER_DELAY_MS = 800  # Play filler if no LLM token within this
_TEXT_MIN_WORDS = 3  # Min words before TTS synthesis

__all__ = [
    "_BARGE_IN_THRESHOLD_FRAMES",
    "_FILLER_DELAY_MS",
    "_FRAME_SAMPLES",
    "_MAX_RECORDING_FRAMES",
    "_SAMPLE_RATE",
    "_SILENCE_FRAMES_END",
    "_TEXT_MIN_WORDS",
]
