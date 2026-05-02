"""Exponential backoff with jitter (band-aid #10 replacement).

Foundation for the F1-inventory band-aid #10: capture path
currently retries with a constant linear delay
(``capture_reconnect_delay_seconds``) after a PortAudio error.
The mission identifies this as a band-aid because:

* **Constant delay is wrong under sustained outages** — a
  driver-side glitch that takes 30 s to clear gets hammered
  every 5 s, generating cascading errors in the system log,
  burning CPU on failed reopens, and racing against the kernel's
  device cleanup.
* **No jitter means correlated retries** — if N Sovyx instances
  on the same host all reopen at the same constant delay, they
  collide on every retry. Jitter desynchronises them.
* **No upper bound means the daemon retries forever** — better
  to escalate to operator after a meaningful time budget so the
  user sees "voice unavailable" instead of silent retry churn.

This module ships the foundation; per the staged-adoption
discipline, the per-site wire-up lands in subsequent commits
(capture reconnect first, model download retry second, etc.).

Why ``engine/`` not ``voice/``: backoff is a cross-cutting
defensive primitive — voice capture reconnect uses it, but so
should model download retry, plugin reconnection, channel
outbound retry, etc. ``engine/`` is the right module-tree home
for cross-component primitives (alongside ``_lock_dict.py``).

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25
band-aid #10 (``_capture_task.py:75``); F1 inventory entry #10
(archived at
`docs-internal/archive/audits-completed/F1-band-aid-inventory-2026-04-25.md`);
AWS exponential backoff guide
(``aws.amazon.com/blogs/architecture/exponential-backoff-and-
jitter/``); CLAUDE.md anti-patterns #9 (StrEnum), #11
(loud-fail bounds), #22/#24 (monotonic clock).
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)


# ── Bound enforcement constants ────────────────────────────────────


_MIN_BASE_DELAY_S = 0.001
_MAX_BASE_DELAY_S = 600.0
"""Initial-attempt delay. Below 1 ms the loop runs hot before
the first sleep; above 10 min the first retry is a user-visible
silent wait."""


_MIN_MAX_DELAY_S = 0.001
_MAX_MAX_DELAY_S = 3600.0
"""Per-attempt ceiling. Above 1 hour the daemon should be
escalating to operator instead of silent retry."""


_MIN_MULTIPLIER = 1.0
_MAX_MULTIPLIER = 10.0
"""Multiplier per attempt. 1.0 = constant (degrades to legacy
band-aid behaviour); above 10 the schedule explodes too fast
to be useful."""


_MIN_MAX_ATTEMPTS = 1
_MAX_MAX_ATTEMPTS = 1_000_000
"""Total attempts before the schedule is exhausted. 1 = single
shot (no retry); 1M is effectively unbounded for any realistic
budget."""


# ── Jitter strategies ─────────────────────────────────────────────


class JitterStrategy(StrEnum):
    """Closed-set vocabulary of jitter strategies.

    StrEnum (anti-pattern #9) — value-based comparison is
    xdist-safe + serialises to the structured-log "strategy"
    field verbatim.

    Why these three (and not "decorrelated", "equal", etc.):
    these are the AWS-canonical strategies that cover every
    realistic Sovyx need. Adding more would dilute the
    operator's mental model without practical benefit.
    """

    NONE = "none"
    """Deterministic schedule. Use when the call site needs
    predictable timing for observability + the correlated-retry
    risk is moot (single Sovyx instance, no peer collisions)."""

    FULL = "full"
    """Per-attempt delay is uniform random in ``[0, computed]``.
    Maximum desynchronisation — N concurrent retriers spread
    evenly across the window. AWS's recommended default for
    most retry scenarios."""

    EQUAL = "equal"
    """Per-attempt delay is uniform random in
    ``[computed/2, computed]``. Less aggressive desynchronisation
    than FULL but tighter latency variance. Use when bound on
    minimum retry delay matters (e.g. token-bucket rate
    limiting on the upstream)."""


# ── Config ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Immutable backoff configuration.

    Loud-fail at construction (anti-pattern #11) — every field
    validated against documented bounds so a misconfigured
    deployment fails at boot rather than retrying with a 0 s
    delay (busy loop) or 10 000 s delay (silent stall).

    Args:
        base_delay_s: Delay for attempt 1 (the first retry,
            before any exponentiation). The schedule's "starting
            point". Bounded ``[0.001, 600]``.
        max_delay_s: Per-attempt delay ceiling. The exponential
            grows ``base × multiplier^(n-1)``; this caps the
            growth so a long retry window doesn't reach
            absurd delays. Bounded ``[0.001, 3600]``.
        multiplier: Growth factor per attempt. 1.0 = constant
            (degenerates to legacy linear retry); 2.0 (default)
            doubles each attempt — AWS canonical. Bounded
            ``[1.0, 10.0]``.
        max_attempts: Schedule length. Past this count the
            schedule iterator is exhausted; caller decides
            whether to give up or restart. Bounded ``[1, 1M]``.
        jitter: Which :class:`JitterStrategy` to apply to each
            computed delay. Default FULL (AWS recommendation).
    """

    base_delay_s: float = 0.5
    max_delay_s: float = 60.0
    multiplier: float = 2.0
    max_attempts: int = 10
    jitter: JitterStrategy = JitterStrategy.FULL

    def __post_init__(self) -> None:
        if not (_MIN_BASE_DELAY_S <= self.base_delay_s <= _MAX_BASE_DELAY_S):
            msg = (
                f"base_delay_s must be in "
                f"[{_MIN_BASE_DELAY_S}, {_MAX_BASE_DELAY_S}], "
                f"got {self.base_delay_s}"
            )
            raise ValueError(msg)
        if not (_MIN_MAX_DELAY_S <= self.max_delay_s <= _MAX_MAX_DELAY_S):
            msg = (
                f"max_delay_s must be in "
                f"[{_MIN_MAX_DELAY_S}, {_MAX_MAX_DELAY_S}], "
                f"got {self.max_delay_s}"
            )
            raise ValueError(msg)
        if self.max_delay_s < self.base_delay_s:
            msg = (
                f"max_delay_s ({self.max_delay_s}) must be >= "
                f"base_delay_s ({self.base_delay_s}) — otherwise "
                f"the ceiling is below the starting point and the "
                f"schedule clamps every attempt to max_delay_s"
            )
            raise ValueError(msg)
        if not (_MIN_MULTIPLIER <= self.multiplier <= _MAX_MULTIPLIER):
            msg = (
                f"multiplier must be in "
                f"[{_MIN_MULTIPLIER}, {_MAX_MULTIPLIER}], "
                f"got {self.multiplier}"
            )
            raise ValueError(msg)
        if not (_MIN_MAX_ATTEMPTS <= self.max_attempts <= _MAX_MAX_ATTEMPTS):
            msg = (
                f"max_attempts must be in "
                f"[{_MIN_MAX_ATTEMPTS}, {_MAX_MAX_ATTEMPTS}], "
                f"got {self.max_attempts}"
            )
            raise ValueError(msg)


# ── Schedule ──────────────────────────────────────────────────────


class BackoffSchedule:
    """Stateful exponential-backoff schedule.

    One instance per retry session. Iterate with :meth:`next`
    to get the next sleep delay (in seconds), or use the
    schedule as an iterator::

        schedule = BackoffSchedule(BackoffPolicy())
        for delay_s in schedule:
            await asyncio.sleep(delay_s)
            try:
                result = await operation()
                break  # success
            except OperationError:
                continue  # next iteration → next delay
        else:
            # schedule exhausted — escalate to operator

    Thread-safe: an internal :class:`threading.Lock` serialises
    counter mutations + RNG access. The hot path is one
    lock-acquire + one float multiplication + (for jittered
    strategies) one ``random.uniform`` call.

    Args:
        policy: Tuning tuple. Defaults to ``BackoffPolicy()`` —
            base 0.5 s, max 60 s, multiplier 2, 10 attempts,
            FULL jitter.
        seed: Optional RNG seed for deterministic test runs.
            ``None`` (the default) uses the module's shared
            :class:`random.Random` instance for non-deterministic
            production behaviour.
    """

    def __init__(
        self,
        policy: BackoffPolicy | None = None,
        *,
        seed: int | None = None,
    ) -> None:
        self._policy = policy or BackoffPolicy()
        self._lock = threading.Lock()
        if seed is None:
            self._rng: random.Random = random.Random()
        else:
            self._rng = random.Random(seed)
        self._attempt_count = 0  # 0 = not started; 1+ = N attempts taken

    @property
    def policy(self) -> BackoffPolicy:
        return self._policy

    @property
    def attempt_count(self) -> int:
        with self._lock:
            return self._attempt_count

    @property
    def exhausted(self) -> bool:
        """True once :attr:`attempt_count` has reached
        :attr:`BackoffPolicy.max_attempts`."""
        with self._lock:
            return self._attempt_count >= self._policy.max_attempts

    def reset(self) -> None:
        """Reset attempt counter to zero. Test-only / explicit-
        restart helper; production code should construct a fresh
        :class:`BackoffSchedule` per retry session."""
        with self._lock:
            self._attempt_count = 0

    def next(self) -> float:
        """Return the next delay in seconds.

        Raises:
            StopIteration: Schedule exhausted (caller has taken
                ``max_attempts`` delays). Use :attr:`exhausted`
                to check before calling, or wrap in
                ``try/except StopIteration`` for the canonical
                "is the schedule done" pattern.
        """
        with self._lock:
            if self._attempt_count >= self._policy.max_attempts:
                raise StopIteration
            n = self._attempt_count
            self._attempt_count += 1
            # Compute the deterministic exponential delay.
            # Attempt 0 → base_delay_s
            # Attempt 1 → base × multiplier
            # Attempt 2 → base × multiplier²
            # ...
            computed = self._policy.base_delay_s * (self._policy.multiplier**n)
            computed = min(computed, self._policy.max_delay_s)
            jittered = self._apply_jitter(computed)
        # Logging outside the lock (logging may yield).
        logger.debug(
            "backoff.next_delay",
            attempt=n + 1,
            max_attempts=self._policy.max_attempts,
            computed_s=round(computed, 3),
            jittered_s=round(jittered, 3),
            jitter=self._policy.jitter.value,
        )
        return jittered

    def __iter__(self) -> Iterator[float]:
        return self

    def __next__(self) -> float:
        return self.next()

    def _apply_jitter(self, computed: float) -> float:
        """Apply the configured :class:`JitterStrategy` to
        ``computed`` (must be called under the lock).

        Returns the jittered delay, always in
        ``[0, computed]`` for FULL, ``[computed/2, computed]``
        for EQUAL, exactly ``computed`` for NONE.
        """
        if self._policy.jitter is JitterStrategy.NONE:
            return computed
        if self._policy.jitter is JitterStrategy.FULL:
            return self._rng.uniform(0.0, computed)
        # EQUAL
        return self._rng.uniform(computed / 2.0, computed)


__all__ = [
    "BackoffPolicy",
    "BackoffSchedule",
    "JitterStrategy",
]
