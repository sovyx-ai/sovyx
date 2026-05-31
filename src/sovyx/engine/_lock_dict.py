"""Bounded LRU dictionary of asyncio.Lock instances.

Use this whenever you need to serialize work per-key but the key space
is potentially unbounded (e.g. one per conversation, per mind, per user).
A plain ``defaultdict(asyncio.Lock)`` grows forever; this class evicts
the least-recently-used lock once ``maxsize`` is reached so memory stays
bounded over the long lifetime of the daemon.

Originally an internal helper of ``bridge/manager.py``; promoted here so
``cloud/flex.py``, ``cloud/usage.py``, and any future caller can share
the implementation.

Telemetry (Phase 6 Task 6.4): the :meth:`LRULockDict.acquire` async
context manager captures wait latency around the underlying
``asyncio.Lock.acquire()`` call and emits structured telemetry — useful
for spotting hot keys, contention bursts, and memory pressure caused
by short-lived locks. The legacy ``__getitem__`` / ``setdefault``
returns the bare lock unchanged so existing ``async with locks[key]:``
sites keep working without modification, just without latency
telemetry until they opt in to ``acquire()``. ``setdefault`` emits a
``lock.evicted`` record (INFO) when the LRU drops an unheld key, or
``lock.evicted_all_held`` (WARNING) when every entry is held and the
oldest is force-dropped. Grep BOTH event names to count all evictions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Generic, TypeVar

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_K = TypeVar("_K")

logger = get_logger(__name__)

# Threshold above which an acquire is logged as contention (WARNING).
# Aligned with plan §6.4 ("Se wait_ms > 10: emit lock.contention.detected").
_CONTENTION_THRESHOLD_MS: float = 10.0

# Truncation for the hashed key tag in telemetry. SHA-256 is overkill
# for cardinality control, but matches the rest of Sovyx (sovyx
# observability.pii uses the same 12-hex-char prefix).
_HASH_PREFIX_LEN: int = 12


def _hash_key(key: object) -> str:
    """Return a short, irreversible label for *key* suitable for telemetry.

    Lock keys are often conversation/user/mind ids — never log them raw.
    A 12-char SHA-256 prefix gives ~2^48 buckets, ample for grouping
    contention reports without leaking the key space.
    """
    encoded = str(key).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()[:_HASH_PREFIX_LEN]


class LRULockDict(Generic[_K]):
    """Bounded dict of ``asyncio.Lock`` instances with LRU eviction.

    Prevents unbounded memory growth when keys are generated dynamically
    over the daemon's lifetime (one lock per chat conversation, per
    user balance, per usage cascade key, etc.). When ``maxsize`` is
    reached the least-recently-used lock is evicted.

    Thread/async safety: this object is intended to be touched only from
    the event loop. ``setdefault`` is the only mutating method and it is
    O(1).
    """

    def __init__(self, maxsize: int = 500) -> None:
        if maxsize <= 0:
            msg = f"maxsize must be > 0, got {maxsize}"
            raise ValueError(msg)
        self._maxsize = maxsize
        self._locks: OrderedDict[_K, asyncio.Lock] = OrderedDict()

    def setdefault(self, key: _K, default: asyncio.Lock | None = None) -> asyncio.Lock:
        """Return the existing lock for ``key`` (promoted to MRU) or insert one.

        If ``default`` is omitted a fresh ``asyncio.Lock()`` is created
        on insertion. Insertion may evict the oldest entry — eviction
        emits a ``lock.evicted`` telemetry record so operators can spot
        churn or undersized ``maxsize`` values.

        v0.31.7 T3.3 (LOW.1) — eviction now SKIPS held locks. Pre-T3.3
        eviction unconditionally popped the LRU entry; if that lock
        happened to be currently held by an awaiter (rare in practice
        because callers release before the entry decays to LRU, but
        possible under heavy contention with N>maxsize keys all in
        flight), the held lock was orphaned + the awaiter's
        ``release()`` later targeted a phantom. Walk the OrderedDict
        from oldest to newest and drop the first UNHELD entry; if all
        N entries are held, log a WARN + insert anyway and accept the
        eviction risk on the genuinely-oldest held lock (under correct
        usage this branch is unreachable; the WARN exists so operators
        can spot pathological contention before it produces user
        symptoms).
        """
        if key in self._locks:
            self._locks.move_to_end(key)
            return self._locks[key]
        # Evict if at capacity. Walk LRU→MRU; skip held locks; emit a
        # structured WARN if every entry is held (means maxsize is
        # undersized for the real concurrency).
        while len(self._locks) >= self._maxsize:
            evicted_key: _K | None = None
            evicted_lock: asyncio.Lock | None = None
            for candidate_key, candidate_lock in self._locks.items():
                if not candidate_lock.locked():
                    evicted_key = candidate_key
                    evicted_lock = candidate_lock
                    break
            if evicted_key is None:
                # Every lock is held — capacity exhausted by live
                # awaiters. Pop the genuinely-oldest entry (front of
                # the OrderedDict) so we make progress; log WARN so
                # operators can bump ``maxsize``. Orphans the held
                # lock — the awaiter's later ``release()`` is harmless
                # because the lock object survives via reference in
                # the awaiting coroutine; the dictionary mapping is
                # what's lost.
                evicted_key, evicted_lock = self._locks.popitem(last=False)
                logger.warning(
                    "lock.evicted_all_held",
                    **{
                        "lock.key_hash": _hash_key(evicted_key),
                        "lock.dict_size": len(self._locks),
                        "lock.maxsize": self._maxsize,
                        "lock.was_locked": True,
                        "lock.action_required": (
                            "Every lock in the LRULockDict is currently "
                            "held — capacity is undersized for the live "
                            "concurrency. Consider raising maxsize."
                        ),
                    },
                )
            else:
                del self._locks[evicted_key]
                logger.info(
                    "lock.evicted",
                    **{
                        "lock.key_hash": _hash_key(evicted_key),
                        "lock.dict_size": len(self._locks),
                        "lock.maxsize": self._maxsize,
                        "lock.was_locked": False,
                    },
                )
        lock = default if default is not None else asyncio.Lock()
        self._locks[key] = lock
        return lock

    def __getitem__(self, key: _K) -> asyncio.Lock:
        """Return the lock for ``key``, creating + inserting one on miss.

        Mirrors ``defaultdict(asyncio.Lock)`` semantics so existing
        ``async with self._locks[key]:`` patterns work without changes.
        Callers that want acquire-latency telemetry should switch to
        :meth:`acquire`.
        """
        return self.setdefault(key)

    @contextlib.asynccontextmanager
    async def acquire(self, key: _K) -> AsyncIterator[asyncio.Lock]:
        """Acquire the lock for *key*, emitting wait-latency telemetry.

        Use as ``async with locks.acquire(key):``. Captures
        ``perf_counter`` deltas around ``lock.acquire()`` and emits
        ``lock.acquire.latency`` (DEBUG, sampled downstream by
        :class:`SamplingProcessor`). Waits longer than
        ``_CONTENTION_THRESHOLD_MS`` additionally emit
        ``lock.contention.detected`` at WARNING so tail-latency
        regressions are visible without trawling DEBUG.

        The yielded value is the underlying ``asyncio.Lock`` so callers
        can re-enter or pass it on; the lock is released automatically
        when the ``async with`` block exits, even on exceptions.
        """
        lock = self.setdefault(key)
        key_hash = _hash_key(key)
        t0 = time.perf_counter()
        await lock.acquire()
        wait_ms = (time.perf_counter() - t0) * 1000.0
        try:
            logger.debug(
                "lock.acquire.latency",
                **{
                    "lock.key_hash": key_hash,
                    "lock.wait_ms": round(wait_ms, 3),
                    "lock.dict_size": len(self._locks),
                },
            )
            if wait_ms > _CONTENTION_THRESHOLD_MS:
                logger.warning(
                    "lock.contention.detected",
                    **{
                        "lock.key_hash": key_hash,
                        "lock.wait_ms": round(wait_ms, 3),
                        "lock.threshold_ms": _CONTENTION_THRESHOLD_MS,
                        "lock.dict_size": len(self._locks),
                    },
                )
            yield lock
        finally:
            lock.release()

    def __len__(self) -> int:
        return len(self._locks)

    def __contains__(self, key: object) -> bool:
        return key in self._locks
