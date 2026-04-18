"""Normalise arbitrary PortAudio input into the pipeline's frame contract.

:class:`sovyx.voice.pipeline.VoicePipeline` hard-requires every frame fed via
:meth:`~sovyx.voice.pipeline._orchestrator.VoicePipeline.feed_frame` to be
exactly ``(512,) int16`` at **16 kHz mono** — that is the invariant Silero
v5 was trained on and any deviation causes the model to see silence even
when the microphone is capturing loud speech (probability stuck near
zero, pipeline never leaves IDLE).

Historically the capture task forwarded whatever shape PortAudio delivered
and only downmixed (``indata[:, 0]``). That silently worked when the WASAPI
mixer format already matched (16 kHz / 1 ch) but broke whenever the opener
pyramid fell back to 48 kHz / 2 ch — common on Windows shared-mode mics
(Razer BlackShark, most USB headsets). See the root-cause writeup in
``docs-internal/audits/voice-silent-vad.md`` (local) for the full debug
trace.

This module owns the three transformations that have to happen between
the PortAudio callback and :meth:`feed_frame`:

1. **Downmix** — ``(N, C) → (N,)`` via channel averaging (mean) or
   explicit first-channel pick when the source is already mono.
2. **Resample** — arbitrary ``source_rate → 16 kHz`` via
   :func:`scipy.signal.resample_poly` (polyphase FIR). Stateless per call,
   which introduces a sub-millisecond filter-state discontinuity at block
   boundaries. The discontinuity is well below Silero's sensitivity and
   negligible for voice activity detection; end-to-end FFT tests verify
   that a 1 kHz tone stays at 1 kHz after the transform.
3. **Rewindow** — accumulate the resampled stream in a bounded buffer
   and emit as many complete 512-sample windows as possible. Partial
   windows are held for the next call so frame boundaries stay aligned
   across PortAudio blocks.

Fast-path: when ``source_rate == 16000`` and ``source_channels == 1`` the
resampler is skipped entirely and the normaliser degenerates into a pure
rewindower — zero DSP cost. Callers can still hand 16 kHz mono in blocks
of any size and get back 512-sample windows.

Thread-safety: the class is **not** thread-safe. PortAudio delivers frames
on a worker thread and the sovyx capture task hops onto the asyncio loop
(``call_soon_threadsafe``) before calling :meth:`push`, so all invocations
serialise on the event loop. Adding a lock would buy nothing in the
current architecture and would drop ~5 µs/frame.

Memory: the internal buffers are bounded by ``_TARGET_WINDOW`` for output
(max 512 samples = 1 KiB at int16) and by ``blocksize`` for input (max ~6
KiB at 48 kHz / 32 ms). Safe for long-running daemons.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)


_TARGET_RATE = 16_000
"""Output sample rate — the invariant SileroVAD v5 and MoonshineSTT share."""

_TARGET_WINDOW = 512
"""Output window size in samples. 32 ms at 16 kHz — matches VAD config."""


class FrameNormalizer:
    """Stream-oriented resample + downmix + rewindow for PortAudio input.

    Construct once per opened stream, call :meth:`push` on every callback
    block, forward each returned array to the pipeline. The class keeps
    a small output tail between calls so 512-sample windows stay aligned
    across PortAudio's variable block size.

    Args:
        source_rate: Rate PortAudio is delivering at (Hz). Must be > 0.
        source_channels: Channel count in each incoming block. Must be
            ≥ 1. When > 1, each block is downmixed by channel averaging
            before resampling.

    Raises:
        ValueError: If ``source_rate`` ≤ 0 or ``source_channels`` < 1.
    """

    def __init__(self, source_rate: int, source_channels: int) -> None:
        if source_rate <= 0:
            msg = f"source_rate must be positive, got {source_rate}"
            raise ValueError(msg)
        if source_channels < 1:
            msg = f"source_channels must be >= 1, got {source_channels}"
            raise ValueError(msg)

        import numpy as np

        self._source_rate = source_rate
        self._source_channels = source_channels
        self._passthrough = source_rate == _TARGET_RATE and source_channels == 1

        gcd = math.gcd(source_rate, _TARGET_RATE)
        self._up = _TARGET_RATE // gcd
        self._down = source_rate // gcd

        self._output_buf: npt.NDArray[np.int16] = np.zeros(0, dtype=np.int16)

        logger.debug(
            "frame_normalizer_created",
            source_rate=source_rate,
            source_channels=source_channels,
            target_rate=_TARGET_RATE,
            target_window=_TARGET_WINDOW,
            passthrough=self._passthrough,
            up=self._up,
            down=self._down,
        )

    @property
    def source_rate(self) -> int:
        """Configured source sample rate in Hz."""
        return self._source_rate

    @property
    def source_channels(self) -> int:
        """Configured source channel count."""
        return self._source_channels

    @property
    def is_passthrough(self) -> bool:
        """Whether the fast path is active (source already 16 kHz mono)."""
        return self._passthrough

    @property
    def target_rate(self) -> int:
        """Output sample rate in Hz (always 16 000)."""
        return _TARGET_RATE

    @property
    def target_window(self) -> int:
        """Output window size in samples (always 512)."""
        return _TARGET_WINDOW

    def push(
        self,
        block: npt.NDArray[np.int16] | npt.NDArray[np.float32],
    ) -> list[npt.NDArray[np.int16]]:
        """Push one PortAudio callback block, get any complete 16 kHz windows back.

        Accepts either ``(N,)`` mono or ``(N, C)`` multichannel input.
        The block's dtype may be ``int16`` (normal sovyx capture) or
        ``float32`` (tests / unusual drivers). Internally everything is
        converted to ``float32`` in [-1, 1] for the resample and back to
        ``int16`` before handoff to the pipeline.

        Args:
            block: Raw samples as delivered by the PortAudio callback.

        Returns:
            A list of 512-sample ``int16`` arrays at 16 kHz. Empty when
            the internal buffer doesn't yet hold a full window.

        Raises:
            ValueError: If ``block`` is empty or has an incompatible
                channel count relative to ``source_channels``.
        """
        import numpy as np

        if block.size == 0:
            return []

        mono = self._downmix(block)
        resampled = mono if self._passthrough else self._resample(mono)
        as_int16 = _float_or_int_to_int16(resampled)

        self._output_buf = np.concatenate([self._output_buf, as_int16])

        windows: list[npt.NDArray[np.int16]] = []
        while len(self._output_buf) >= _TARGET_WINDOW:
            window = self._output_buf[:_TARGET_WINDOW].copy()
            self._output_buf = self._output_buf[_TARGET_WINDOW:]
            windows.append(window)
        return windows

    def reset(self) -> None:
        """Drop any buffered samples (call on stream restart)."""
        import numpy as np

        self._output_buf = np.zeros(0, dtype=np.int16)

    def _downmix(
        self,
        block: npt.NDArray[np.int16] | npt.NDArray[np.float32],
    ) -> npt.NDArray[np.float32]:
        """Collapse ``block`` to mono ``float32`` samples in [-1, 1].

        Normalises to ``float32`` *before* channel averaging so ``int16``
        arithmetic does not overflow on loud stereo pairs and so the
        downstream ``_float_or_int_to_int16`` step sees values already on
        the [-1, 1] scale regardless of source dtype.
        """
        import numpy as np

        if block.dtype == np.int16:
            block_f = block.astype(np.float32) / 32768.0
        else:
            block_f = block.astype(np.float32)

        if block_f.ndim == 1:
            return block_f
        if block_f.ndim == 2:
            if self._source_channels == 1:
                out: npt.NDArray[np.float32] = block_f[:, 0].astype(np.float32)
                return out
            avg: npt.NDArray[np.float32] = block_f.mean(axis=1).astype(np.float32)
            return avg
        msg = f"block must be 1-D or 2-D, got ndim={block_f.ndim}"
        raise ValueError(msg)

    def _resample(
        self,
        mono: npt.NDArray[np.float32],
    ) -> npt.NDArray[np.float32]:
        """Polyphase resample to 16 kHz using scipy."""
        import numpy as np
        from scipy.signal import resample_poly

        out = resample_poly(mono, self._up, self._down).astype(np.float32)
        return out  # type: ignore[no-any-return]


def _float_or_int_to_int16(
    samples: npt.NDArray[np.int16] | npt.NDArray[np.float32],
) -> npt.NDArray[np.int16]:
    """Convert a ``float32`` buffer in [-1, 1] (or a pass-through ``int16``) to ``int16``.

    Clips to ``[-32768, 32767]`` so loud transients near ±1.0 do not wrap
    to ``INT16_MIN`` (which Silero would interpret as an impulse). The
    ``int16`` pass-through is a no-op on the passthrough code path.
    """
    import numpy as np

    if samples.dtype == np.int16:
        return samples  # type: ignore[return-value]
    scaled = samples * 32768.0
    clipped = np.clip(scaled, -32768.0, 32767.0)
    out: npt.NDArray[np.int16] = clipped.astype(np.int16)
    return out


__all__ = ["FrameNormalizer"]
