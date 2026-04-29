"""Adaptive noise-floor tracker for AGC2 (Phase 4 / T4.51).

Replaces the fixed ``silence_floor_dbfs`` gate (-60 dBFS default)
with a first-quartile-of-RMS estimate over a sliding window —
typically 10 s. Per master mission §Phase 4 / T4.51:

    Use first-quartile of RMS over 10s rolling window as noise floor

The first quartile (Q1, 25th-percentile) is the canonical robust
estimator for the noise level of a recording: it's stable under
the assumption that 25%+ of frames are background-only (typical
speech sessions have inter-utterance silence + transients), and
unlike the MIN-tracker used for SNR estimation it's resilient to
single-frame outliers (e.g. a brief loud bump that would pin the
minimum at speech-level for the entire window).

Foundation phase scope (T4.51, this commit):

* :class:`AdaptiveNoiseFloorTracker` — sliding-window Q1 estimator.
* :class:`AdaptiveFloorConfig` — tuning snapshot.
* Configuration via ``VoiceTuningConfig.voice_agc2_adaptive_floor_*``.
* Wire-up into :class:`AGC2` so the silence gate uses Q1 instead of
  the fixed ``silence_floor_dbfs`` when enabled.

Out of scope (later commits per ``feedback_staged_adoption``):

* T4.52 — VAD feedback path (suppress AGC updates during
  VAD-negative frames). Requires plumbing VAD state into the
  capture path; the adaptive floor stands alone for foundation.
* T4.53 — PESQ A/B validation against the adaptive variant.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_WINDOW_SECONDS = 10.0
"""Master-mission §Phase 4 / T4.51 default window length."""

_DEFAULT_QUANTILE = 0.25
"""First quartile — robust noise-floor estimator."""

_DEFAULT_FRAME_SAMPLES = 512
"""FrameNormalizer's _TARGET_WINDOW invariant."""

_DEFAULT_SAMPLE_RATE = 16_000
"""FrameNormalizer's _TARGET_RATE invariant."""

_FLOOR_LOWER_DBFS = -90.0
"""Hard floor for the adaptive estimate.

Below this the estimate enters int16 quantization territory and
loses meaning; the AGC2 gate should never go this quiet because
the speech level estimator wouldn't update either. Acts as a
safety rail when the tracker has only a few sub-floor samples
(e.g. the first second after a device change).
"""

_FLOOR_UPPER_DBFS = -20.0
"""Hard ceiling for the adaptive estimate.

Above this the gate would silence routine speech (-30 dBFS is
typical room voice). If Q1 ever drifts above this it usually
means the operator is in a continuously loud environment and the
session has zero genuine silence — the cap prevents the AGC2
from gating EVERY frame in that case.
"""


@dataclass(frozen=True, slots=True)
class AdaptiveFloorConfig:
    """Immutable tuning snapshot for the adaptive noise-floor tracker."""

    enabled: bool
    window_seconds: float
    quantile: float
    sample_rate: int
    frame_size_samples: int

    @property
    def window_frames(self) -> int:
        """Ring length in frames at the configured rate.

        ``window_seconds * sample_rate / frame_size_samples``,
        floored at 1 to avoid a degenerate zero-length deque.
        """
        frames_per_second = self.sample_rate / self.frame_size_samples
        return max(1, int(round(self.window_seconds * frames_per_second)))


class AdaptiveNoiseFloorTracker:
    """Sliding-window first-quartile RMS-dBFS estimator.

    Stateful: callers feed every emitted frame's RMS dBFS via
    :meth:`update`, then read :attr:`floor_db` to get the current
    Q1 estimate. The tracker uses a bounded deque so old samples
    are evicted automatically; the Q1 computation is O(n log n)
    in the window length but n ≤ ~313 (10 s @ 16 kHz / 512), so
    each call is sub-millisecond.

    The tracker is silent-frame agnostic — the AGC2 caller decides
    whether to feed every frame or only above-threshold ones. For
    Q1 to be meaningful as a noise-floor estimator the tracker
    MUST see at least 25% silent frames in its window, so feeding
    everything (silent + speech) is the right call.

    Returns ``None`` for :attr:`floor_db` until at least one sample
    has been observed; AGC2 falls back to the fixed
    ``silence_floor_dbfs`` in that bootstrap window.
    """

    def __init__(self, config: AdaptiveFloorConfig) -> None:
        if not (0.0 < config.quantile < 1.0):
            msg = f"quantile must be in (0.0, 1.0), got {config.quantile!r}"
            raise ValueError(msg)
        self._config = config
        self._rms_history: deque[float] = deque(maxlen=config.window_frames)

    def update(self, rms_dbfs: float) -> None:
        """Append one RMS-dBFS sample to the sliding window.

        Args:
            rms_dbfs: Frame RMS in dBFS as computed by AGC2.
                ``-inf`` (true zero-power frames) is rejected — the
                Q1 of a deque containing -inf would always be
                -inf and the gate would never engage. The caller
                should clamp -inf to a sensible floor (e.g.
                :data:`_FLOOR_LOWER_DBFS`) before passing.
        """
        if math.isinf(rms_dbfs) or math.isnan(rms_dbfs):
            return
        self._rms_history.append(float(rms_dbfs))

    @property
    def sample_count(self) -> int:
        """Number of valid RMS samples currently in the window."""
        return len(self._rms_history)

    @property
    def floor_db(self) -> float | None:
        """Current Q1 estimate clamped to ``[_FLOOR_LOWER_DBFS, _FLOOR_UPPER_DBFS]``.

        Returns ``None`` until at least one sample has been
        observed (bootstrap state).
        """
        if not self._rms_history:
            return None
        # numpy.quantile is exact (linear interpolation between
        # the two surrounding samples) and handles the small-N
        # edge cases correctly.
        q = float(np.quantile(list(self._rms_history), self._config.quantile))
        return max(_FLOOR_LOWER_DBFS, min(_FLOOR_UPPER_DBFS, q))

    def reset(self) -> None:
        """Clear the sliding window.

        Called on device change / pipeline restart so the next
        session's noise floor doesn't anchor to the previous
        session's room tone.
        """
        self._rms_history.clear()


def build_agc2_adaptive_floor(
    *,
    enabled: bool,
    window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    quantile: float = _DEFAULT_QUANTILE,
    sample_rate: int = _DEFAULT_SAMPLE_RATE,
    frame_size_samples: int = _DEFAULT_FRAME_SAMPLES,
) -> AdaptiveNoiseFloorTracker | None:
    """Construct an adaptive-floor tracker when enabled, ``None`` otherwise.

    Convenience helper mirroring the AEC / NS / SNR factory
    pattern. AGC2's constructor calls this at instantiation; when
    the tuning flag is off, the tracker is ``None`` and the
    silence gate falls back to the fixed
    ``silence_floor_dbfs`` for bit-exact pre-T4.51 behaviour.
    """
    if not enabled:
        return None
    config = AdaptiveFloorConfig(
        enabled=True,
        window_seconds=window_seconds,
        quantile=quantile,
        sample_rate=sample_rate,
        frame_size_samples=frame_size_samples,
    )
    return AdaptiveNoiseFloorTracker(config)


__all__ = [
    "AdaptiveFloorConfig",
    "AdaptiveNoiseFloorTracker",
    "build_agc2_adaptive_floor",
]
