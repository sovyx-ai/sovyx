"""Audio input source abstraction for the voice device test.

The :class:`AudioInputSource` protocol lets the test session open an
arbitrary source (real PortAudio device or an in-process fake that
replays a known signal) without changing session code. Tests inject
:class:`FakeAudioInputSource`; production uses :class:`SoundDeviceInputSource`.

Keeping this as a protocol (not a concrete class) means we never need
``sys.modules`` patching of ``sounddevice`` in tests — a long-standing
anti-pattern in this codebase (see CLAUDE.md rule #2).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.device_test._protocol import ErrorCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)


class AudioSourceError(Exception):
    """Raised when an :class:`AudioInputSource` cannot open or read."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AudioSourceInfo:
    """Metadata surfaced once the source is open."""

    device_id: int | None
    device_name: str
    sample_rate: int
    channels: int
    blocksize: int


@runtime_checkable
class AudioInputSource(Protocol):
    """Async protocol for an audio input stream."""

    async def open(self) -> AudioSourceInfo:
        """Open the stream. Raise :class:`AudioSourceError` on failure."""
        ...

    async def close(self) -> None:
        """Close the stream. Must be idempotent."""
        ...

    def frames(self) -> AsyncIterator[npt.NDArray[np.int16]]:
        """Yield int16 mono frames at the configured sample rate."""
        ...


# ---------------------------------------------------------------------------
# Real implementation: sounddevice / PortAudio
# ---------------------------------------------------------------------------


_DEFAULT_SAMPLE_RATE = 16_000
_DEFAULT_BLOCKSIZE = 512
_DEFAULT_QUEUE_MAXSIZE = 32


class SoundDeviceInputSource:
    """Live PortAudio microphone stream.

    Follows the same pattern as
    :class:`sovyx.voice._capture_task.AudioCaptureTask`: the PortAudio
    thread pushes frames onto an asyncio queue via
    :meth:`asyncio.loop.call_soon_threadsafe` and :meth:`frames` drains it.
    """

    def __init__(
        self,
        *,
        device_id: int | None,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        blocksize: int = _DEFAULT_BLOCKSIZE,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(
            maxsize=queue_maxsize,
        )
        self._stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = threading.Event()
        self._info: AudioSourceInfo | None = None

    async def open(self) -> AudioSourceInfo:
        try:
            import sounddevice as sd
        except OSError as exc:
            raise AudioSourceError(
                ErrorCode.INTERNAL_ERROR,
                f"PortAudio unavailable: {exc}",
            ) from exc

        self._loop = asyncio.get_running_loop()
        device_name, default_sr = await asyncio.to_thread(
            self._probe_device,
            sd,
        )

        def _callback(
            indata: npt.NDArray[np.int16],
            _frames: int,
            _time: object,
            status: object,
        ) -> None:
            if status:
                logger.debug("voice_test_input_status", status=str(status))
            if self._closed.is_set() or self._loop is None:
                return
            # Copy: the buffer is reused by PortAudio after callback returns.
            frame = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
            with contextlib.suppress(RuntimeError):
                # Loop closed — ignore.
                self._loop.call_soon_threadsafe(self._enqueue, frame)

        stream, used_rate = await self._open_stream_with_rate_fallback(
            sd,
            _callback,
            default_sr,
        )
        self._sample_rate = used_rate
        self._stream = stream

        self._info = AudioSourceInfo(
            device_id=self._device_id,
            device_name=device_name,
            sample_rate=used_rate,
            channels=1,
            blocksize=self._blocksize,
        )
        logger.info(
            "voice_test_input_opened",
            device_id=self._device_id,
            device_name=device_name,
            sample_rate=used_rate,
            default_samplerate=default_sr,
        )
        return self._info

    async def _open_stream_with_rate_fallback(
        self,
        sd: Any,  # noqa: ANN401
        callback: Any,  # noqa: ANN401
        default_sr: int,
    ) -> tuple[Any, int]:
        """Open the input stream, retrying at the device's native rate on rejection.

        Windows MME (and occasionally WASAPI in exclusive mode) refuses
        non-native sample rates with PaErrorCode -9997. Rather than hand
        the user a blank meter, we log the raw error at INFO, query the
        device's ``default_samplerate`` and retry once. The RMS/peak meter
        is rate-agnostic so the wizard still reports useful levels.
        """

        def _factory(rate: int) -> Any:  # noqa: ANN401
            return sd.InputStream(
                samplerate=rate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device_id,
                callback=callback,
            )

        try:
            stream = await asyncio.to_thread(_factory, self._sample_rate)
            await asyncio.to_thread(stream.start)
            return stream, self._sample_rate
        except Exception as exc:  # noqa: BLE001
            classified = _classify_portaudio_error(exc)
            if classified.code != ErrorCode.UNSUPPORTED_SAMPLERATE:
                raise classified from exc
            if default_sr <= 0 or default_sr == self._sample_rate:
                raise classified from exc
            logger.info(
                "voice_test_input_rate_fallback",
                device_id=self._device_id,
                requested_rate=self._sample_rate,
                native_rate=default_sr,
                reason=str(exc),
            )

        try:
            stream = await asyncio.to_thread(_factory, default_sr)
            await asyncio.to_thread(stream.start)
            return stream, default_sr
        except Exception as retry_exc:  # noqa: BLE001
            raise _classify_portaudio_error(retry_exc) from retry_exc

    def _probe_device(self, sd: Any) -> tuple[str, int]:  # noqa: ANN401
        try:
            info = sd.query_devices(self._device_id, "input")
        except (ValueError, OSError) as exc:
            raise AudioSourceError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"Input device not found: {exc}",
            ) from exc
        name = str(info.get("name", "unknown")) if isinstance(info, dict) else "unknown"
        default_sr = int(info.get("default_samplerate", 0)) if isinstance(info, dict) else 0
        return name, default_sr

    def _enqueue(self, frame: npt.NDArray[np.int16]) -> None:
        # Drop oldest on overflow — we stream levels, not audio.
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(frame)

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            await asyncio.to_thread(stream.stop)
        except Exception as exc:  # noqa: BLE001
            logger.debug("voice_test_input_stop_failed", error=str(exc))
        try:
            await asyncio.to_thread(stream.close)
        except Exception as exc:  # noqa: BLE001
            logger.debug("voice_test_input_close_failed", error=str(exc))
        logger.info("voice_test_input_closed", device_id=self._device_id)

    async def frames(self) -> AsyncIterator[npt.NDArray[np.int16]]:
        while not self._closed.is_set():
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                if self._closed.is_set():
                    break
                continue
            yield frame


def _classify_portaudio_error(exc: BaseException) -> AudioSourceError:
    """Map a raw PortAudio exception into a typed :class:`AudioSourceError`.

    Windows WASAPI surfaces shared-mode mixer mismatches as
    ``AUDCLNT_E_UNSUPPORTED_FORMAT`` (HRESULT ``0x88890008`` / decimal
    ``-2004287480``). These hit ``PaErrorCode -9999`` (paUnanticipatedHostError)
    so the generic PortAudio substring matchers never see them as a
    sample-rate or channel problem. The ``AUDCLNT_*`` patterns are checked
    first because they let the frontend render an actionable hint
    ("change the microphone format in Windows Sound settings") instead
    of surfacing the raw host-error string.
    """
    msg = str(exc).lower()

    # Windows WASAPI AUDCLNT_* macros (checked first — they convey richer
    # semantics than the generic "sample rate"/"channel" substrings).
    if "audclnt_e_unsupported_format" in msg or "0x88890008" in msg or "-2004287480" in msg:
        return AudioSourceError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"WASAPI mixer format mismatch: {exc}",
        )
    if (
        "audclnt_e_exclusive_mode_not_allowed" in msg
        or "audclnt_e_device_in_use" in msg
        or "audclnt_e_resource_not_available" in msg
    ):
        return AudioSourceError(
            ErrorCode.DEVICE_BUSY,
            f"Device is busy (another app holding it): {exc}",
        )
    if "audclnt_e_buffer_size" in msg or "audclnt_e_buffer_too_large" in msg:
        return AudioSourceError(
            ErrorCode.BUFFER_SIZE_INVALID,
            f"Buffer size rejected by WASAPI: {exc}",
        )
    if "audclnt_e_endpoint_create_failed" in msg or "audclnt_e_service_not_running" in msg:
        return AudioSourceError(
            ErrorCode.DEVICE_NOT_FOUND,
            f"Audio endpoint unavailable: {exc}",
        )

    if "invalid device" in msg or "device unavailable" in msg:
        return AudioSourceError(
            ErrorCode.DEVICE_NOT_FOUND,
            f"Device unavailable: {exc}",
        )
    if "invalid sample rate" in msg or "sample rate" in msg:
        return AudioSourceError(
            ErrorCode.UNSUPPORTED_SAMPLERATE,
            f"Sample rate not supported: {exc}",
        )
    if "invalid number of channels" in msg or "channel" in msg:
        return AudioSourceError(
            ErrorCode.UNSUPPORTED_CHANNELS,
            f"Channel configuration not supported: {exc}",
        )
    if "busy" in msg or "exclusive" in msg or "already in use" in msg:
        return AudioSourceError(
            ErrorCode.DEVICE_BUSY,
            f"Device is busy (another app holding it): {exc}",
        )
    if "permission" in msg or "access denied" in msg:
        return AudioSourceError(
            ErrorCode.PERMISSION_DENIED,
            f"Permission denied for audio device: {exc}",
        )
    return AudioSourceError(
        ErrorCode.INTERNAL_ERROR,
        f"Failed to open input stream: {exc}",
    )


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeAudioInputSource:
    """In-memory :class:`AudioInputSource` for tests.

    Yields pre-recorded ``int16`` frames at a controllable cadence (defaults
    to 30 Hz to match the production frame rate). Setting ``error_on_open``
    to an :class:`AudioSourceError` causes :meth:`open` to raise it.
    """

    def __init__(
        self,
        frames: list[npt.NDArray[np.int16]],
        *,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        device_id: int | None = 0,
        device_name: str = "FakeInput",
        frame_interval_s: float = 1 / 30,
        error_on_open: AudioSourceError | None = None,
    ) -> None:
        self._frames = frames
        self._sample_rate = sample_rate
        self._device_id = device_id
        self._device_name = device_name
        self._frame_interval_s = frame_interval_s
        self._error_on_open = error_on_open
        self._closed = False

    async def open(self) -> AudioSourceInfo:
        if self._error_on_open is not None:
            raise self._error_on_open
        return AudioSourceInfo(
            device_id=self._device_id,
            device_name=self._device_name,
            sample_rate=self._sample_rate,
            channels=1,
            blocksize=self._frames[0].size if self._frames else 0,
        )

    async def close(self) -> None:
        self._closed = True

    async def frames(self) -> AsyncIterator[npt.NDArray[np.int16]]:
        for frame in self._frames:
            if self._closed:
                break
            yield frame
            await asyncio.sleep(self._frame_interval_s)
