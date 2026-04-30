"""Rolling SNR sample window for transcription-time queries (T4.36).

Parallel to :mod:`._snr_heartbeat`: the heartbeat aggregator drains
samples atomically per heartbeat, so by transcription time the
buffer is typically empty. This module keeps a separate small
rolling window (default ~10 s of samples) that the orchestrator
can READ at end-of-recording to estimate the SNR distribution
during the just-completed utterance.

The orchestrator uses the rolling p50 to compute a per-utterance
``snr_confidence_factor`` ∈ [0, 1] that downstream consumers
(cognitive layer, dashboard) can multiply against the STT
engine's raw confidence to reflect "how trustworthy is this
transcription given the room noise".

Architecture mirrors the other voice/health rolling-window
modules (see :mod:`._noise_floor_trending` for the same
read-only contract). Producer = FrameNormalizer's
``_observe_snr``; consumer = orchestrator's transcription
completion path.

Cardinality / memory: 310 samples × 8 bytes = ~2.5 KB. Bounded
``deque(maxlen=N)`` drops oldest samples FIFO.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

_WINDOW_SAMPLES = 310
"""~10 s at the FrameNormalizer's 31 samples/s rate. Long enough
to capture sustained-noise utterances without dilution from
silence-only frames; short enough that the SNR snapshot reflects
the recent (i.e. utterance-relevant) capture state, not minutes-
old data."""


@dataclass(frozen=True, slots=True)
class RecentSnrSummary:
    """Per-query SNR percentile snapshot.

    All values reflect the rolling buffer contents at query
    time. The orchestrator reads this at transcription
    completion + uses ``p50_db`` to compute the confidence
    factor.
    """

    p50_db: float
    """Median SNR in dB across the buffer. ``0.0`` when
    ``count == 0`` (the orchestrator gates the confidence
    factor on ``count > 0``)."""

    count: int
    """Number of samples in the buffer at query time. ``0``
    means no recent SNR samples — typical right after boot
    before the first speech frame, OR during a long silence
    run that the FrameNormalizer's emit filter excluded from
    the stream."""


_lock = threading.Lock()
_buffer: deque[float] = deque(maxlen=_WINDOW_SAMPLES)


def record_sample(snr_db: float) -> None:
    """Append one SNR sample to the rolling buffer.

    Called from :meth:`sovyx.voice._frame_normalizer.FrameNormalizer.
    _observe_snr` alongside the existing T4.34 heartbeat-
    aggregator feed. The two aggregators are independent: the
    heartbeat drains atomically per heartbeat, this one keeps
    rolling so transcription can read at any time.

    Args:
        snr_db: Per-window SNR estimate in decibels. Same filter
            as the heartbeat path (caller skips floor + first-
            frame samples) so the percentile pair stays
            consistent across both aggregators.
    """
    with _lock:
        _buffer.append(snr_db)


def window_summary() -> RecentSnrSummary:
    """Read the current rolling p50 + count without clearing.

    Returns:
        :class:`RecentSnrSummary`. ``count == 0`` indicates no
        recent SNR samples — the orchestrator falls back to
        STT confidence unmodified in that case.
    """
    with _lock:
        snapshot = list(_buffer)
    count = len(snapshot)
    if count == 0:
        return RecentSnrSummary(p50_db=0.0, count=0)
    snapshot.sort()
    p50 = snapshot[count // 2]
    return RecentSnrSummary(p50_db=float(p50), count=count)


def reset_for_tests() -> None:
    """Clear the buffer.

    Test-only helper. Production code does NOT clear the buffer —
    the rolling window's purpose is to span utterance boundaries
    so transcription-time queries always have recent context.
    """
    with _lock:
        _buffer.clear()
