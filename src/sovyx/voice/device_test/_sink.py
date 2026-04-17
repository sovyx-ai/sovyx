"""Audio output sink abstraction for the TTS test playback.

Same rationale as :mod:`sovyx.voice.device_test._source`: a protocol so
tests can inject a :class:`FakeAudioOutputSink` and assert on what was
written, without monkeypatching ``sounddevice``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._source import AudioSourceError, _classify_portaudio_error

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)


class AudioSinkError(AudioSourceError):
    """Raised when an :class:`AudioOutputSink` cannot open or write."""


@runtime_checkable
class AudioOutputSink(Protocol):
    """Async protocol for a one-shot audio playback sink."""

    async def play(
        self,
        audio: npt.NDArray[np.int16],
        *,
        sample_rate: int,
        device_id: int | None,
    ) -> float:
        """Play the clip synchronously and return actual playback duration (ms)."""
        ...


class SoundDeviceOutputSink:
    """Live PortAudio playback via :class:`sounddevice.OutputStream`."""

    async def play(
        self,
        audio: npt.NDArray[np.int16],
        *,
        sample_rate: int,
        device_id: int | None,
    ) -> float:
        try:
            import sounddevice as sd
        except OSError as exc:
            raise AudioSinkError(
                ErrorCode.INTERNAL_ERROR,
                f"PortAudio unavailable: {exc}",
            ) from exc

        if audio.size == 0:
            return 0.0

        start = asyncio.get_running_loop().time()
        try:
            await asyncio.to_thread(
                _blocking_play,
                sd,
                audio,
                sample_rate,
                device_id,
            )
        except AudioSinkError:
            raise
        except Exception as exc:  # noqa: BLE001
            classified = _classify_portaudio_error(exc)
            raise AudioSinkError(classified.code, classified.detail) from exc
        elapsed_ms = (asyncio.get_running_loop().time() - start) * 1000
        logger.info(
            "voice_test_output_played",
            device_id=device_id,
            sample_rate=sample_rate,
            samples=int(audio.size),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return elapsed_ms


def _blocking_play(
    sd: Any,  # noqa: ANN401
    audio: npt.NDArray[np.int16],
    sample_rate: int,
    device_id: int | None,
) -> None:
    sd.play(audio, samplerate=sample_rate, device=device_id, blocking=True)


class FakeAudioOutputSink:
    """Records calls for tests without touching any audio hardware."""

    def __init__(self, *, error: AudioSinkError | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def play(
        self,
        audio: npt.NDArray[np.int16],
        *,
        sample_rate: int,
        device_id: int | None,
    ) -> float:
        if self._error is not None:
            raise self._error
        self.calls.append(
            {
                "samples": int(audio.size),
                "sample_rate": sample_rate,
                "device_id": device_id,
            },
        )
        # Simulate real playback duration.
        duration_ms = (audio.size / sample_rate) * 1000 if audio.size else 0.0
        return duration_ms
