"""Rolling-window noise-floor trend tracker (Phase 4 / T4.38).

Tracks the capture noise floor (in dBFS) over a long enough
window that a *sustained* room-noise increase surfaces while
short-lived spikes (door slam, mic bump, single keystroke) are
smoothed out. Pairs with the T4.34 / T4.35 SNR heartbeat
pipeline: SNR p50 measures the speech-vs-noise ratio per
window, this module measures the noise floor itself drifting.

Architecture mirrors :mod:`._snr_heartbeat`:

* FrameNormalizer feeds samples via :func:`record_noise_floor_sample`
  once per emitted capture window.
* The orchestrator's ``_track_vad_for_heartbeat`` calls
  :func:`compute_drift` to read (without clearing) the current
  short-window vs long-window mean delta, and the per-mind
  alert latch fires WARN / CLEARED on sustained drift.

Unlike the SNR drain, the noise-floor sampler is **read-only**
on each heartbeat: the rolling buffer keeps accumulating across
heartbeats so the trend computation has a stable horizon. The
buffer wraps at the long-window cap; old samples drop FIFO via
``deque(maxlen=...)``.

Cardinality / memory: at the FrameNormalizer's ~31 windows-per-
second rate, a 5-minute buffer holds ~9 300 samples. ``deque``
with bounded ``maxlen`` keeps that under 80 KB and trims O(1)
on overflow. The drift computation is O(N) per heartbeat (one
walk over each window's slice); at the 30 s heartbeat interval
that is ≤ 0.5 ms of CPU per heartbeat — negligible.

Concurrency: same lock contract as :mod:`._snr_heartbeat` —
producer (capture audio thread) and consumer (orchestrator
asyncio loop) both touch the buffer under ``threading.Lock``.

Multi-mind future: module-level singleton matches the rest of
the voice/health stack (see :mod:`._snr_heartbeat`); per-mind
instances graduate when v0.31.0 ships multi-mind voice.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

_SHORT_WINDOW_SAMPLES = 1_800
"""Samples in the short ("now") window. At ~31 windows/s that
is ~60 s of capture — long enough to absorb single-frame noise
bursts but short enough to react to a fan turning on within a
minute."""

_LONG_WINDOW_SAMPLES = 9_300
"""Samples in the long ("baseline") window. ~5 minutes at the
FrameNormalizer's 31 windows/s rate, matching the master
mission's §Phase 4 / T4.38 contract: "moving average of
background RMS over 5 min window; alert if floor raised >10
dB". The long window IS the rolling baseline; sustained
drift re-baselines automatically after one full window of
the new noise floor."""


@dataclass(frozen=True, slots=True)
class NoiseFloorDrift:
    """Per-heartbeat noise-floor drift summary.

    ``drift_db = short_avg_db - long_avg_db``. Positive means
    the floor rose; negative means it fell (room got quieter).
    The orchestrator alerts on the positive-direction crossing
    of the configured threshold (default 10 dB).
    """

    short_avg_db: float
    """Mean noise-floor dBFS across the most recent ~60 s. ``0.0``
    when ``short_count == 0``."""

    long_avg_db: float
    """Mean noise-floor dBFS across the rolling ~5 min baseline.
    ``0.0`` when ``long_count == 0``."""

    drift_db: float
    """Signed delta short_avg_db − long_avg_db. ``0.0`` when
    either window lacks samples (the orchestrator gates the
    alert on ``ready=True``)."""

    short_count: int
    """Sample count in the short window. The orchestrator gates
    the heartbeat field on ``short_count > 0``."""

    long_count: int
    """Sample count in the long window. ``ready=True`` requires
    this to equal :data:`_LONG_WINDOW_SAMPLES` so the baseline
    has had time to settle."""

    ready: bool
    """``True`` iff both windows are full enough that drift is
    meaningful — short window has at least
    :data:`_SHORT_WINDOW_SAMPLES // 4` samples (≈15 s) AND the
    long window is fully populated. Pre-``ready`` heartbeats
    skip the alert path so a cold-boot transient doesn't
    misfire."""


_lock = threading.Lock()
_buffer: deque[float] = deque(maxlen=_LONG_WINDOW_SAMPLES)


def record_noise_floor_sample(noise_floor_db: float) -> None:
    """Append one noise-floor dBFS sample to the rolling buffer.

    Called from :meth:`sovyx.voice._frame_normalizer.FrameNormalizer.
    _observe_snr` once per emitted capture window. The caller
    converts the SnrEstimator's linear noise-power tracker to
    dBFS using the int16 full-scale reference; this aggregator
    does NOT re-derive units.

    Args:
        noise_floor_db: Current noise-floor estimate in dBFS.
            Typical range ``[-90, -30]`` — anything outside is
            still recorded (clamping is the dashboard layer's
            responsibility, not the aggregator's).
    """
    with _lock:
        _buffer.append(noise_floor_db)


def compute_drift() -> NoiseFloorDrift:
    """Read short-vs-long noise-floor drift WITHOUT clearing.

    Unlike :func:`._snr_heartbeat.drain_window_stats`, this
    function does NOT modify the buffer — the rolling window
    keeps accumulating across heartbeats so the trend horizon
    is stable. Each heartbeat sees a fresh "short vs long"
    snapshot derived from the same long-running buffer.

    Returns:
        :class:`NoiseFloorDrift` with the two window averages,
        their difference, and a ``ready`` flag the orchestrator
        uses to gate the alert path.
    """
    with _lock:
        # Snapshot under lock; analyse outside.
        snapshot = list(_buffer)

    long_count = len(snapshot)
    short_count = min(long_count, _SHORT_WINDOW_SAMPLES)

    if short_count == 0:
        return NoiseFloorDrift(
            short_avg_db=0.0,
            long_avg_db=0.0,
            drift_db=0.0,
            short_count=0,
            long_count=0,
            ready=False,
        )

    # Short window = the most recent _SHORT_WINDOW_SAMPLES samples.
    short_slice = snapshot[-short_count:]
    short_avg = sum(short_slice) / short_count
    long_avg = sum(snapshot) / long_count

    # Ready gate: long window must be fully populated AND short
    # window has at least 25 % of its capacity. Below the 25 %
    # floor the short avg is too noisy to compare.
    ready = long_count >= _LONG_WINDOW_SAMPLES and short_count >= _SHORT_WINDOW_SAMPLES // 4

    return NoiseFloorDrift(
        short_avg_db=float(short_avg),
        long_avg_db=float(long_avg),
        drift_db=float(short_avg - long_avg),
        short_count=short_count,
        long_count=long_count,
        ready=ready,
    )


def reset_for_tests() -> None:
    """Clear the rolling buffer.

    Test-only helper. Production code does NOT clear the buffer —
    the long window's purpose is to span pipeline lifetimes;
    clearing it from the heartbeat would defeat the trend
    detection.
    """
    with _lock:
        _buffer.clear()
