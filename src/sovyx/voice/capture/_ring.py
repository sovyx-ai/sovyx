"""Ring-buffer access — :class:`RingMixin`.

Extracted from ``voice/_capture_task.py`` (lines 1590-1721 + 1663-1720
pre-split) per master mission Phase 1 / T1.4 step 7. Builds on the
``EpochMixin`` from step 6 — same composition pattern, larger
surface (4 methods vs 1).

The mixin owns:

* :meth:`_allocate_ring_buffer` — allocate / resize the 16 kHz mono
  int16 ring; bumps the epoch on every (re)allocation so an in-flight
  :meth:`tap_frames_since_mark` can detect the ring was reset and
  short-circuit.
* :meth:`_ring_write` — synchronous append-with-wrap, called
  between awaits in ``LoopMixin._consume_loop``.
* :meth:`tap_recent_frames` — async public API; returns a fresh
  copy of the most recent ``duration_s`` of audio.
* :meth:`tap_frames_since_mark` — async public API; polls the ring
  state until ``min_samples`` new frames accumulate post-``mark``
  or ``max_wait_s`` elapses (probe-window-contamination fix per
  v1.3 §4.2 L4-B).

Mixin contract — the host class (``AudioCaptureTask``) initialises
the ring-buffer state in ``__init__``:

* ``self._ring_buffer: np.ndarray | None`` — int16 buffer, ``None``
  before :meth:`_allocate_ring_buffer` runs.
* ``self._ring_capacity: int`` — sample count.
* ``self._ring_write_index: int`` — wrap pointer.
* ``self._ring_state: int`` — packed ``(epoch, samples_written)``
  shared with :class:`~sovyx.voice.capture._epoch.EpochMixin`.
* ``self._tuning: VoiceTuningConfig | None`` — optional override
  for :attr:`mark_tap_poll_interval_s`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.voice.capture._constants import (
    _RING_EPOCH_SHIFT,
    _RING_SAMPLES_MASK,
    _SAMPLE_RATE,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig


__all__ = ["RingMixin"]


class RingMixin:
    """Ring-buffer write + tap helpers (host-supplied state).

    See module docstring for the host-class attribute contract. All
    four methods read / mutate ``self._ring_*`` directly via Python's
    MRO attribute resolution; the mixin pattern keeps ring-buffer
    semantics in one file without forcing the host class to inline
    ~200 LOC of buffer logic.
    """

    # Host-class state declarations for mypy strict.
    _ring_buffer: np.ndarray | None
    _ring_capacity: int
    _ring_write_index: int
    _ring_state: int
    _tuning: VoiceTuningConfig | None

    def _allocate_ring_buffer(self, tuning: VoiceTuningConfig) -> None:
        """Allocate (or resize) the 16 kHz-mono int16 ring buffer.

        Called from :meth:`start` and from the two reopen paths
        (:meth:`request_exclusive_restart`, :meth:`request_shared_restart`,
        :meth:`_reopen_stream_after_device_error`) so a reopen never
        leaks stale frames from the pre-reopen stream. The write pointer
        is reset to zero so ``tap_recent_frames`` after the reopen only
        returns audio from the fresh stream.

        v1.3 §4.2 L4-B — the epoch component of ``_ring_state`` bumps on
        every allocation so an in-flight :meth:`tap_frames_since_mark`
        can detect "the ring was reset while I was waiting" via epoch
        inequality and avoid waiting forever for a sample count the new
        ring will never reach.
        """
        import numpy as np

        seconds = max(0.0, float(tuning.capture_ring_buffer_seconds))
        capacity = max(1, int(seconds * _SAMPLE_RATE))
        self._ring_buffer = np.zeros(capacity, dtype=np.int16)
        self._ring_capacity = capacity
        self._ring_write_index = 0
        # Bump epoch, reset samples. Single atomic assignment so any
        # concurrent reader observing ``_ring_state`` sees the new
        # (epoch, 0) pair consistently — never an old-epoch/new-samples
        # or new-epoch/old-samples interleaving.
        current_epoch = self._ring_state >> _RING_EPOCH_SHIFT
        self._ring_state = (current_epoch + 1) << _RING_EPOCH_SHIFT

    def _ring_write(self, window: npt.NDArray[np.int16]) -> None:
        """Append a pipeline-shaped frame (16 kHz mono int16) to the ring.

        Synchronous by design: runs between ``await`` points inside
        :meth:`_consume_loop` so no lock is required against
        :meth:`tap_recent_frames` (which is also synchronous between
        its own awaits). Silent no-op when :meth:`_allocate_ring_buffer`
        hasn't run yet — keeps test harnesses that drive ``feed_frame``
        without starting the task alive.
        """
        buf = self._ring_buffer
        if buf is None:
            return
        cap = self._ring_capacity
        n = int(window.shape[0])
        if n <= 0 or cap <= 0:
            return
        # v1.3 §4.2 — compute the post-write state once and commit via
        # a single ``_ring_state = ...`` assignment so cross-loop readers
        # never observe a half-updated pair. The samples component wraps
        # at ``_RING_SAMPLES_MASK`` (effectively never, at 16 kHz); the
        # epoch is preserved by masking the low bits.
        state = self._ring_state
        epoch_bits = state & ~_RING_SAMPLES_MASK
        new_samples = ((state & _RING_SAMPLES_MASK) + n) & _RING_SAMPLES_MASK
        # If a single window is larger than the buffer (pathological —
        # 33 s default holds ~1_032 blocks of 16 ms), keep only the tail.
        if n >= cap:
            buf[:] = window[-cap:]
            self._ring_write_index = 0
            self._ring_state = epoch_bits | new_samples
            return
        start = self._ring_write_index
        end = start + n
        if end <= cap:
            buf[start:end] = window
        else:
            head = cap - start
            buf[start:cap] = window[:head]
            buf[0 : n - head] = window[head:]
        self._ring_write_index = (start + n) % cap
        self._ring_state = epoch_bits | new_samples

    async def tap_recent_frames(
        self,
        duration_s: float,
    ) -> npt.NDArray[np.int16]:
        """Return the most recent ``duration_s`` seconds of 16 kHz mono int16.

        The returned array is always a fresh copy — callers can hold on
        to it after subsequent writes invalidate the ring slot. When
        fewer frames than requested have been written (cold start, early
        bypass attempt) the slice is truncated to what's actually
        available; callers inspect ``.shape[0]`` to decide whether the
        sample is large enough for their analysis.

        Thread-safety: see ``__init__`` docstring — reads happen
        synchronously against writes that also run between awaits on the
        same event loop, so no lock is required. The async signature is
        kept for future-proofing (Protocol contract + possible move to
        an off-loop ring implementation).

        Args:
            duration_s: Requested snapshot duration in seconds. Clamped
                to ``[0, capture_ring_buffer_seconds]``.

        Returns:
            An ``(N,)`` int16 array, ``N == int(duration_s * 16_000)``
            at most, possibly shorter when the ring is not yet full.
        """
        import numpy as np

        buf = self._ring_buffer
        cap = self._ring_capacity
        if buf is None or cap <= 0 or duration_s <= 0:
            return np.zeros(0, dtype=np.int16)
        wanted = min(cap, int(duration_s * _SAMPLE_RATE))
        # v1.3 §4.2 — derive samples_written from the packed ``_ring_state``
        # so reads and writes share a single source of truth.
        available = min(self._ring_state & _RING_SAMPLES_MASK, cap)
        n = min(wanted, available)
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        end = self._ring_write_index
        begin = (end - n) % cap
        if begin + n <= cap:
            return buf[begin : begin + n].copy()
        # Wrap — copy two slices into a fresh contiguous array.
        head = cap - begin
        out = np.empty(n, dtype=np.int16)
        out[:head] = buf[begin:cap]
        out[head:] = buf[0 : n - head]
        return out

    async def tap_frames_since_mark(
        self,
        mark: tuple[int, int],
        min_samples: int,
        max_wait_s: float,
    ) -> npt.NDArray[np.int16]:
        """Return frames written AFTER ``mark`` was captured.

        See :class:`~sovyx.voice.health.contract.CaptureTaskProto` for
        the full contract. Implementation notes:

        * ``_ring_state`` is read in one ``LOAD_ATTR`` per loop iteration
          so epoch and samples count always correspond to the same state
          generation.
        * If the epoch bundled in ``mark`` no longer matches the current
          epoch, the ring was reallocated (a stream reopen / exclusive
          restart): every sample currently in the buffer is by
          definition post-mark, and we short-circuit with the available
          tail rather than spinning for a delta that will never
          materialise.
        * The poll interval comes from
          :attr:`VoiceTuningConfig.mark_tap_poll_interval_s` (§14.E4)
          so operators can tune responsiveness without editing code.
        """
        import numpy as np

        mark_epoch, mark_samples = mark
        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        poll_interval_s = max(0.001, float(tuning.mark_tap_poll_interval_s))
        deadline = time.monotonic() + max(0.0, float(max_wait_s))

        while True:
            state = self._ring_state  # atomic LOAD_ATTR per iteration
            current_epoch = state >> _RING_EPOCH_SHIFT
            current_samples = state & _RING_SAMPLES_MASK

            if current_epoch != mark_epoch:
                # Ring was reallocated after the mark was taken — every
                # sample now in the buffer is post-reset, so treat the
                # entire capacity as post-mark.
                available = min(current_samples, self._ring_capacity)
                if available >= min_samples or time.monotonic() >= deadline:
                    if available <= 0:
                        return np.zeros(0, dtype=np.int16)
                    return await self.tap_recent_frames(
                        min(available, min_samples) / _SAMPLE_RATE,
                    )
            else:
                new_samples = current_samples - mark_samples
                if new_samples >= min_samples:
                    return await self.tap_recent_frames(min_samples / _SAMPLE_RATE)
                if time.monotonic() >= deadline:
                    if new_samples <= 0:
                        return np.zeros(0, dtype=np.int16)
                    return await self.tap_recent_frames(new_samples / _SAMPLE_RATE)

            await asyncio.sleep(poll_interval_s)
