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
    from collections.abc import AsyncIterator, Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_enum import DeviceEntry

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

    Delegates stream construction to :func:`sovyx.voice._stream_opener.open_input_stream`
    so the full host-API × auto_convert × channels × rate pyramid is tried
    before giving up. The callback pushes mono frames onto an asyncio queue
    via :meth:`asyncio.loop.call_soon_threadsafe` and :meth:`frames` drains
    it — same pattern as :class:`sovyx.voice._capture_task.AudioCaptureTask`.

    Device resolution (``device_id`` → :class:`DeviceEntry`) happens inside
    :meth:`open` to preserve the existing call-sites that pass only an
    integer index. Phase 3 will lift this to the WebSocket route edge so
    telemetry can observe it earlier.
    """

    def __init__(
        self,
        *,
        device_id: int | None,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        blocksize: int = _DEFAULT_BLOCKSIZE,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        tuning: VoiceTuningConfig | None = None,
        sd_module: Any | None = None,  # noqa: ANN401 — DI for tests
        enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,
    ) -> None:
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(
            maxsize=queue_maxsize,
        )
        self._tuning = tuning
        self._sd_module = sd_module
        self._enumerate_fn = enumerate_fn
        self._stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = threading.Event()
        self._info: AudioSourceInfo | None = None

    async def open(self) -> AudioSourceInfo:
        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _default_tuning()
        self._loop = asyncio.get_running_loop()

        entry = _resolve_input_entry(
            device_id=self._device_id,
            enumerate_fn=self._enumerate_fn,
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

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
            )
        except StreamOpenError as exc:
            raise AudioSourceError(exc.code, exc.detail) from exc

        self._stream = stream
        self._sample_rate = info.sample_rate

        self._info = AudioSourceInfo(
            device_id=self._device_id,
            device_name=entry.name,
            sample_rate=info.sample_rate,
            channels=info.channels,
            blocksize=self._blocksize,
        )
        logger.info(
            "voice_test_input_opened",
            device_id=self._device_id,
            device_name=entry.name,
            sample_rate=info.sample_rate,
            channels=info.channels,
            host_api=info.host_api,
            auto_convert=info.auto_convert_used,
            fallback_depth=info.fallback_depth,
        )
        return self._info

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


def _default_tuning() -> VoiceTuningConfig:
    """Construct a default :class:`VoiceTuningConfig` on demand.

    Imported lazily to avoid a pydantic-settings load on module import
    (keeps ``from sovyx.voice.device_test import SoundDeviceInputSource``
    cheap for dashboard routes that only occasionally open a stream).
    """
    from sovyx.engine.config import VoiceTuningConfig

    return VoiceTuningConfig()


def _resolve_input_entry(
    *,
    device_id: int | None,
    enumerate_fn: Callable[[], list[DeviceEntry]] | None,
) -> DeviceEntry:
    """Resolve an input ``device_id`` to a live :class:`DeviceEntry`.

    When ``device_id`` does not match any live entry we fall back to the
    OS default input so the wizard keeps working after a device is
    unplugged mid-session. Raises :class:`AudioSourceError` when the host
    has no input devices at all.
    """
    if enumerate_fn is not None:
        entries = enumerate_fn()
    else:
        from sovyx.voice.device_enum import enumerate_devices

        entries = enumerate_devices()

    candidates = [e for e in entries if e.max_input_channels > 0]
    if not candidates:
        raise AudioSourceError(
            ErrorCode.DEVICE_NOT_FOUND,
            "No audio input devices available",
        )

    if device_id is not None:
        for entry in candidates:
            if entry.index == device_id:
                return entry

    defaults = [e for e in candidates if e.is_os_default]
    return defaults[0] if defaults else candidates[0]


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
