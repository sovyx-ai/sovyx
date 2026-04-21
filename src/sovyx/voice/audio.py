"""Audio I/O — Microphone capture and speaker playback.

Platform-aware audio I/O built on ``sounddevice`` (PortAudio).
Supports ALSA (Pi 5), PulseAudio (desktop), CoreAudio (macOS).

References:
    - SPE-010 §2 (AudioCapture)
    - SPE-010 §9 (AudioOutput)
    - IMPL-SUP-005 §SPEC-4 (platform quirks)
    - IMPL-SUP-005 §SPEC-5 (ducking, LUFS normalisation)
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# int16 PCM full-scale reference for dBFS conversion. Using full-scale
# (relative to 32768) is the standard digital-audio convention: 0 dBFS
# is the clipping ceiling, -∞ dBFS is silence, voice typically lands
# between -30 and -10 dBFS. A non-FS "raw" log10 of int16 samples
# would produce numbers that are valid only inside this codebase and
# meaningless against any external audio tool — so we use dBFS.
_INT16_FULL_SCALE: float = 32768.0

# Floor for the silence case. log10(0) is -∞; a fixed sentinel keeps
# downstream consumers (dashboards, anomaly detectors) free of NaN /
# -inf handling. -120 dBFS is well below any meaningful microphone
# noise floor.
_SILENCE_DBFS: float = -120.0

# Per-frame clipping detection threshold. An int16 magnitude at or
# above ~99% of full scale is treated as clipped. The threshold is
# slightly below the ceiling so quantization noise on a pure 0 dBFS
# tone doesn't generate false negatives.
_CLIPPING_INT16_THRESHOLD: int = 32440


def _frame_metrics(samples: np.ndarray) -> tuple[float, float, int]:
    """Compute (rms_dbfs, peak_dbfs, clipping_count) for an int16 frame.

    Uses float32 math to avoid int16 overflow when squaring (a single
    sample at full scale already exceeds int16 range when squared).
    Silence (zero RMS / peak) returns the sentinel
    :data:`_SILENCE_DBFS` so log consumers never see ``-inf`` / ``NaN``.
    """
    if samples.size == 0:
        return _SILENCE_DBFS, _SILENCE_DBFS, 0
    f = samples.astype(np.float32, copy=False)
    rms = float(np.sqrt(np.mean(f * f)))
    peak = float(np.max(np.abs(f))) if f.size else 0.0
    rms_dbfs = 20.0 * np.log10(rms / _INT16_FULL_SCALE) if rms > 0 else _SILENCE_DBFS
    peak_dbfs = 20.0 * np.log10(peak / _INT16_FULL_SCALE) if peak > 0 else _SILENCE_DBFS
    clipping = int(np.sum(np.abs(samples) >= _CLIPPING_INT16_THRESHOLD))
    return rms_dbfs, peak_dbfs, clipping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CAPTURE_RATE = 16_000
"""Default capture sample rate in Hz (16 kHz for STT)."""

DEFAULT_OUTPUT_RATE = 22_050
"""Default output sample rate in Hz (22.05 kHz for Piper TTS)."""

DEFAULT_CHANNELS = 1
"""Mono audio — all voice processing assumes mono."""

DEFAULT_CHUNK_MS = 20
"""Default chunk duration in milliseconds (320 samples @ 16 kHz)."""

RING_BUFFER_SECONDS = 30
"""Ring buffer length for wake-word lookback."""

QUEUE_MAXSIZE = 100
"""Max queued chunks before dropping (~2 s at 20 ms/chunk)."""

OUTPUT_QUEUE_MAXSIZE = 50
"""Max queued output chunks."""


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class AudioPlatform(enum.StrEnum):
    """Detected host audio subsystem."""

    ALSA = "alsa"
    PULSEAUDIO = "pulseaudio"
    COREAUDIO = "coreaudio"
    WASAPI = "wasapi"
    UNKNOWN = "unknown"


def detect_platform() -> AudioPlatform:
    """Detect the host audio subsystem.

    Returns:
        The detected :class:`AudioPlatform`.
    """
    import platform as _platform

    system = _platform.system().lower()
    if system == "darwin":
        return AudioPlatform.COREAUDIO
    if system == "windows":
        return AudioPlatform.WASAPI
    if system == "linux":
        import shutil

        if shutil.which("pactl"):
            return AudioPlatform.PULSEAUDIO
        return AudioPlatform.ALSA
    return AudioPlatform.UNKNOWN


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class RingBuffer:
    """Fixed-size ring buffer for PCM int16 samples.

    Provides O(1) write and O(1) read for the voice pipeline.

    Args:
        max_seconds: Buffer duration in seconds.
        sample_rate: Sample rate in Hz.
    """

    def __init__(
        self,
        max_seconds: int = RING_BUFFER_SECONDS,
        sample_rate: int = DEFAULT_CAPTURE_RATE,
    ) -> None:
        self._capacity = max_seconds * sample_rate
        self._buf: np.ndarray = np.zeros(self._capacity, dtype=np.int16)
        self._write_pos = 0
        self._read_pos = 0
        self._count = 0

    @property
    def capacity(self) -> int:
        """Total buffer capacity in samples."""
        return self._capacity

    @property
    def available(self) -> int:
        """Number of readable samples."""
        return self._count

    def write(self, data: np.ndarray) -> None:
        """Write samples into the ring buffer.

        Args:
            data: 1-D array of int16 samples.
        """
        n = len(data)
        if n == 0:
            return
        if n >= self._capacity:
            # Only keep the last _capacity samples
            data = data[-self._capacity :]
            n = self._capacity
            self._buf[:] = data
            self._write_pos = 0
            self._read_pos = 0
            self._count = self._capacity
            return

        end = self._write_pos + n
        if end <= self._capacity:
            self._buf[self._write_pos : end] = data
        else:
            first = self._capacity - self._write_pos
            self._buf[self._write_pos :] = data[:first]
            self._buf[: n - first] = data[first:]
        self._write_pos = end % self._capacity
        self._count = min(self._count + n, self._capacity)
        # Advance read_pos if we overwrote unread data
        if self._count == self._capacity:
            self._read_pos = self._write_pos

    def read(self, n: int) -> np.ndarray | None:
        """Read *n* samples from the buffer.

        Returns:
            Array of int16 samples, or ``None`` if not enough data.
        """
        if self._count < n:
            return None
        end = self._read_pos + n
        if end <= self._capacity:
            out = self._buf[self._read_pos : end].copy()
        else:
            first = self._capacity - self._read_pos
            out = np.concatenate([self._buf[self._read_pos :], self._buf[: n - first]])
        self._read_pos = end % self._capacity
        self._count -= n
        return out

    def clear(self) -> None:
        """Discard all buffered data."""
        self._write_pos = 0
        self._read_pos = 0
        self._count = 0


# ---------------------------------------------------------------------------
# AudioCapture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioCaptureConfig:
    """Configuration for :class:`AudioCapture`.

    Args:
        sample_rate: Capture rate in Hz.
        channels: Number of channels (1 = mono).
        chunk_ms: Chunk duration in milliseconds.
        device: PortAudio device index or name (``None`` = system default).
        queue_maxsize: Max async queue depth.
    """

    sample_rate: int = DEFAULT_CAPTURE_RATE
    channels: int = DEFAULT_CHANNELS
    chunk_ms: int = DEFAULT_CHUNK_MS
    device: int | str | None = None
    queue_maxsize: int = QUEUE_MAXSIZE


class AudioCapture:
    """Real-time audio capture using ``sounddevice``.

    Captures 16 kHz mono int16 audio in chunks via a PortAudio callback.
    Chunks are placed in an :class:`asyncio.Queue` for async consumption
    by the voice pipeline.

    Args:
        config: Capture configuration (or defaults).
    """

    def __init__(self, config: AudioCaptureConfig | None = None) -> None:
        cfg = config or AudioCaptureConfig()
        self._sample_rate = cfg.sample_rate
        self._channels = cfg.channels
        self._chunk_samples = int(cfg.sample_rate * cfg.chunk_ms / 1000)
        self._device = cfg.device
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=cfg.queue_maxsize)
        self._ring_buffer = RingBuffer(
            max_seconds=RING_BUFFER_SECONDS,
            sample_rate=cfg.sample_rate,
        )
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        # voice.stream_id and voice.device_id are bound on every
        # audio.frame entry so a saga-grouped log view can correlate
        # acoustic telemetry back to the exact stream/device that
        # produced it (vs. a noisy "audio_capture" generic source).
        # The id is regenerated on each start() so a restart after a
        # device hot-plug produces a distinct stream identifier.
        self._stream_id: str = ""
        self._device_label: str = str(cfg.device) if cfg.device is not None else "default"

    # -- Properties ---------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        """Capture sample rate in Hz."""
        return self._sample_rate

    @property
    def chunk_samples(self) -> int:
        """Number of samples per chunk."""
        return self._chunk_samples

    @property
    def is_running(self) -> bool:
        """Whether capture is active."""
        return self._running

    @property
    def ring_buffer(self) -> RingBuffer:
        """Access the underlying ring buffer (e.g. for wake-word lookback)."""
        return self._ring_buffer

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start audio capture.

        Raises:
            RuntimeError: If ``sounddevice`` is unavailable or the device
                cannot be opened.
        """
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        self._stream_id = uuid4().hex[:16]
        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
            blocksize=self._chunk_samples,
            device=self._device,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._running = True
        logger.info(
            "audio_capture_started",
            rate=self._sample_rate,
            chunk_ms=self._chunk_samples * 1000 // self._sample_rate,
            device=self._device or "default",
            **{"voice.stream_id": self._stream_id, "voice.device_id": self._device_label},
        )

    async def stop(self) -> None:
        """Stop audio capture and release resources."""
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("audio_capture_stopped")

    # -- Reading ------------------------------------------------------------

    async def read_chunk(self) -> np.ndarray:
        """Read the next audio chunk (blocks until available).

        Returns:
            1-D int16 numpy array of ``chunk_samples`` length.
        """
        return await self._queue.get()

    def read_chunk_nowait(self) -> np.ndarray | None:
        """Read the next audio chunk without blocking.

        Returns:
            The chunk, or ``None`` if the queue is empty.
        """
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def get_frame(self) -> np.ndarray | None:
        """Read a frame from the ring buffer (non-blocking).

        Returns:
            Array of int16 samples, or ``None`` if insufficient data.
        """
        return self._ring_buffer.read(self._chunk_samples)

    # -- Callback -----------------------------------------------------------

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,  # noqa: ARG002
        time_info: object,  # noqa: ARG002
        status: object,
    ) -> None:
        """sounddevice callback — runs in the audio thread.

        Emits ``audio.frame`` with rms_dbfs / peak_dbfs / clipping
        count on every callback. The structlog SamplingProcessor
        keeps every Nth entry (rate from
        ``ObservabilitySamplingConfig.audio_frame_rate``) so the file
        handler isn't saturated. The metric computation is microseconds
        on a 320-sample int16 frame and the emit path is queue-backed
        (BackgroundLogWriter) — both safe to call from the PortAudio
        callback thread.
        """
        if status:
            logger.warning("audio_input_status", status=str(status))
        # Extract mono channel
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        # Write to ring buffer
        self._ring_buffer.write(mono)
        # Acoustic telemetry — sampled by SamplingProcessor in the
        # structlog chain so emit is cheap and bounded.
        rms_dbfs, peak_dbfs, clipping = _frame_metrics(mono)
        logger.info(
            "audio.frame",
            **{
                "audio.rms_db": rms_dbfs,
                "audio.peak_db": peak_dbfs,
                "audio.clipping": clipping,
                "voice.stream_id": self._stream_id,
                "voice.device_id": self._device_label,
            },
        )
        # Thread-safe enqueue into asyncio
        if self._loop is not None and not self._queue.full():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, mono)

    # -- Device helpers -----------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        """List available audio input devices.

        Returns:
            List of dicts with ``index``, ``name``, ``channels``, ``rate``.
        """
        import sounddevice as sd

        devices: Any = sd.query_devices()
        return [
            {
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "rate": d["default_samplerate"],
            }
            for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]

    @staticmethod
    def negotiate_sample_rate(
        device: int | str | None = None,
        preferred: int = DEFAULT_CAPTURE_RATE,
    ) -> int:
        """Find the best supported sample rate for *device*.

        Tries *preferred* first, then common alternatives.

        Args:
            device: Device index/name (``None`` = default).
            preferred: Desired sample rate in Hz.

        Returns:
            A supported sample rate.

        Raises:
            RuntimeError: If no rate is supported.
        """
        import sounddevice as sd

        rates_to_try = [preferred, 44100, 48000, 22050, 8000]
        for rate in rates_to_try:
            try:
                sd.check_input_settings(device=device, samplerate=rate)
                return rate
            except sd.PortAudioError:
                continue
        msg = f"No supported sample rate found for device {device}"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Audio output
# ---------------------------------------------------------------------------


class OutputPriority(enum.IntEnum):
    """Playback priority (lower value = higher priority)."""

    FILLER = 0
    NORMAL = 1
    LOW = 2


@dataclass
class OutputChunk:
    """Wrapper around audio data with priority metadata.

    Args:
        audio: 1-D float32 or int16 PCM array.
        sample_rate: Sample rate in Hz.
        priority: Playback priority.
        timestamp: Monotonic enqueue time.
    """

    audio: np.ndarray
    sample_rate: int
    priority: OutputPriority = OutputPriority.NORMAL
    timestamp: float = field(default_factory=time.monotonic)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, OutputChunk):
            return NotImplemented
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp

    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds."""
        return len(self.audio) / self.sample_rate * 1000


@dataclass(frozen=True)
class AudioOutputConfig:
    """Configuration for :class:`AudioOutput`.

    Args:
        sample_rate: Output sample rate in Hz.
        channels: Number of channels.
        device: PortAudio device index or name.
        target_lufs: Target loudness in LUFS.
    """

    sample_rate: int = DEFAULT_OUTPUT_RATE
    channels: int = DEFAULT_CHANNELS
    device: int | str | None = None
    target_lufs: float = -16.0


# ---------------------------------------------------------------------------
# AudioDucker
# ---------------------------------------------------------------------------


class AudioDucker:
    """Reduce background audio volume when the agent is speaking.

    Uses a simple gain reduction with configurable duck level.

    Args:
        duck_level_db: Volume reduction in dB (negative).
        fade_in_ms: Duration to fade background back in.
        fade_out_ms: Duration to fade background down.
        sample_rate: Sample rate for fade calculation.
    """

    def __init__(
        self,
        duck_level_db: float = -12.0,
        fade_in_ms: int = 50,
        fade_out_ms: int = 30,
        sample_rate: int = DEFAULT_OUTPUT_RATE,
    ) -> None:
        self._duck_gain = 10 ** (duck_level_db / 20)
        self._fade_in_samples = int(sample_rate * fade_in_ms / 1000)
        self._fade_out_samples = int(sample_rate * fade_out_ms / 1000)
        self._is_ducked = False

    @property
    def duck_gain(self) -> float:
        """Linear gain factor when ducked."""
        return self._duck_gain

    @property
    def is_ducked(self) -> bool:
        """Whether ducking is currently active."""
        return self._is_ducked

    def duck(self, background: np.ndarray, is_speaking: bool) -> np.ndarray:
        """Apply ducking to *background* audio.

        Args:
            background: Background audio samples (float32).
            is_speaking: Whether the agent is currently speaking.

        Returns:
            Ducked (or unmodified) audio array.
        """
        if is_speaking:
            self._is_ducked = True
            return (background * self._duck_gain).astype(background.dtype)
        if self._is_ducked:
            # Fade back in
            self._is_ducked = False
            fade_len = min(self._fade_in_samples, len(background))
            if fade_len > 0:
                result = background.copy()
                fade = np.linspace(self._duck_gain, 1.0, fade_len)
                result[:fade_len] = (result[:fade_len] * fade).astype(result.dtype)
                return result
        return background


# ---------------------------------------------------------------------------
# LUFS normalisation
# ---------------------------------------------------------------------------


def normalize_lufs(audio: np.ndarray, target: float = -16.0) -> np.ndarray:
    """Normalise *audio* to *target* LUFS (simplified RMS-based).

    Uses an RMS-based approximation of integrated loudness.
    For full ITU-R BS.1770-4 compliance, use ``pyloudnorm``.

    Args:
        audio: 1-D float32 audio.
        target: Target loudness in LUFS.

    Returns:
        Loudness-normalised audio clipped to [-1, 1].
    """
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-6:
        return audio
    current_lufs = 20 * np.log10(rms) - 0.691
    gain_db = target - current_lufs
    gain_linear = 10 ** (gain_db / 20)
    # Clamp gain to avoid distortion
    gain_linear = min(gain_linear, 4.0)
    result: np.ndarray = np.clip(audio * gain_linear, -1.0, 1.0).astype(audio.dtype)
    return result


# ---------------------------------------------------------------------------
# AudioOutput
# ---------------------------------------------------------------------------


class AudioOutput:
    """Audio output with priority queue, ducking, and LUFS normalisation.

    Features:
        - Priority queue (fillers > responses > notifications).
        - LUFS normalisation (-16 LUFS target for voice).
        - Fade out on flush to avoid clicks.
        - Instant flush for barge-in.

    Args:
        config: Output configuration (or defaults).
    """

    def __init__(self, config: AudioOutputConfig | None = None) -> None:
        cfg = config or AudioOutputConfig()
        self._sample_rate = cfg.sample_rate
        self._channels = cfg.channels
        self._device = cfg.device
        self._target_lufs = cfg.target_lufs
        self._queue: asyncio.PriorityQueue[OutputChunk] = asyncio.PriorityQueue(
            maxsize=OUTPUT_QUEUE_MAXSIZE,
        )
        self._stream: Any = None
        self._is_playing = False
        self._current_audio: np.ndarray | None = None
        self._play_position = 0
        self._ducker = AudioDucker(sample_rate=cfg.sample_rate)

    # -- Properties ---------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        return self._sample_rate

    @property
    def is_playing(self) -> bool:
        """Whether audio is currently being played."""
        return self._is_playing

    @property
    def ducker(self) -> AudioDucker:
        """Access the audio ducker."""
        return self._ducker

    @property
    def queue_size(self) -> int:
        """Number of chunks in the playback queue."""
        return self._queue.qsize()

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the output stream.

        Raises:
            RuntimeError: If ``sounddevice`` is unavailable.
        """
        import sounddevice as sd

        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="float32",
            device=self._device,
        )
        self._stream.start()
        logger.info(
            "audio_output_started",
            rate=self._sample_rate,
            device=self._device or "default",
        )

    async def stop(self) -> None:
        """Close the output stream and release resources."""
        self._is_playing = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("audio_output_stopped")

    # -- Enqueue / play -----------------------------------------------------

    async def enqueue(
        self,
        audio: np.ndarray,
        sample_rate: int | None = None,
        priority: OutputPriority = OutputPriority.NORMAL,
    ) -> None:
        """Add audio to the playback queue.

        The audio is LUFS-normalised before queueing.

        Args:
            audio: 1-D PCM array (float32 or int16).
            sample_rate: Sample rate (defaults to output rate).
            priority: Playback priority.
        """
        sr = sample_rate or self._sample_rate
        # Convert int16 → float32 if necessary
        if audio.dtype == np.int16:
            audio_f32 = audio.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio.astype(np.float32)
        normalised = normalize_lufs(audio_f32, self._target_lufs)
        chunk = OutputChunk(audio=normalised, sample_rate=sr, priority=priority)
        await self._queue.put(chunk)

    async def play_immediate(self, audio: np.ndarray, sample_rate: int | None = None) -> None:
        """Play audio immediately, bypassing the queue.

        Args:
            audio: 1-D PCM array.
            sample_rate: Sample rate (defaults to output rate).
        """
        sr = sample_rate or self._sample_rate
        if audio.dtype == np.int16:
            audio_f32 = audio.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio.astype(np.float32)
        normalised = normalize_lufs(audio_f32, self._target_lufs)
        self._is_playing = True
        try:
            await self._play_chunk(normalised, sr)
        finally:
            self._is_playing = False

    async def drain(self) -> None:
        """Play all queued chunks sequentially until the queue is empty."""
        self._is_playing = True
        try:
            while not self._queue.empty():
                chunk = self._queue.get_nowait()
                self._current_audio = chunk.audio
                self._play_position = 0
                await self._play_chunk(chunk.audio, chunk.sample_rate)
        finally:
            self._is_playing = False
            self._current_audio = None
            self._play_position = 0

    # -- Flush / barge-in ---------------------------------------------------

    def flush(self) -> None:
        """Immediately stop playback and clear the queue (barge-in)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._current_audio = None
        self._play_position = 0
        self._is_playing = False

    def apply_fade_out(self, samples: int = 160) -> None:
        """Apply a fade-out to avoid clicks on sudden stop.

        Args:
            samples: Number of samples over which to fade.
        """
        if self._current_audio is not None and self._play_position > 0:
            end = min(self._play_position + samples, len(self._current_audio))
            fade_len = end - self._play_position
            if fade_len > 0:
                fade = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
                self._current_audio[self._play_position : end] = (
                    self._current_audio[self._play_position : end] * fade
                )

    # -- Internal -----------------------------------------------------------

    async def _play_chunk(self, audio: np.ndarray, sample_rate: int) -> None:
        """Play a single audio chunk.

        Uses :func:`sovyx.voice._stream_opener.blocking_write_play`
        (``sd.OutputStream.write`` blocking path, threadpool-safe)
        when sounddevice is available; falls back to sleep-based
        simulation for headless / test environments.

        ``sd.play`` is avoided because its callback engine needs COM
        on the calling thread — a requirement that
        :func:`asyncio.to_thread` workers do not satisfy on Windows +
        WASAPI. See :func:`blocking_write_play` for the root cause.
        """
        try:
            import sounddevice as sd
        except ImportError:
            duration = len(audio) / sample_rate
            await asyncio.sleep(duration)
            return

        from sovyx.voice._stream_opener import blocking_write_play

        device = self._device
        await asyncio.to_thread(
            blocking_write_play,
            sd,
            audio,
            sample_rate,
            device=device,
        )

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        """List available audio output devices.

        Returns:
            List of dicts with ``index``, ``name``, ``channels``, ``rate``.
        """
        import sounddevice as sd

        devices: Any = sd.query_devices()
        return [
            {
                "index": i,
                "name": d["name"],
                "channels": d["max_output_channels"],
                "rate": d["default_samplerate"],
            }
            for i, d in enumerate(devices)
            if d["max_output_channels"] > 0
        ]
