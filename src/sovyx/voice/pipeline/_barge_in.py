"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.pipeline._constants import _BARGE_IN_THRESHOLD_FRAMES

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice.pipeline._output_queue import AudioOutputQueue
    from sovyx.voice.vad import SileroVAD

logger = get_logger(__name__)


class BargeInDetector:
    """Detects when the user speaks while TTS is playing (barge-in).

    Monitors the VAD while :class:`AudioOutputQueue` is playing.
    If consecutive speech frames exceed the threshold, triggers
    barge-in by interrupting the output queue.

    Args:
        vad: The voice-activity detector.
        output: The audio output queue to interrupt on barge-in.
        threshold_frames: Consecutive speech frames needed to trigger.
    """

    def __init__(
        self,
        vad: SileroVAD,
        output: AudioOutputQueue,
        threshold_frames: int = _BARGE_IN_THRESHOLD_FRAMES,
    ) -> None:
        self._vad = vad
        self._output = output
        self._threshold = threshold_frames

    def check_frame(self, frame: npt.NDArray[np.int16]) -> bool:
        """Process one audio frame and return True if barge-in detected.

        Args:
            frame: Audio frame (512 samples, 16-bit PCM, 16kHz).

        Returns:
            ``True`` if barge-in threshold was reached.
        """
        import numpy as np

        audio_f32 = frame.astype(np.float32) / 32768.0
        event = self._vad.process_frame(audio_f32)
        return event.is_speech

    async def monitor(
        self,
        get_frame: Callable[[], npt.NDArray[np.int16] | None],
    ) -> bool:
        """Monitor for barge-in while output is playing.

        Args:
            get_frame: Callable that returns the next audio frame or None.

        Returns:
            ``True`` if barge-in was detected and output was interrupted.
        """
        consecutive = 0
        while self._output.is_playing:
            frame = get_frame()
            if frame is None:
                await asyncio.sleep(0.01)
                continue
            if self.check_frame(frame):
                consecutive += 1
                if consecutive >= self._threshold:
                    self._output.interrupt()
                    return True
            else:
                consecutive = 0
            await asyncio.sleep(0)  # Yield to event loop
        return False


# ---------------------------------------------------------------------------
# JarvisIllusion — re-exported from jarvis.py (V05-24)
# ---------------------------------------------------------------------------


__all_jarvis__ = ["JarvisIllusion", "JarvisConfig", "split_at_boundaries"]


# ---------------------------------------------------------------------------
# VoicePipeline — main orchestrator
# ---------------------------------------------------------------------------
