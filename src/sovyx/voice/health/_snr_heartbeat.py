"""Per-heartbeat SNR sample aggregator (Phase 4 / T4.34).

Captures per-window SNR estimates from the FrameNormalizer between
two consecutive ``voice_pipeline_heartbeat`` emissions and exposes
``p50`` / ``p95`` summaries for the orchestrator to log.

Module-level singleton state is the right shape today: voice runs
as a single in-process pipeline (single-mind production until
v0.31.0). When multi-mind ships, this aggregator graduates to a
per-mind instance keyed by ``mind_id``; the call sites already
pass ``mind_id`` through the heartbeat so the migration is a
search-and-replace.

Cardinality / memory: bounded ring buffer of
:data:`_MAX_BUFFER_SAMPLES` floats. At the FrameNormalizer's
~31 windows-per-second rate (16 kHz / 512 samples) and the
default 30-second heartbeat interval, ~930 samples accumulate
per heartbeat — comfortably below the buffer cap. Overflow drops
the oldest samples (FIFO) so the window's p95 still reflects the
most recent capture state.

Concurrency: the FrameNormalizer runs on the capture audio
thread; the orchestrator drains on its asyncio loop. Both touch
the buffer through a :class:`threading.Lock` — appends complete
in microseconds and never block the orchestrator.

The orchestrator MUST call :func:`drain_window_stats` exactly
once per heartbeat cycle: read-and-clear semantics ensure the
NEXT window's stats reflect ONLY the samples that arrived
between two consecutive calls, matching the contract of the
existing ``max_vad_probability`` / ``frames_processed`` fields
on the heartbeat.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

_MAX_BUFFER_SAMPLES = 4_096
"""Hard cap on the per-heartbeat buffer. At ~31 windows/s + 30 s
heartbeat = ~930 samples; 4 096 leaves headroom for slower
heartbeat cadences (max 60 s in practice) without uncapped growth
on a stuck orchestrator."""


@dataclass(frozen=True, slots=True)
class SnrWindowStats:
    """Per-heartbeat-window SNR summary.

    All three fields are computed from samples observed since the
    previous :func:`drain_window_stats` call.
    """

    p50_db: float
    """Median SNR in dB across the window. ``0.0`` when count == 0
    (no real samples drained — the heartbeat field is suppressed
    in that case so the dashboard doesn't render a synthetic 0)."""

    p95_db: float
    """95th-percentile SNR in dB. Same zero-fallback contract as
    ``p50_db``."""

    count: int
    """Number of samples that contributed to the percentiles. The
    orchestrator gates the heartbeat field on ``count > 0``."""


_lock = threading.Lock()
_buffer: deque[float] = deque(maxlen=_MAX_BUFFER_SAMPLES)


def record_snr_sample(snr_db: float) -> None:
    """Append one SNR sample to the heartbeat-window buffer.

    Called from :meth:`sovyx.voice._frame_normalizer.FrameNormalizer.
    _observe_snr` once per emitted capture window. The sample value
    has already been filtered against the SNR floor and the
    first-frame anchor by the caller; this aggregator does NOT
    re-filter so the call site retains a single point of policy.

    Args:
        snr_db: Per-window SNR estimate in decibels. Values
            outside the typical -30 to +60 dB range are still
            recorded — clamping is the dashboard layer's
            responsibility.
    """
    with _lock:
        _buffer.append(snr_db)


def drain_window_stats() -> SnrWindowStats:
    """Compute + clear the per-window p50/p95 summary.

    Called once per ``voice_pipeline_heartbeat`` emission. The
    return value reflects samples observed since the previous
    drain; the buffer is cleared atomically so the NEXT call sees
    a fresh window.

    Returns:
        :class:`SnrWindowStats` carrying the percentile pair and
        the sample count. ``count == 0`` means no samples
        accumulated in this window — typical during sustained
        silence (FrameNormalizer suppresses floor-only emissions)
        or before the first speech frame on boot.
    """
    with _lock:
        samples = list(_buffer)
        _buffer.clear()

    count = len(samples)
    if count == 0:
        return SnrWindowStats(p50_db=0.0, p95_db=0.0, count=0)

    samples.sort()
    p50_idx = count // 2
    # 95th percentile via nearest-rank method — correct for any
    # count >= 1, no fractional-index edge cases. count*0.95
    # rounds DOWN; we add the conventional +1 nearest-rank
    # adjustment (see Wikipedia "Percentile / nearest-rank") and
    # clamp into [0, count-1].
    p95_idx = min(count - 1, max(0, int(count * 0.95)))
    return SnrWindowStats(
        p50_db=float(samples[p50_idx]),
        p95_db=float(samples[p95_idx]),
        count=count,
    )


def reset_for_tests() -> None:
    """Clear the buffer without computing stats.

    Test-only helper. Production code MUST go through
    :func:`drain_window_stats` so the contract "drain returns the
    last window's stats" holds.
    """
    with _lock:
        _buffer.clear()
