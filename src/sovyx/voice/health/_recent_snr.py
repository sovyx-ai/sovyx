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

Phase 5.A.2 — multi-mind keying. Each mind has its own bounded
ring buffer keyed by ``mind_id``. ``OrderedDict`` + LRU eviction
caps memory at ``_MAX_MINDS = 32`` per process; misbehaving
callers that generate unbounded mind_id values can't blow the
heap. Pre-Phase-5.A.2 a single module-level ``deque`` merged
samples from every mind on multi-mind hosts, distorting the
per-utterance confidence factor.

Cardinality / memory: 310 samples × 8 bytes × 32 minds = ~80 KB
worst case. Bounded ``deque(maxlen=N)`` drops oldest samples FIFO.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass

_WINDOW_SAMPLES = 310
"""~10 s at the FrameNormalizer's 31 samples/s rate. Long enough
to capture sustained-noise utterances without dilution from
silence-only frames; short enough that the SNR snapshot reflects
the recent (i.e. utterance-relevant) capture state, not minutes-
old data."""


_DEFAULT_MIND = "default"
"""Sentinel for un-migrated callers (probe / health-check sites
that don't bind to a specific mind). Real production callers
pass an explicit mind_id."""


_MAX_MINDS = 32
"""LRU cap. Two orders of magnitude above any plausible operator
mind topology (typical 1-5 minds; large multi-tenant deployments
~10-15). Defends against unbounded-mind_id misuse."""


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
# OrderedDict so we can move-to-end on access for LRU semantics. Each
# value is a bounded ring buffer for that mind's recent samples.
_per_mind_buffers: OrderedDict[str, deque[float]] = OrderedDict()


def _get_or_create_buffer_locked(mind_id: str) -> deque[float]:
    """Resolve the per-mind buffer; create + LRU-evict if needed.

    Caller MUST hold ``_lock``. The OrderedDict is touched on EVERY
    access to maintain LRU ordering — the eviction target is whichever
    mind's buffer hasn't been touched longest.
    """
    buf = _per_mind_buffers.get(mind_id)
    if buf is None:
        # Evict oldest mind if at capacity. Cold path in normal
        # multi-mind use; defends against a misbehaving caller that
        # generates unbounded mind_id values.
        while len(_per_mind_buffers) >= _MAX_MINDS:
            evicted_mind, _ = _per_mind_buffers.popitem(last=False)
            del evicted_mind  # name retained for grep / future logging
        buf = deque(maxlen=_WINDOW_SAMPLES)
        _per_mind_buffers[mind_id] = buf
    else:
        # LRU touch: move to end (most-recently-used).
        _per_mind_buffers.move_to_end(mind_id, last=True)
    return buf


def record_sample(snr_db: float, *, mind_id: str = _DEFAULT_MIND) -> None:
    """Append one SNR sample to the rolling buffer for ``mind_id``.

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
        mind_id: Owning mind. Default ``"default"`` for backward-
            compat with un-migrated producers; multi-mind producers
            (FrameNormalizer post-Phase 5.A.2) pass the configured
            mind_id of their owning AudioCaptureTask.
    """
    with _lock:
        _get_or_create_buffer_locked(mind_id).append(snr_db)


def window_summary(*, mind_id: str = _DEFAULT_MIND) -> RecentSnrSummary:
    """Read the current rolling p50 + count for ``mind_id`` without clearing.

    Args:
        mind_id: Mind whose buffer to read. Default ``"default"`` for
            backward-compat with un-migrated consumers.

    Returns:
        :class:`RecentSnrSummary`. ``count == 0`` indicates no
        recent SNR samples — the orchestrator falls back to
        STT confidence unmodified in that case.
    """
    with _lock:
        buf = _per_mind_buffers.get(mind_id)
        snapshot = list(buf) if buf is not None else []
    count = len(snapshot)
    if count == 0:
        return RecentSnrSummary(p50_db=0.0, count=0)
    snapshot.sort()
    p50 = snapshot[count // 2]
    return RecentSnrSummary(p50_db=float(p50), count=count)


def reset_for_tests() -> None:
    """Clear every per-mind buffer.

    Test-only helper. Production code does NOT clear buffers —
    the rolling window's purpose is to span utterance boundaries
    so transcription-time queries always have recent context.
    """
    with _lock:
        _per_mind_buffers.clear()
