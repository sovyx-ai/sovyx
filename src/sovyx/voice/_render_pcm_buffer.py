"""Render-side PCM ring buffer for AEC reference (Phase 4 / T4.4.b).

Concrete implementation of the
:class:`sovyx.voice._aec.RenderPcmProvider` Protocol that the
playback path (TTS) feeds and the capture path (FrameNormalizer)
reads. Bridges the producer/consumer thread split:

* **Producer** — :func:`sovyx.voice.pipeline._output_queue._play_audio`
  worker thread. Calls :meth:`RenderPcmBuffer.feed` BEFORE handing
  the chunk to ``sd.OutputStream.write``. The worker thread is
  the same thread that PortAudio drives playback on, so feed +
  playback share a clock.

* **Consumer** — :class:`sovyx.voice._frame_normalizer.FrameNormalizer`
  via its ``render_provider`` parameter. Calls
  :meth:`get_aligned_window` once per emitted 512-sample capture
  window, on the asyncio loop thread.

Synchronisation is :class:`threading.Lock` (not asyncio) because
the producer is a worker thread, not a coroutine.

Alignment strategy — pragmatic foundation:

The Speex AEC adaptive filter (configured via
``voice_aec_filter_length_ms``, default 128 ms = 2048 samples @
16 kHz) accommodates up to ~filter_length samples of render-to-
capture delay automatically. We don't need to compute the
playback + capture latencies exactly — we just keep the most
recent N seconds of rendered PCM in the ring and return the most
recent ``n`` samples on every read. The filter learns to look
back the right amount.

This is correct for the typical desktop hardware delay budget
(speaker output latency 10-50 ms + air propagation < 5 ms + mic
input latency 10-30 ms = 25-85 ms total, well inside the 128 ms
Speex window). Hardware with tail > 128 ms (large room PA, very
high-latency Bluetooth speakers) would need either a longer
filter or a dedicated alignment-tracking provider; T4.4.c can
revisit if telemetry shows the gap.

Resampling — TTS engines emit PCM at varied rates
(Piper 22050 Hz, Kokoro 24000 Hz, custom voices anywhere). The
buffer transparently resamples to the FrameNormalizer's 16 kHz
invariant via :func:`scipy.signal.resample_poly`. The polyphase
FIR is the same algorithm the FrameNormalizer uses for capture-
side resampling so render and capture share spectral
characteristics.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import numpy as np

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy.typing as npt

logger = get_logger(__name__)


_TARGET_RATE = 16_000
"""Output sample rate — matches :mod:`sovyx.voice._frame_normalizer`."""

_DEFAULT_BUFFER_SECONDS = 2.0
"""Ring-buffer size in seconds.

Two seconds covers the worst-case AEC filter window (typically
≤ 128 ms) plus a margin for jittery render→capture pipelining.
Larger buffers waste memory; shorter buffers risk truncating long
filter tails. 2 s @ 16 kHz mono int16 = 64 KiB — negligible for a
long-running daemon.
"""


class RenderPcmBuffer:
    """Thread-safe ring buffer for render-side PCM.

    Implements :class:`sovyx.voice._aec.RenderPcmProvider`.

    Producer (playback thread) calls :meth:`feed` to push the next
    PCM chunk; consumer (capture-stage call site on the asyncio
    loop) calls :meth:`get_aligned_window` to read the most recent
    ``n`` samples for AEC reference.

    Args:
        buffer_seconds: Ring capacity in seconds at the target
            16 kHz output rate. Default 2.0 s. Sub-100 ms values
            are rejected — they don't even cover one Speex AEC
            filter window. Hard upper bound 30 s — beyond that,
            the operator should question whether they actually
            want a render buffer or some other primitive.

    Raises:
        ValueError: If ``buffer_seconds`` is outside ``[0.1, 30.0]``.
    """

    def __init__(self, *, buffer_seconds: float = _DEFAULT_BUFFER_SECONDS) -> None:
        if not (0.1 <= buffer_seconds <= 30.0):  # noqa: PLR2004 — bounds documented above
            msg = f"buffer_seconds must be in [0.1, 30.0], got {buffer_seconds!r}"
            raise ValueError(msg)

        self._capacity = int(round(buffer_seconds * _TARGET_RATE))
        # Pre-allocated ring; values are zero on construction so any
        # ``get_aligned_window`` issued before the first ``feed`` returns
        # silence (the AEC short-circuit branch).
        self._ring: npt.NDArray[np.int16] = np.zeros(self._capacity, dtype=np.int16)
        # ``_write_head`` is the index where the NEXT sample will land.
        # ``_filled`` tracks how many samples have ever been written
        # (clamped to ``_capacity`` so reads beyond the buffer return
        # zero-padded windows rather than wrapping into stale slots).
        self._write_head: int = 0
        self._filled: int = 0
        self._lock = threading.Lock()

    # ── Producer (playback thread) ──────────────────────────────────────

    def feed(
        self,
        pcm: np.ndarray,
        sample_rate: int,
    ) -> None:
        """Append ``pcm`` to the ring after resampling to 16 kHz mono.

        Accepts ``int16`` mono or stereo (downmixed by mean) and
        ``float32`` mono in ``[-1, 1]``. Other dtypes raise.

        Args:
            pcm: PCM samples as delivered by the TTS engine. Mono or
                stereo. Shape ``(N,)`` or ``(N, C)``.
            sample_rate: Source rate of ``pcm`` in Hz. Resampled to
                ``_TARGET_RATE`` (16 kHz) via polyphase FIR.

        Raises:
            ValueError: dtype not in ``{int16, float32}`` or
                ``sample_rate <= 0``.
        """
        if sample_rate <= 0:
            msg = f"sample_rate must be positive, got {sample_rate}"
            raise ValueError(msg)

        if pcm.size == 0:
            return

        if pcm.ndim == 2:
            # ``ndarray.mean`` returns float64 by default. Preserve
            # the source dtype so the dispatch below routes correctly:
            # int16 stereo → int16 mono, float32 stereo → float32 mono.
            if pcm.dtype == np.int16:
                mono = pcm.mean(axis=1).astype(np.int16)
            elif pcm.dtype == np.float32:
                mono = pcm.mean(axis=1).astype(np.float32)
            else:
                msg = f"pcm dtype must be int16 or float32, got {pcm.dtype}"
                raise ValueError(msg)
        elif pcm.ndim == 1:
            mono = pcm
        else:
            msg = f"pcm must be 1D or 2D, got ndim={pcm.ndim}"
            raise ValueError(msg)

        if mono.dtype == np.int16:
            int16_target = self._resample_int16(mono, sample_rate)
        elif mono.dtype == np.float32:
            int16_target = self._resample_float32(mono, sample_rate)
        else:
            msg = f"pcm dtype must be int16 or float32, got {mono.dtype}"
            raise ValueError(msg)

        with self._lock:
            self._append_locked(int16_target)

    def _resample_int16(
        self,
        mono: np.ndarray,
        source_rate: int,
    ) -> npt.NDArray[np.int16]:
        if source_rate == _TARGET_RATE:
            return mono.astype(np.int16, copy=False)
        # int16 → float32 in [-1, 1] for resample, then back to int16.
        f32 = (mono.astype(np.float32) / float(1 << 15)).astype(np.float32)
        resampled = self._polyphase_resample(f32, source_rate)
        clipped = np.clip(resampled * float(1 << 15), -(1 << 15), (1 << 15) - 1)
        return clipped.astype(np.int16)  # type: ignore[no-any-return]

    def _resample_float32(
        self,
        mono: np.ndarray,
        source_rate: int,
    ) -> npt.NDArray[np.int16]:
        f32 = mono if source_rate == _TARGET_RATE else self._polyphase_resample(mono, source_rate)
        clipped = np.clip(f32 * float(1 << 15), -(1 << 15), (1 << 15) - 1)
        return clipped.astype(np.int16)

    @staticmethod
    def _polyphase_resample(
        f32: np.ndarray,
        source_rate: int,
    ) -> npt.NDArray[np.float32]:
        """Resample a mono float32 [-1, 1] signal to ``_TARGET_RATE``."""
        from math import gcd  # noqa: PLC0415 — small, only on resample path

        from scipy.signal import resample_poly  # noqa: PLC0415 — heavy import deferred

        common = gcd(source_rate, _TARGET_RATE)
        up = _TARGET_RATE // common
        down = source_rate // common
        result = resample_poly(f32, up, down)
        return result.astype(np.float32, copy=False)  # type: ignore[no-any-return]

    def _append_locked(self, samples: npt.NDArray[np.int16]) -> None:
        """Copy ``samples`` into the ring at ``_write_head`` with wraparound."""
        n = samples.size
        if n == 0:
            return
        # Optimisation: when the incoming chunk is larger than the
        # ring itself we only need to keep the trailing ``capacity``
        # samples (older samples would be overwritten anyway).
        if n >= self._capacity:
            self._ring[:] = samples[-self._capacity :]
            self._write_head = 0
            self._filled = self._capacity
            return

        end = self._write_head + n
        if end <= self._capacity:
            self._ring[self._write_head : end] = samples
            self._write_head = end % self._capacity
        else:
            first_part = self._capacity - self._write_head
            self._ring[self._write_head :] = samples[:first_part]
            self._ring[: n - first_part] = samples[first_part:]
            self._write_head = n - first_part

        self._filled = min(self._filled + n, self._capacity)

    # ── Consumer (capture stage) ────────────────────────────────────────

    def get_aligned_window(self, n_samples: int) -> npt.NDArray[np.int16]:
        """Return the most recent ``n_samples`` of buffered render PCM.

        When fewer than ``n_samples`` have been fed (buffer not full
        yet), the result is zero-padded at the START so the most
        recent samples sit at the END (the position the AEC's
        adaptive filter expects). When the buffer is empty, returns
        all zeros — :class:`SpeexAecProcessor` short-circuits to
        passthrough on silent reference.

        Args:
            n_samples: Window length. Must be positive and ≤ the
                ring capacity.

        Raises:
            ValueError: If ``n_samples`` ≤ 0 or > ring capacity.
        """
        if n_samples <= 0:
            msg = f"n_samples must be positive, got {n_samples}"
            raise ValueError(msg)
        if n_samples > self._capacity:
            msg = (
                f"n_samples {n_samples} exceeds ring capacity "
                f"{self._capacity} — increase buffer_seconds"
            )
            raise ValueError(msg)

        with self._lock:
            available = min(self._filled, n_samples)
            if available == 0:
                return np.zeros(n_samples, dtype=np.int16)

            # The "most recent ``available`` samples" sit immediately
            # before ``_write_head`` (with possible wraparound).
            start = (self._write_head - available) % self._capacity
            if start + available <= self._capacity:
                tail = self._ring[start : start + available].copy()
            else:
                first_part = self._capacity - start
                tail = np.concatenate(
                    [
                        self._ring[start:],
                        self._ring[: available - first_part],
                    ],
                )

            if available == n_samples:
                return tail
            # Zero-pad at the start so recent samples align with the end.
            out = np.zeros(n_samples, dtype=np.int16)
            out[-available:] = tail
            return out

    # ── Diagnostics ─────────────────────────────────────────────────────

    @property
    def capacity_samples(self) -> int:
        """Ring capacity in samples at 16 kHz."""
        return self._capacity

    @property
    def filled_samples(self) -> int:
        """How many samples the ring holds (clamped at ``capacity``)."""
        with self._lock:
            return self._filled

    def reset(self) -> None:
        """Clear the ring (zero-fill + reset write head + filled count).

        Called when the audio path is invalidated (device change,
        explicit pipeline restart) so AEC doesn't operate on stale
        render PCM after a reset.
        """
        with self._lock:
            self._ring[:] = 0
            self._write_head = 0
            self._filled = 0


__all__ = [
    "RenderPcmBuffer",
]
