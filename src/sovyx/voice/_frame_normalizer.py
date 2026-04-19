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

This module owns the four transformations that have to happen between
the PortAudio callback and :meth:`feed_frame`:

1. **Format normalise** — the VCHL cascade (``docs-internal/ADR-voice-
   capture-health-lifecycle.md`` §5.1) can negotiate ``int16`` / ``int24`` /
   ``float32`` capture. PortAudio delivers ``int24`` inside ``int32`` numpy
   arrays with the 24-bit payload sign-extended, so it scales with
   ``2**23`` (= 8 388 608), not ``2**31``. ``float32`` is already in
   [-1, 1]. All three collapse into the common ``float32 [-1, 1]``
   representation before any DSP runs.
2. **Downmix** — ``(N, C) → (N,)`` via channel averaging (mean) or
   explicit first-channel pick when the source is already mono.
3. **Resample** — arbitrary ``source_rate → 16 kHz`` via
   :func:`scipy.signal.resample_poly` (polyphase FIR). Stateless per call,
   which introduces a sub-millisecond filter-state discontinuity at block
   boundaries. The discontinuity is well below Silero's sensitivity and
   negligible for voice activity detection; end-to-end FFT tests verify
   that a 1 kHz tone stays at 1 kHz after the transform.
4. **Ducking gain (optional)** — §4.4.6.b of the ADR: while TTS is
   playing, attenuate the mic by ``-18 dB`` so residual bleed cannot
   retrigger the wake word / VAD. Applied in the resampled ``float32``
   domain with a short linear ramp (default ``10 ms`` at 16 kHz = 160
   samples) so step changes never click. Ramp length is well below the
   50 ms "gain removed within 50 ms of TTS-end" requirement in the ADR.
5. **Rewindow** — accumulate the resampled stream in a bounded buffer
   and emit as many complete 512-sample windows as possible. Partial
   windows are held for the next call so frame boundaries stay aligned
   across PortAudio blocks.

Fast-path: when ``source_rate == 16000`` and ``source_channels == 1`` the
resampler is skipped entirely and the normaliser degenerates into a pure
rewindower — zero DSP cost. Callers can still hand 16 kHz mono in blocks
of any size and get back 512-sample windows. The ducking stage stays a
multiply-by-1 no-op when the target gain is unity, so passthrough of
already-16-kHz ``int16`` mono remains bit-exact end-to-end.

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
import typing
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

_SUPPORTED_FORMATS = frozenset({"int16", "int24", "float32"})
"""Source formats the cascade can negotiate (ADR §5.1)."""

_INT24_SCALE = float(1 << 23)
"""PortAudio int24 sign-extended into int32 scales by 2**23, not 2**31."""

_INT16_SCALE = float(1 << 15)
"""int16 full-scale divisor."""

_DEFAULT_DUCKING_RAMP_MS = 10.0
"""Default ramp duration for mic-ducking step changes (ms).

10 ms at 16 kHz = 160 samples. Well under the ADR §4.4.6.b "gain removed
within 50 ms of TTS-end" requirement and short enough to stay inaudible
on voice content while long enough to avoid click artefacts from sudden
multiplicative steps.
"""


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
        source_format: Sample format the cascade negotiated. Must be one
            of ``"int16"`` (default — numpy ``int16`` blocks), ``"int24"``
            (numpy ``int32`` blocks with 24-bit sign-extended payload), or
            ``"float32"`` (numpy ``float32`` blocks in ``[-1, 1]``).
        ducking_ramp_ms: Duration of the linear ramp used when
            :meth:`set_ducking_gain_db` changes the target gain. Default
            10 ms, which is well under the ADR's 50 ms release bound.

    Raises:
        ValueError: If ``source_rate`` ≤ 0, ``source_channels`` < 1,
            ``source_format`` is not one of the supported strings, or
            ``ducking_ramp_ms`` ≤ 0.
    """

    def __init__(
        self,
        source_rate: int,
        source_channels: int,
        *,
        source_format: str = "int16",
        ducking_ramp_ms: float = _DEFAULT_DUCKING_RAMP_MS,
    ) -> None:
        if source_rate <= 0:
            msg = f"source_rate must be positive, got {source_rate}"
            raise ValueError(msg)
        if source_channels < 1:
            msg = f"source_channels must be >= 1, got {source_channels}"
            raise ValueError(msg)
        if source_format not in _SUPPORTED_FORMATS:
            msg = (
                f"source_format must be one of {sorted(_SUPPORTED_FORMATS)}, got {source_format!r}"
            )
            raise ValueError(msg)
        if ducking_ramp_ms <= 0:
            msg = f"ducking_ramp_ms must be positive, got {ducking_ramp_ms}"
            raise ValueError(msg)

        import numpy as np

        self._source_rate = source_rate
        self._source_channels = source_channels
        self._source_format = source_format
        self._passthrough = source_rate == _TARGET_RATE and source_channels == 1

        gcd = math.gcd(source_rate, _TARGET_RATE)
        self._up = _TARGET_RATE // gcd
        self._down = source_rate // gcd

        self._output_buf: npt.NDArray[np.int16] = np.zeros(0, dtype=np.int16)

        self._ducking_ramp_ms = ducking_ramp_ms
        self._ducking_ramp_samples = max(
            1,
            int(round(_TARGET_RATE * ducking_ramp_ms / 1000.0)),
        )
        self._current_linear_gain: float = 1.0
        self._target_linear_gain: float = 1.0

        logger.debug(
            "frame_normalizer_created",
            source_rate=source_rate,
            source_channels=source_channels,
            source_format=source_format,
            target_rate=_TARGET_RATE,
            target_window=_TARGET_WINDOW,
            passthrough=self._passthrough,
            up=self._up,
            down=self._down,
            ducking_ramp_samples=self._ducking_ramp_samples,
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
    def source_format(self) -> str:
        """Configured source sample format (int16 / int24 / float32)."""
        return self._source_format

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

    @property
    def ducking_gain_db(self) -> float:
        """Target mic-ducking gain in dB. ``0.0`` means no attenuation.

        Returns the *target* set by :meth:`set_ducking_gain_db`, not the
        instantaneous value mid-ramp. Use :attr:`current_ducking_gain_db`
        for the instantaneous value.
        """
        return _linear_to_db(self._target_linear_gain)

    @property
    def current_ducking_gain_db(self) -> float:
        """Instantaneous mic-ducking gain in dB (may differ from target mid-ramp)."""
        return _linear_to_db(self._current_linear_gain)

    def set_ducking_gain_db(self, gain_db: float) -> None:
        """Set the target mic-ducking gain (dB attenuation, ≤ 0).

        Per ADR §4.4.6.b, a ``-18 dB`` cut during TTS playback is the
        standard ducking level. Setting ``0 dB`` restores unity. Changes
        are transitioned linearly over ``ducking_ramp_ms`` to avoid
        clicks at the step edge.

        Setting the same value twice is a cheap no-op — it does NOT
        reset an in-progress ramp.

        Args:
            gain_db: Target gain in dB. Must be ``≤ 0`` (the stage is
                an *attenuator*, never an amplifier). ``float('-inf')``
                is accepted and collapses to linear gain ``0.0``.

        Raises:
            ValueError: If ``gain_db > 0``.
        """
        if gain_db > 0.0:
            msg = f"ducking gain must be <= 0 dB (attenuation only), got {gain_db}"
            raise ValueError(msg)

        target_linear = _db_to_linear(gain_db)
        if math.isclose(
            target_linear,
            self._target_linear_gain,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            return
        self._target_linear_gain = target_linear
        logger.debug(
            "frame_normalizer_ducking_target",
            gain_db=gain_db,
            target_linear=target_linear,
            current_linear=self._current_linear_gain,
        )

    def push(
        self,
        block: (npt.NDArray[np.int16] | npt.NDArray[np.int32] | npt.NDArray[np.float32]),
    ) -> list[npt.NDArray[np.int16]]:
        """Push one PortAudio callback block, get any complete 16 kHz windows back.

        Accepts either ``(N,)`` mono or ``(N, C)`` multichannel input.
        The block's dtype is validated against ``source_format``:

        - ``int16``: numpy ``int16`` (or ``float32`` in ``[-1, 1]`` for
          tests / loopback drivers).
        - ``int24``: numpy ``int32`` with 24-bit sign-extended payload.
        - ``float32``: numpy ``float32`` in ``[-1, 1]``.

        Internally everything is converted to ``float32`` in [-1, 1] for
        the resample / ducking stages, then back to ``int16`` before
        handoff to the pipeline (saturation clip per ADR §5.1).

        Args:
            block: Raw samples as delivered by the PortAudio callback.

        Returns:
            A list of 512-sample ``int16`` arrays at 16 kHz. Empty when
            the internal buffer doesn't yet hold a full window.

        Raises:
            ValueError: If ``block`` has an incompatible channel count
                or dtype relative to ``source_format``, or a rank other
                than 1 or 2.
        """
        import numpy as np

        if block.size == 0:
            return []

        # Ultra-fast path: int16 mono 16 kHz with unity ducking. This
        # is the dominant case once the cascade settles on the invariant
        # format on good hardware. Skips the float32 round-trip so the
        # capture→VAD path is a memcpy.
        if (
            self._passthrough
            and self._source_format == "int16"
            and block.dtype == np.int16
            and self._target_linear_gain == 1.0
            and self._current_linear_gain == 1.0
        ):
            as_int16 = block if block.ndim == 1 else block[:, 0].copy()
        else:
            mono_f32 = self._downmix(block)
            resampled = mono_f32 if self._passthrough else self._resample(mono_f32)
            ducked = self._apply_ducking(resampled)
            as_int16 = _float_to_int16_saturate(ducked)

        self._output_buf = np.concatenate([self._output_buf, as_int16])

        windows: list[npt.NDArray[np.int16]] = []
        while len(self._output_buf) >= _TARGET_WINDOW:
            window = self._output_buf[:_TARGET_WINDOW].copy()
            self._output_buf = self._output_buf[_TARGET_WINDOW:]
            windows.append(window)
        return windows

    def reset(self) -> None:
        """Drop any buffered samples and collapse ducking ramp to target.

        Called on stream restart. The ducking *target* stays at whatever
        the caller last set, but the ramp state snaps to that target so
        the next block does not start at a stale mid-ramp gain.
        """
        import numpy as np

        self._output_buf = np.zeros(0, dtype=np.int16)
        self._current_linear_gain = self._target_linear_gain

    def _downmix(
        self,
        block: (npt.NDArray[np.int16] | npt.NDArray[np.int32] | npt.NDArray[np.float32]),
    ) -> npt.NDArray[np.float32]:
        """Collapse ``block`` to mono ``float32`` samples in [-1, 1].

        Normalises to ``float32`` *before* channel averaging so integer
        arithmetic does not overflow on loud stereo pairs and so the
        downstream ``_float_to_int16_saturate`` step sees values already
        on the [-1, 1] scale regardless of source dtype.

        The scale factor comes from ``source_format``:

        - ``int16``: ``2**15 = 32 768``
        - ``int24`` (int32 payload): ``2**23 = 8 388 608``
        - ``float32``: identity (no scaling)
        """
        import numpy as np

        block_f: npt.NDArray[np.float32]
        if self._source_format == "int24":
            if block.dtype != np.int32:
                msg = (
                    f"int24 source requires numpy int32 blocks "
                    f"(sign-extended 24-bit payload), got dtype={block.dtype}"
                )
                raise ValueError(msg)
            block_f = (block.astype(np.float32) / _INT24_SCALE).astype(np.float32)
        elif self._source_format == "float32":
            # Cast narrows the numpy-stubs union (int16 | int32 | float32)
            # back to the NDArray[float32] we already know astype produces.
            block_f = typing.cast(
                "npt.NDArray[np.float32]",
                np.asarray(block, dtype=np.float32),
            )
        else:  # int16
            if block.dtype == np.int16:
                block_f = (block.astype(np.float32) / _INT16_SCALE).astype(np.float32)
            elif block.dtype == np.float32:
                # Tolerated for test-suite / loopback callers that
                # already hand [-1, 1] float32 against an int16-declared
                # source. Treated as already on the target scale.
                block_f = typing.cast("npt.NDArray[np.float32]", block)
            else:
                msg = (
                    f"int16 source expects numpy int16 (or float32 in [-1, 1]) "
                    f"blocks, got dtype={block.dtype}"
                )
                raise ValueError(msg)

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
        from scipy.signal import resample_poly

        out = resample_poly(mono, self._up, self._down).astype("float32")
        return out  # type: ignore[no-any-return]

    def _apply_ducking(
        self,
        samples: npt.NDArray[np.float32],
    ) -> npt.NDArray[np.float32]:
        """Apply mic-ducking gain with a linear ramp toward the target.

        No-op when both ``current`` and ``target`` are unity (the common
        case: TTS not playing). When the caller has set a non-unity
        target, the ramp proceeds at a fixed step of
        ``(target - current) / ducking_ramp_samples`` per output sample
        until ``current`` reaches ``target``; then the gain stays flat.

        Returns a fresh array. Input is not mutated.
        """
        import numpy as np

        n = len(samples)
        if n == 0:
            return samples

        if self._current_linear_gain == self._target_linear_gain:
            if self._current_linear_gain == 1.0:
                return samples
            return samples * np.float32(self._current_linear_gain)

        step = (self._target_linear_gain - self._current_linear_gain) / self._ducking_ramp_samples
        indices = np.arange(1, n + 1, dtype=np.float32)
        envelope = np.float32(self._current_linear_gain) + np.float32(step) * indices

        low = min(self._current_linear_gain, self._target_linear_gain)
        high = max(self._current_linear_gain, self._target_linear_gain)
        envelope = np.clip(envelope, low, high)

        self._current_linear_gain = float(envelope[-1])
        ducked: npt.NDArray[np.float32] = (samples * envelope).astype(np.float32, copy=False)
        return ducked


def _float_to_int16_saturate(
    samples: npt.NDArray[np.float32],
) -> npt.NDArray[np.int16]:
    """Convert ``float32`` in [-1, 1] to ``int16`` with saturation clip.

    Per ADR §5.1 ("int24/float32 → int16 via saturation clip"). Loud
    transients near ±1.0 would otherwise wrap to ``INT16_MIN`` (which
    Silero reads as a non-physical impulse).
    """
    import numpy as np

    scaled = samples * 32768.0
    clipped = np.clip(scaled, -32768.0, 32767.0)
    out: npt.NDArray[np.int16] = clipped.astype(np.int16)
    return out


def _db_to_linear(db: float) -> float:
    """Convert dB to linear gain. ``-inf dB → 0.0``. ``0 dB → 1.0``."""
    if db == float("-inf"):
        return 0.0
    return float(10.0 ** (db / 20.0))


def _linear_to_db(linear: float) -> float:
    """Convert linear gain to dB. ``0.0 → -inf``. ``1.0 → 0.0``."""
    if linear <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(linear)


__all__ = ["FrameNormalizer"]
