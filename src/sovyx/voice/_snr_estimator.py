"""Per-frame SNR estimator (Phase 4 / T4.31).

Estimates the speech-vs-noise ratio of each capture frame using
the spectral-subtraction formula from the master mission §Phase 4
/ T4.31:

    SNR_est = 10 · log10((|S_speech|² - |S_noise|²) / |S_noise|²)

Where:

* ``|S_speech|²`` = current frame's mean-square power (raw signal
  energy — speech + noise + room tone).
* ``|S_noise|²`` = noise floor estimate, tracked as the rolling
  *minimum* mean-square power across the last
  :attr:`SnrEstimatorConfig.noise_window_seconds` of frames.

The minimum-tracker is the canonical noise-estimation approach
when no separate VAD is available — it assumes that within any
~5-second window at least one frame contains genuine background
silence. The floor adapts to the current room as the user moves,
HVAC changes, etc.

Foundation phase scope (T4.31, this commit):

* :class:`SnrEstimatorConfig` — tuning snapshot.
* :class:`SnrEstimator` — stateful per-frame estimator.
* :func:`estimate_frame_power` — pure-DSP helper used by tests
  + the future T4.33 ``voice.audio.snr_db`` histogram emitter.

Out of scope (later commits per ``feedback_staged_adoption``):

* T4.32 — wire into VAD path so each VAD-positive frame emits SNR.
* T4.33 — ``voice.audio.snr_db`` histogram metric.
* T4.34 — per-session p50/p95 in the heartbeat event.
* T4.35 — alert when SNR p50 < 9 dB (Moonshine degradation
  threshold — flagged in the master mission §Phase 4 quality gate
  table).
* T4.36 — SNR-aware STT confidence gating.
* T4.37 — dashboard SNR distribution panel.
* T4.38 — noise-floor trending alert.
* T4.39 — per-frame SNR threshold for VAD hallucination guard.
* T4.40 — operator documentation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_INT16_FULL_SCALE_SQ = float((1 << 15) ** 2)
"""Mean-square value of a full-scale int16 frame (32 768²)."""

_DEFAULT_FRAME_SIZE_SAMPLES = 512
"""Matches the FrameNormalizer's _TARGET_WINDOW invariant."""

_DEFAULT_SAMPLE_RATE = 16_000
"""Output sample rate of the FrameNormalizer."""

_SNR_CEILING_DB = 120.0
"""Cap returned SNR so a near-zero noise floor doesn't blow charts."""

_SNR_FLOOR_DB = -120.0
"""Floor returned when both signal and noise are below detection."""


@dataclass(frozen=True, slots=True)
class SnrEstimatorConfig:
    """Tuning snapshot for the SNR estimator.

    Constructed once per pipeline lifetime; not mutated at runtime.
    Operators rebuild the estimator after a config reload.
    """

    enabled: bool
    sample_rate: int
    frame_size_samples: int
    noise_window_seconds: float
    silence_floor_db: float

    @property
    def noise_window_frames(self) -> int:
        """Ring length in frames at the configured sample rate.

        ``noise_window_seconds * sample_rate / frame_size_samples``.
        Floored at 1 to avoid a degenerate zero-length ring.
        """
        frames_per_second = self.sample_rate / self.frame_size_samples
        return max(1, int(round(self.noise_window_seconds * frames_per_second)))

    @property
    def silence_floor_linear_sq(self) -> float:
        """Linear-domain mean-square power threshold for "silent" frames.

        Frames whose mean-square power sits below this threshold
        are skipped when updating the noise estimate AND the SNR
        computation returns :data:`_SNR_FLOOR_DB`.

        Conversion from amplitude dBFS to mean-square power:

            amplitude_threshold = full_scale · 10^(db/20)
            power_threshold     = amplitude_threshold²
                                = full_scale² · 10^(db·2/20)
                                = full_scale² · 10^(db/10)

        So a ``-90 dBFS`` flag maps to ``32 768² · 10^(-9) ≈ 1.07
        LSB²`` (the canonical "below 16-bit detection" floor).
        """
        return _INT16_FULL_SCALE_SQ * float(10.0 ** (self.silence_floor_db / 10.0))


def estimate_frame_power(frame: np.ndarray) -> float:
    """Return the mean-square power of an int16 frame.

    Used by both :class:`SnrEstimator` and tests to compute the
    "speech" side of the spectral-subtraction formula. The function
    accepts only int16 PCM — float frames are rejected because the
    full-scale reference (32 768²) implicit in
    :data:`_INT16_FULL_SCALE_SQ` doesn't apply.

    Returns 0.0 on empty input — callers should treat this as
    "below silence floor" semantically.
    """
    if frame.dtype != np.int16:
        raise ValueError(f"frame dtype must be int16, got {frame.dtype}")
    if frame.size == 0:
        return 0.0
    return float(np.mean(np.square(frame.astype(np.float64))))


class SnrEstimator:
    """Stateful per-frame SNR estimator.

    Maintains a sliding-window minimum of frame mean-square power
    as the noise floor estimate. Each :meth:`estimate` call:

    1. Computes the incoming frame's mean-square power.
    2. Skips silent frames (power below
       :attr:`SnrEstimatorConfig.silence_floor_linear_sq`) — they
       carry no useful SNR information AND would pollute the
       minimum-tracker with sub-detection noise.
    3. Updates the noise-window deque, popping old entries.
    4. Computes ``SNR = 10·log10((P_signal - P_noise) / P_noise)``
       using the deque minimum as the noise estimate.
    5. Clamps to ``[-120 dB, +120 dB]`` for histogram stability.

    Stateful contract: callers feed every emitted capture frame
    sequentially. Out-of-order or skipped frames make the minimum
    tracker stale — operators must call :meth:`reset` after a
    pipeline restart so the estimator doesn't carry the previous
    session's noise floor into the new one.
    """

    def __init__(self, config: SnrEstimatorConfig) -> None:
        self._config = config
        self._silence_floor = config.silence_floor_linear_sq
        self._window_size = config.noise_window_frames
        self._powers: deque[float] = deque(maxlen=self._window_size)
        self._frames_seen = 0

    @property
    def frames_seen(self) -> int:
        """Total non-silent frames the estimator has processed."""
        return self._frames_seen

    @property
    def noise_floor_estimate(self) -> float | None:
        """Current noise-floor mean-square power, or ``None`` if no data."""
        if not self._powers:
            return None
        return min(self._powers)

    def estimate(self, frame: np.ndarray) -> float:
        """Compute the SNR (dB) of ``frame`` against the tracked noise floor.

        Returns :data:`_SNR_FLOOR_DB` (-120 dB) when the frame is
        below the silence floor — the caller should treat this as
        "no estimate available" rather than a literal SNR of -120.
        Returns :data:`_SNR_CEILING_DB` (+120 dB) when the noise
        floor estimate is at or below the silence floor itself
        (typical at boot before any noise has been observed).
        """
        signal_power = estimate_frame_power(frame)

        if signal_power < self._silence_floor:
            return _SNR_FLOOR_DB

        # Update the minimum-tracker BEFORE the SNR computation so
        # the noise estimate includes the current frame. This
        # guarantees the SNR formula's ``P_signal - P_noise`` is
        # non-negative for all observed frames; the alternative
        # (compute SNR first, then update) leaves a one-frame lag
        # that turns the first observation into a divide-by-zero.
        self._powers.append(signal_power)
        self._frames_seen += 1

        noise_power = min(self._powers)
        if noise_power <= self._silence_floor:
            return _SNR_CEILING_DB

        speech_power = signal_power - noise_power
        if speech_power <= 0.0:
            # Current frame IS the noise floor — SNR is 0 dB.
            return 0.0

        snr_db = 10.0 * float(np.log10(speech_power / noise_power))
        return max(_SNR_FLOOR_DB, min(_SNR_CEILING_DB, snr_db))

    def reset(self) -> None:
        """Clear the noise-floor history.

        Called on device change / pipeline restart so the next
        session's SNR doesn't anchor to the previous session's
        room tone.
        """
        self._powers.clear()
        self._frames_seen = 0


def build_snr_estimator(config: SnrEstimatorConfig) -> SnrEstimator | None:
    """Construct an :class:`SnrEstimator` when enabled, ``None`` otherwise.

    Mirrors the AEC / NS factory pattern: the disabled path
    returns ``None`` so wire-up sites can short-circuit with a
    single None check.
    """
    if not config.enabled:
        return None
    return SnrEstimator(config)


def build_frame_normalizer_snr_estimator(
    *,
    enabled: bool,
    noise_window_seconds: float,
    silence_floor_db: float,
) -> SnrEstimator | None:
    """Build an SNR estimator pinned to the FrameNormalizer invariants.

    Convenience helper mirroring
    :func:`sovyx.voice._aec.build_frame_normalizer_aec`. Pins
    ``sample_rate=16000`` and ``frame_size_samples=512`` so call
    sites only forward operator-tunable knobs.
    """
    config = SnrEstimatorConfig(
        enabled=enabled,
        sample_rate=_DEFAULT_SAMPLE_RATE,
        frame_size_samples=_DEFAULT_FRAME_SIZE_SAMPLES,
        noise_window_seconds=noise_window_seconds,
        silence_floor_db=silence_floor_db,
    )
    return build_snr_estimator(config)


__all__ = [
    "SnrEstimator",
    "SnrEstimatorConfig",
    "build_frame_normalizer_snr_estimator",
    "build_snr_estimator",
    "estimate_frame_power",
]
