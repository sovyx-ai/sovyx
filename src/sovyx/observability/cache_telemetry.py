"""Cache telemetry helpers — sampled hit/miss + always-on evict.

Caches that emit ``cache.hit`` / ``cache.miss`` on every lookup would
swamp the log stream (the safety classifier cache alone serves dozens
of lookups per second under chat load). This helper batches lookups
into windows of ``sample_rate`` calls — only one in every N is logged
— while keeping a running hit ratio that the emitted record exposes.

Eviction events are rare and load-bearing (they tell operators a cache
is undersized or churning), so ``record_evict`` is *not* sampled.

Usage::

    from sovyx.observability.cache_telemetry import CacheTelemetry

    class MyCache:
        def __init__(self) -> None:
            self._store: dict[str, V] = {}
            self._telemetry = CacheTelemetry(name="my.cache")

        def get(self, key: str) -> V | None:
            entry = self._store.get(key)
            if entry is None:
                self._telemetry.record_miss(size=len(self._store), maxsize=None)
                return None
            self._telemetry.record_hit(size=len(self._store), maxsize=None)
            return entry

The default sample rate of 100 matches plan §6.7
("sampled — não em toda lookup, 1/100").
"""

from __future__ import annotations

import threading

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_SAMPLE_RATE: int = 100


class CacheTelemetry:
    """Per-cache hit/miss/evict emitter with built-in sampling.

    Construct one per logical cache (the ``name`` becomes ``cache.name``
    in every emitted record). Counters live on the instance and are
    guarded by ``threading.Lock`` — the asyncio caches in the codebase
    don't strictly need it, but the lock cost is negligible compared to
    the cache lookup itself and protects against future cross-thread
    callers (executor pool, sync wrappers).

    The hit ratio is computed lifetime-to-date, not per-window — that
    way a transient drop in hit rate (e.g. cold cache after restart)
    decays naturally as the cache warms up rather than swinging wildly
    inside small sampling windows.
    """

    __slots__ = ("_hits", "_lock", "_lookups", "_misses", "_name", "_sample_rate")

    def __init__(self, name: str, *, sample_rate: int = _DEFAULT_SAMPLE_RATE) -> None:
        self._name = name
        # ``max(1, ...)`` so ``sample_rate=0`` (a misconfiguration) still
        # behaves — emits every lookup instead of dividing by zero.
        self._sample_rate = max(1, int(sample_rate))
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._lookups = 0

    @property
    def name(self) -> str:
        """Logical cache name (carried as ``cache.name`` in emitted records)."""
        return self._name

    def record_hit(self, *, size: int, maxsize: int | None) -> None:
        """Tally a hit and emit ``cache.hit`` if this lookup is sampled."""
        with self._lock:
            self._hits += 1
            self._lookups += 1
            should_emit = (self._lookups - 1) % self._sample_rate == 0
            ratio = self._hits / max(1, self._hits + self._misses)
        if should_emit:
            logger.debug(
                "cache.hit",
                **{
                    "cache.name": self._name,
                    "cache.size": size,
                    "cache.maxsize": maxsize,
                    "cache.hit_ratio": round(ratio, 4),
                    "cache.sample_rate": self._sample_rate,
                },
            )

    def record_miss(self, *, size: int, maxsize: int | None) -> None:
        """Tally a miss and emit ``cache.miss`` if this lookup is sampled."""
        with self._lock:
            self._misses += 1
            self._lookups += 1
            should_emit = (self._lookups - 1) % self._sample_rate == 0
            ratio = self._hits / max(1, self._hits + self._misses)
        if should_emit:
            logger.debug(
                "cache.miss",
                **{
                    "cache.name": self._name,
                    "cache.size": size,
                    "cache.maxsize": maxsize,
                    "cache.hit_ratio": round(ratio, 4),
                    "cache.sample_rate": self._sample_rate,
                },
            )

    def record_evict(
        self,
        *,
        size: int,
        maxsize: int | None,
        reason: str = "lru",
    ) -> None:
        """Always emit ``cache.evict`` — eviction is rare and signal-rich."""
        with self._lock:
            ratio = self._hits / max(1, self._hits + self._misses)
        logger.info(
            "cache.evict",
            **{
                "cache.name": self._name,
                "cache.size": size,
                "cache.maxsize": maxsize,
                "cache.hit_ratio": round(ratio, 4),
                "cache.evict_reason": reason,
            },
        )

    def snapshot(self) -> tuple[int, int, float]:
        """Return ``(hits, misses, hit_ratio)`` — test helper."""
        with self._lock:
            ratio = self._hits / max(1, self._hits + self._misses)
            return self._hits, self._misses, round(ratio, 4)


__all__ = ["CacheTelemetry"]
