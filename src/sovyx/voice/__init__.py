"""voice — Voice activity detection and speech processing pipeline."""

from __future__ import annotations

from sovyx.voice.vad import SileroVAD, VADConfig, VADEvent, VADState

__all__ = ["SileroVAD", "VADConfig", "VADEvent", "VADState"]
