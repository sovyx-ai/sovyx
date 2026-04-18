"""Audio output sink abstraction for the TTS test playback.

Same rationale as :mod:`sovyx.voice.device_test._source`: a protocol so
tests can inject a :class:`FakeAudioOutputSink` and assert on what was
written, without monkeypatching ``sounddevice``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._source import AudioSourceError

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_enum import DeviceEntry

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
    """Live PortAudio playback delegating to :func:`_stream_opener.play_audio`.

    Kokoro synthesises at 24 kHz; many Windows output devices — especially
    the MME variant of USB headsets — refuse non-native rates. The opener
    handles rate + channel + WASAPI auto_convert fallback centrally, so
    this sink is a thin wrapper that (a) resolves ``device_id`` to a
    :class:`DeviceEntry` and (b) maps :class:`StreamOpenError` to the
    sink's public :class:`AudioSinkError` type.
    """

    def __init__(
        self,
        *,
        tuning: VoiceTuningConfig | None = None,
        sd_module: Any | None = None,  # noqa: ANN401 — DI for tests
        enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,
    ) -> None:
        self._tuning = tuning
        self._sd_module = sd_module
        self._enumerate_fn = enumerate_fn

    async def play(
        self,
        audio: npt.NDArray[np.int16],
        *,
        sample_rate: int,
        device_id: int | None,
    ) -> float:
        from sovyx.voice._stream_opener import StreamOpenError, play_audio

        if audio.size == 0:
            return 0.0

        tuning = self._tuning if self._tuning is not None else _default_tuning()
        entry = _resolve_output_entry(
            device_id=device_id,
            enumerate_fn=self._enumerate_fn,
        )

        try:
            elapsed_ms = await play_audio(
                audio,
                source_rate=sample_rate,
                device=entry,
                tuning=tuning,
                sd_module=self._sd_module,
            )
        except StreamOpenError as exc:
            raise AudioSinkError(exc.code, exc.detail) from exc

        logger.info(
            "voice_test_output_played",
            device_id=device_id,
            device_name=entry.name,
            host_api=entry.host_api_name,
            sample_rate=sample_rate,
            samples=int(audio.size),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return elapsed_ms


def _default_tuning() -> VoiceTuningConfig:
    from sovyx.engine.config import VoiceTuningConfig

    return VoiceTuningConfig()


def _resolve_output_entry(
    *,
    device_id: int | None,
    enumerate_fn: Callable[[], list[DeviceEntry]] | None,
) -> DeviceEntry:
    """Resolve an output ``device_id`` to a live :class:`DeviceEntry`."""
    if enumerate_fn is not None:
        entries = enumerate_fn()
    else:
        from sovyx.voice.device_enum import enumerate_devices

        entries = enumerate_devices()

    candidates = [e for e in entries if e.max_output_channels > 0]
    if not candidates:
        raise AudioSinkError(
            ErrorCode.DEVICE_NOT_FOUND,
            "No audio output devices available",
        )

    if device_id is not None:
        for entry in candidates:
            if entry.index == device_id:
                return entry

    defaults = [e for e in candidates if e.is_os_default]
    return defaults[0] if defaults else candidates[0]


def _resample_int16(
    audio: npt.NDArray[np.int16],
    src_rate: int,
    dst_rate: int,
) -> npt.NDArray[np.int16]:
    """Linear-interpolation resample of a mono int16 buffer."""
    import numpy as np  # noqa: PLC0415

    if src_rate == dst_rate or audio.size == 0:
        return audio
    src_len = int(audio.size)
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    x_src = np.linspace(0.0, 1.0, num=src_len, endpoint=False, dtype=np.float64)
    x_dst = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
    resampled = np.interp(x_dst, x_src, audio.astype(np.float32))
    clipped = np.clip(resampled, -32_768, 32_767)
    return clipped.astype(np.int16)


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
