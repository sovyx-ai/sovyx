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
    """Live PortAudio playback via :class:`sounddevice.OutputStream`.

    Kokoro synthesises at 24 kHz. Many Windows output devices — especially
    the MME variant of USB headsets and speakers — refuse non-native rates
    and PortAudio surfaces ``Invalid sample rate [PaErrorCode -9997]``. We
    catch the first failure, query the device's native rate, resample the
    int16 buffer with linear interpolation (quality is not critical for a
    one-shot test phrase) and retry once.
    """

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
            if classified.code != ErrorCode.UNSUPPORTED_SAMPLERATE:
                raise AudioSinkError(classified.code, classified.detail) from exc
            # Rate rejected — query the device's native rate and resample.
            native_rate = await asyncio.to_thread(_query_output_rate, sd, device_id)
            if native_rate <= 0 or native_rate == sample_rate:
                raise AudioSinkError(classified.code, classified.detail) from exc
            logger.info(
                "voice_test_output_resample_fallback",
                device_id=device_id,
                requested_rate=sample_rate,
                native_rate=native_rate,
                reason=str(exc),
            )
            resampled = await asyncio.to_thread(
                _resample_int16,
                audio,
                sample_rate,
                native_rate,
            )
            try:
                await asyncio.to_thread(
                    _blocking_play,
                    sd,
                    resampled,
                    native_rate,
                    device_id,
                )
            except AudioSinkError:
                raise
            except Exception as retry_exc:  # noqa: BLE001
                retry_classified = _classify_portaudio_error(retry_exc)
                raise AudioSinkError(
                    retry_classified.code,
                    retry_classified.detail,
                ) from retry_exc
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


def _query_output_rate(sd: Any, device_id: int | None) -> int:  # noqa: ANN401
    """Return the device's native ``default_samplerate`` or 0 on failure."""
    try:
        info = sd.query_devices(device_id, "output")
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(info, dict):
        return 0
    try:
        return int(info.get("default_samplerate", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
