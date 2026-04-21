"""Process-global windowed counters for the meta-monitoring endpoint.

The :func:`/api/observability/health` route (§27.2) reports two
60-second windowed counts: dropped log records and handler errors.
Cumulative counters (already exposed by
:meth:`AsyncQueueHandler.dropped_count`) only tell you "how many over
the entire process lifetime" — operators need "how many in the last
minute" to see *current* health.

Implementation: a thread-safe sliding-window counter backed by a
:class:`collections.deque` of monotonic timestamps. Each
``record()`` appends the current monotonic clock; ``count_in_last``
discards expired entries lazily on read so producers stay fast and
readers absorb the cleanup cost (which is bounded — the deque
length is capped to the most recent ``maxlen`` samples regardless
of window).

The singletons live at module scope so producers (the queue handler
in one thread, the file handler in another) and the FastAPI route
all see the same instance without going through the engine
:class:`ServiceRegistry` — health reporting must work even if the
registry is unavailable (test apps, partial-bootstrap failures).

Aligned with IMPL-OBSERVABILITY-001 §27.1 / §27.2.
"""

from __future__ import annotations

import threading
import time
from collections import deque

# Window size for ``dropped_60s`` / ``handler_errors_60s`` — matches
# the §27.2 endpoint contract verbatim. Kept as a module constant so
# the route and the trackers can't drift apart.
_WINDOW_SECONDS: float = 60.0

# Per-counter cap on retained samples. 4 096 events in a 60-second
# window equates to ~68/s sustained — well above any realistic
# steady-state drop rate. The bound exists so a runaway producer
# can't grow the deque without limit before the next read prunes it.
_MAX_SAMPLES: int = 4_096


class WindowedCounter:
    """Thread-safe sliding-window event counter.

    ``record()`` appends ``time.monotonic()`` to a bounded deque;
    ``count_in_last(seconds)`` returns the number of timestamps still
    inside the requested trailing window. Pruning is lazy — readers
    drop expired entries from the left of the deque before counting,
    which keeps producers' critical section to one ``append()`` call
    under the lock.
    """

    __slots__ = ("_lock", "_samples")

    def __init__(self, maxlen: int = _MAX_SAMPLES) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self) -> None:
        """Mark one event as having happened *now*."""
        ts = time.monotonic()
        with self._lock:
            self._samples.append(ts)

    def count_in_last(self, seconds: float) -> int:
        """Return the number of recorded events within the trailing *seconds*."""
        cutoff = time.monotonic() - seconds
        with self._lock:
            # Lazy prune from the left — deque is monotonic in ts.
            while self._samples and self._samples[0] < cutoff:
                self._samples.popleft()
            return len(self._samples)

    def reset(self) -> None:
        """Drop every recorded sample (test helper — never used in production)."""
        with self._lock:
            self._samples.clear()


# ── Process-global singletons ──────────────────────────────────────

_DROP_COUNTER: WindowedCounter = WindowedCounter()
_HANDLER_ERROR_COUNTER: WindowedCounter = WindowedCounter()


def record_drop() -> None:
    """Record one log-record drop (queue full or downstream refusal)."""
    _DROP_COUNTER.record()


def record_handler_error() -> None:
    """Record one handler emit error (e.g., failed write, JSON serialize fail)."""
    _HANDLER_ERROR_COUNTER.record()


def count_drops_60s() -> int:
    """Return the number of drops recorded in the last 60 seconds."""
    return _DROP_COUNTER.count_in_last(_WINDOW_SECONDS)


def count_handler_errors_60s() -> int:
    """Return the number of handler errors recorded in the last 60 seconds."""
    return _HANDLER_ERROR_COUNTER.count_in_last(_WINDOW_SECONDS)


def reset_for_testing() -> None:
    """Clear both counters — wired by test fixtures only."""
    _DROP_COUNTER.reset()
    _HANDLER_ERROR_COUNTER.reset()


__all__ = [
    "WindowedCounter",
    "count_drops_60s",
    "count_handler_errors_60s",
    "record_drop",
    "record_handler_error",
    "reset_for_testing",
]
