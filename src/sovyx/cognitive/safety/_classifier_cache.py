"""Classification cache + statistics — extracted from safety_classifier.py."""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.cognitive.safety._classifier_types import SafetyVerdict


# ── Classification Cache ────────────────────────────────────────────────
# LRU cache with TTL. Key = hash of first 200 chars (deterministic for
# same input). Bounded to prevent memory growth.

_CACHE_TTL_SEC = 300.0  # 5 minutes
_CACHE_MAX_SIZE = 1024


@dataclass(slots=True)
class _CacheEntry:
    """Cached classification result with expiry."""

    verdict: SafetyVerdict
    expires_at: float


class ClassificationCache:
    """Thread-safe LRU cache for safety classifications.

    Key: SHA-256 of first 200 chars of input text.
    Value: SafetyVerdict with TTL.
    Max size: bounded with LRU eviction.

    GIL-protected (single-threaded asyncio) — no explicit locking needed.
    """

    def __init__(
        self,
        *,
        max_size: int = _CACHE_MAX_SIZE,
        ttl_sec: float = _CACHE_TTL_SEC,
    ) -> None:
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl_sec = ttl_sec
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(text: str) -> str:
        """Generate cache key from text prefix."""
        return hashlib.sha256(text[:200].encode()).hexdigest()[:16]

    def get(self, text: str) -> SafetyVerdict | None:
        """Look up cached verdict. Returns None on miss or expired."""
        key = self._key(text)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            # Expired — remove and miss
            del self._cache[key]
            self._misses += 1
            return None
        # Hit — move to end (most recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        return entry.verdict

    def put(self, text: str, verdict: SafetyVerdict) -> None:
        """Store verdict in cache. Evicts LRU if at capacity."""
        key = self._key(text)
        self._cache[key] = _CacheEntry(
            verdict=verdict,
            expires_at=time.monotonic() + self._ttl_sec,
        )
        self._cache.move_to_end(key)
        # Evict oldest if over capacity
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0-1.0). Returns 0.0 if no lookups."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    @property
    def size(self) -> int:
        """Current number of cached entries."""
        return len(self._cache)

    def clear(self) -> None:
        """Clear all cached entries (for testing)."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0


# ── Cache accessor (delegates to SafetyContainer) ──────────────────────


def get_classification_cache() -> ClassificationCache:
    """Get the ClassificationCache from the global container.

    Returns:
        The ClassificationCache instance managed by SafetyContainer.
    """
    from sovyx.cognitive.safety_container import get_safety_container

    return get_safety_container().classification_cache


# ── Cache Statistics ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Snapshot of cache statistics.

    Attributes:
        size: Current number of entries.
        max_size: Maximum capacity.
        hit_rate: Hit rate (0.0-1.0).
        hits: Total cache hits.
        misses: Total cache misses.
        ttl_sec: TTL for entries in seconds.
    """

    size: int
    max_size: int
    hit_rate: float
    hits: int
    misses: int
    ttl_sec: float


def get_cache_stats() -> CacheStats:
    """Get a snapshot of the classification cache statistics.

    Useful for dashboard display and monitoring.
    """
    cache = get_classification_cache()
    return CacheStats(
        size=cache.size,
        max_size=cache._max_size,
        hit_rate=cache.hit_rate,
        hits=cache._hits,
        misses=cache._misses,
        ttl_sec=cache._ttl_sec,
    )
