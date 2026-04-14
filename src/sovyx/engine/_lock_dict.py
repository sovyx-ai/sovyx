"""Bounded LRU dictionary of asyncio.Lock instances.

Use this whenever you need to serialize work per-key but the key space
is potentially unbounded (e.g. one per conversation, per mind, per user).
A plain ``defaultdict(asyncio.Lock)`` grows forever; this class evicts
the least-recently-used lock once ``maxsize`` is reached so memory stays
bounded over the long lifetime of the daemon.

Originally an internal helper of ``bridge/manager.py``; promoted here so
``cloud/flex.py``, ``cloud/usage.py``, and any future caller can share
the implementation.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Generic, TypeVar

_K = TypeVar("_K")


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
        on insertion. Insertion may evict the oldest entry.
        """
        if key in self._locks:
            self._locks.move_to_end(key)
            return self._locks[key]
        # Evict oldest if at capacity.
        while len(self._locks) >= self._maxsize:
            self._locks.popitem(last=False)
        lock = default if default is not None else asyncio.Lock()
        self._locks[key] = lock
        return lock

    def __getitem__(self, key: _K) -> asyncio.Lock:
        """Return the lock for ``key``, creating + inserting one on miss.

        Mirrors ``defaultdict(asyncio.Lock)`` semantics so existing
        ``async with self._locks[key]:`` patterns work without changes.
        """
        return self.setdefault(key)

    def __len__(self) -> int:
        return len(self._locks)

    def __contains__(self, key: object) -> bool:
        return key in self._locks
