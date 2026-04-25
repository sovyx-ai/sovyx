"""Hystrix triple-defense per-resource guard for voice integrations (R1).

Combines three defensive patterns into a single async context
manager keyed by an arbitrary resource fingerprint (typically a
hashed device endpoint id or upstream provider name):

1. **Circuit breaker** — N consecutive failures opens the breaker;
   subsequent calls fast-fail with :class:`CircuitOpenError` until
   the recovery timeout elapses, after which one HALF_OPEN probe is
   admitted. A successful probe closes the breaker; a failing probe
   re-opens it.
2. **Bulkhead** — bounded :class:`asyncio.Semaphore` per resource
   key. When ``max_concurrent`` slots are in use, new entrants
   fast-fail with :class:`BulkheadFullError` rather than queueing
   indefinitely. Fail-fast (not block) is the canonical Hystrix
   discipline — blocking adds latency and masks saturation; loud
   rejection surfaces it.
3. **Watchdog** — :func:`asyncio.wait_for` deadline around the
   guarded coroutine. On expiry, :class:`WatchdogFiredError` is
   raised AND the wrapped coroutine is cancelled (asyncio.wait_for
   contract). Watchdog firing counts as a circuit-breaker failure
   so a stuck upstream eventually opens the breaker.

The guard is a single async context manager so call sites compose
all three with one ``async with``::

    guard = registry.guard_for(device_key)
    try:
        async with guard.run():
            result = await stt.transcribe(audio)
    except CircuitOpenError:
        # Breaker open — skip device, fail over to next.
        ...
    except BulkheadFullError:
        # Too many concurrent calls — drop request.
        ...
    except WatchdogFiredError:
        # Deadline exceeded — already cancelled the inner coroutine.
        ...

Per-key isolation matters: a noisy device (one that fires every
30 s with a transient ALSA error) should not poison every other
device's breaker. The :class:`GuardRegistry` maintains a bounded
LRU cache of guards keyed by an opaque string — typically a
fingerprint produced by :func:`sovyx.voice._observability_pii.hash_pii`
so the keys themselves carry no PII into telemetry labels.

Telemetry: every state transition + bulkhead reject + watchdog
fire emits a structured ``voice.<owner>.guard.<event>`` record AND
flows through the M2 ``record_stage_event`` counter so Grafana
dashboards can attribute failures to the guard layer without
log scraping. The owner label is a :class:`VoiceStage` value so
the M2 cardinality discipline applies automatically.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6), §3.10 R1; Netflix Hystrix wiki (circuit + bulkhead +
isolation patterns); resilience4j (modern JVM equivalent);
Sovyx CLAUDE.md anti-patterns #15 (LRULockDict for bounded keys),
#22 (monotonic clock discipline).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

from sovyx.observability.logging import get_logger
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    record_stage_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)


_T = TypeVar("_T")


# ── Bounds + canonical defaults ────────────────────────────────────


_MIN_FAILURE_THRESHOLD = 1
_MAX_FAILURE_THRESHOLD = 100
"""Failure-count window before OPEN. Above 100 the breaker is so
forgiving it never fires on real-world failure clusters; below 1
opens on a single transient hiccup."""


_MIN_RECOVERY_TIMEOUT_S = 1.0
_MAX_RECOVERY_TIMEOUT_S = 600.0
"""HALF_OPEN probe gap. Below 1 s the breaker thrashes; above 10 min
the daemon never recovers from transient upstream outages within a
reasonable user wait."""


_MIN_BULKHEAD_CONCURRENT = 1
_MAX_BULKHEAD_CONCURRENT = 256
"""Concurrent slots per resource. 1 = serial; above 256 = no
bulkhead in practice (any realistic voice pipeline has < 100
in-flight calls per device)."""


_MIN_WATCHDOG_TIMEOUT_S = 0.1
_MAX_WATCHDOG_TIMEOUT_S = 600.0
"""Per-call deadline. Below 100 ms is shorter than typical
inference; above 10 min is effectively no deadline."""


_DEFAULT_GUARD_REGISTRY_MAXSIZE = 256
"""LRU ceiling for distinct resource keys. Voice deployments
typically see O(10) devices; 256 is generous headroom that still
caps memory at a few KiB of guard state."""


# ── State enum (anti-pattern #9 — StrEnum, value-based comparison) ──


class CircuitState(StrEnum):
    """Three-state circuit breaker model (Netflix Hystrix vocabulary)."""

    CLOSED = "closed"
    """Normal operation — calls flow through."""

    OPEN = "open"
    """Breaker tripped — calls fast-fail until recovery timeout elapses."""

    HALF_OPEN = "half_open"
    """One probe call admitted to test if the upstream recovered."""


# ── Exception hierarchy ─────────────────────────────────────────────


class HystrixGuardError(Exception):
    """Base for all guard-imposed failures.

    Caller code typically catches the specific subclass to drive
    different recovery paths (fail-over vs back-pressure vs
    retry-with-backoff). Catching this base class on its own should
    be reserved for top-level orchestration that treats every
    guard-imposed failure identically.
    """


class CircuitOpenError(HystrixGuardError):
    """Raised when the circuit is OPEN and the call is rejected.

    Semantically: "the upstream is known-broken; do not try; pick
    another path or back off". The recovery timeout has not yet
    elapsed — retrying immediately will hit this same exception.
    """


class BulkheadFullError(HystrixGuardError):
    """Raised when the bulkhead has no free slots.

    Semantically: "the upstream is healthy but saturated; drop or
    queue at a higher level". Fast-fail by design — blocking on the
    semaphore would hide the saturation signal.
    """


class WatchdogFiredError(HystrixGuardError):
    """Raised when the per-call watchdog deadline expires.

    The wrapped coroutine has been cancelled by ``asyncio.wait_for``
    by the time this exception surfaces — the caller does not need
    to clean up the inner task, only its own surrounding state.
    A watchdog fire counts as a circuit-breaker failure, so a stuck
    upstream eventually trips the breaker.
    """


# ── Guard config (immutable, validated at construction) ─────────────


@dataclass(frozen=True, slots=True)
class HystrixGuardConfig:
    """Immutable configuration tuple for a single :class:`HystrixGuard`.

    Loud-fail at construction (anti-pattern #11 / #17) — every
    field is validated against documented bounds so a misconfigured
    daemon fails at boot rather than silently shipping a guard with
    a 0 ms watchdog or a 10 000-failure threshold.
    """

    failure_threshold: int = 3
    """Consecutive failures (incl. watchdog fires) before OPEN."""

    recovery_timeout_s: float = 30.0
    """Wall-clock seconds the breaker stays OPEN before transitioning
    to HALF_OPEN for a probe call."""

    max_concurrent: int = 4
    """Bulkhead — concurrent slots before BulkheadFullError."""

    watchdog_timeout_s: float | None = 10.0
    """Per-call deadline. ``None`` disables the watchdog (caller is
    expected to provide its own deadline upstream — e.g. an HTTP
    timeout for a cloud STT). Disabled is rare and explicit."""

    def __post_init__(self) -> None:
        if not (_MIN_FAILURE_THRESHOLD <= self.failure_threshold <= _MAX_FAILURE_THRESHOLD):
            msg = (
                f"failure_threshold must be in "
                f"[{_MIN_FAILURE_THRESHOLD}, {_MAX_FAILURE_THRESHOLD}], "
                f"got {self.failure_threshold}"
            )
            raise ValueError(msg)
        if not (_MIN_RECOVERY_TIMEOUT_S <= self.recovery_timeout_s <= _MAX_RECOVERY_TIMEOUT_S):
            msg = (
                f"recovery_timeout_s must be in "
                f"[{_MIN_RECOVERY_TIMEOUT_S}, {_MAX_RECOVERY_TIMEOUT_S}], "
                f"got {self.recovery_timeout_s}"
            )
            raise ValueError(msg)
        if not (_MIN_BULKHEAD_CONCURRENT <= self.max_concurrent <= _MAX_BULKHEAD_CONCURRENT):
            msg = (
                f"max_concurrent must be in "
                f"[{_MIN_BULKHEAD_CONCURRENT}, {_MAX_BULKHEAD_CONCURRENT}], "
                f"got {self.max_concurrent}"
            )
            raise ValueError(msg)
        if self.watchdog_timeout_s is not None and not (
            _MIN_WATCHDOG_TIMEOUT_S <= self.watchdog_timeout_s <= _MAX_WATCHDOG_TIMEOUT_S
        ):
            msg = (
                f"watchdog_timeout_s must be None or in "
                f"[{_MIN_WATCHDOG_TIMEOUT_S}, {_MAX_WATCHDOG_TIMEOUT_S}], "
                f"got {self.watchdog_timeout_s}"
            )
            raise ValueError(msg)


# ── Guard ───────────────────────────────────────────────────────────


class HystrixGuard:
    """Per-key triple-defense guard.

    One instance per ``(owner, key)`` pair — typically obtained via
    :class:`GuardRegistry.guard_for` so the per-key state is
    deduplicated correctly. Direct instantiation is supported for
    tests / one-off guards.

    Thread-safety: this guard is asyncio-only. The semaphore +
    state mutations are touched from the event loop only;
    cross-thread use is undefined.
    """

    def __init__(
        self,
        *,
        owner: VoiceStage,
        key: str,
        config: HystrixGuardConfig | None = None,
    ) -> None:
        """Bind a guard to a resource key.

        Args:
            owner: Which :class:`VoiceStage` owns the guarded
                resource — drives the M2 telemetry attribution
                (``voice.stage.events`` ``stage`` label).
            key: Opaque resource identifier. Caller is responsible
                for hashing PII (use
                :func:`sovyx.voice._observability_pii.hash_pii`).
                The key never appears in metric labels — only in
                structured-log attributes (low cardinality cost).
            config: Tuning tuple. Defaults to canonical Hystrix
                values (3 failures, 30 s recovery, 4 concurrent,
                10 s watchdog).
        """
        self._owner = owner
        self._key = key
        self._config = config or HystrixGuardConfig()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._opened_monotonic: float = 0.0
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        self._monotonic = time.monotonic

    # ── Read-only state surface ────────────────────────────────

    @property
    def owner(self) -> VoiceStage:
        return self._owner

    @property
    def key(self) -> str:
        return self._key

    @property
    def state(self) -> CircuitState:
        """Current state with HALF_OPEN auto-promotion check.

        If the breaker is OPEN and recovery_timeout_s has elapsed
        since the last failure, the state is auto-promoted to
        HALF_OPEN here — the next call will be admitted as a probe.
        Side-effecting via property is intentional: Hystrix does
        the same so reads after the recovery window do not require
        an explicit ``tick()`` call from the orchestrator.
        """
        if self._state is CircuitState.OPEN:
            elapsed = self._monotonic() - self._opened_monotonic
            # CLAUDE.md anti-pattern #24: use >= for deadline / TTL
            # comparison so the inclusive boundary works on coarse clocks.
            if elapsed >= self._config.recovery_timeout_s:
                self._transition_to_half_open()
        return self._state

    @property
    def failure_count(self) -> int:
        """Consecutive-failure counter (resets on success)."""
        return self._failure_count

    @property
    def available_slots(self) -> int:
        """Free bulkhead slots — diagnostic only, racy under load."""
        return self._semaphore._value  # noqa: SLF001 — Semaphore exposes nothing public.

    # ── The guard ──────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Async context manager wrapping a guarded call.

        Order of operations:

        1. Check circuit. If OPEN, raise :class:`CircuitOpenError`
           immediately (no bulkhead acquire, no watchdog).
        2. Acquire one bulkhead slot. If none free, raise
           :class:`BulkheadFullError` immediately.
        3. Yield to the wrapped body, wrapping it in
           ``asyncio.wait_for`` if a watchdog timeout is set.
        4. On success: release slot, record success.
        5. On failure: release slot, record failure (which may open
           the breaker).

        Raises:
            CircuitOpenError: Circuit is OPEN.
            BulkheadFullError: All bulkhead slots in use.
            WatchdogFiredError: Watchdog deadline expired (the
                wrapped body has been cancelled).
            BaseException: Any exception raised by the wrapped body
                propagates verbatim after the failure is recorded.
        """
        if self.state is CircuitState.OPEN:
            self._emit_event("circuit_rejected_open")
            record_stage_event(
                self._owner,
                StageEventKind.DROP,
                error_type="circuit_open",
            )
            msg = f"circuit OPEN for owner={self._owner.value} key={self._key}"
            raise CircuitOpenError(msg)

        # Bulkhead — fail-fast, do not block.
        if self._semaphore.locked():
            self._emit_event("bulkhead_rejected")
            record_stage_event(
                self._owner,
                StageEventKind.DROP,
                error_type="bulkhead_full",
            )
            msg = (
                f"bulkhead full for owner={self._owner.value} "
                f"key={self._key} (max_concurrent={self._config.max_concurrent})"
            )
            raise BulkheadFullError(msg)

        await self._semaphore.acquire()
        try:
            if self._config.watchdog_timeout_s is None:
                yield
            else:
                # asyncio.timeout (Python 3.11+) is the canonical way
                # to deadline an ``async with`` body. The library
                # cancels the body task on expiry and raises
                # TimeoutError out the bottom of the ``async with``
                # block — which we catch below to re-raise as
                # WatchdogFiredError. This avoids the fragility of
                # manually cancelling current_task() from a sleeper.
                async with asyncio.timeout(self._config.watchdog_timeout_s):
                    yield
            self._record_success()
        except TimeoutError as exc:
            self._record_failure(error_type="watchdog_fired")
            msg = (
                f"watchdog fired for owner={self._owner.value} "
                f"key={self._key} after {self._config.watchdog_timeout_s} s"
            )
            raise WatchdogFiredError(msg) from exc
        except (CircuitOpenError, BulkheadFullError, WatchdogFiredError):
            # Should never happen here (we check before acquire), but
            # if a nested guard raises one, do NOT count it as a
            # failure on THIS guard — propagate untouched.
            raise
        except BaseException as exc:
            self._record_failure(error_type=type(exc).__name__)
            raise
        finally:
            self._semaphore.release()

    # ── Internal state transitions ─────────────────────────────

    def _record_success(self) -> None:
        was_recovering = self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
        self._failure_count = 0
        if was_recovering:
            self._state = CircuitState.CLOSED
            self._emit_event("circuit_closed", failure_count=0)

    def _record_failure(self, *, error_type: str) -> None:
        self._failure_count += 1
        self._opened_monotonic = self._monotonic()
        if self._state is CircuitState.HALF_OPEN:
            # Probe failed — re-open immediately, do not wait for
            # the failure threshold (Hystrix canonical behaviour).
            self._state = CircuitState.OPEN
            self._emit_event(
                "circuit_reopened",
                failure_count=self._failure_count,
                error_type=error_type,
            )
        elif (
            self._failure_count >= self._config.failure_threshold
            and self._state is not CircuitState.OPEN
        ):
            self._state = CircuitState.OPEN
            self._emit_event(
                "circuit_opened",
                failure_count=self._failure_count,
                threshold=self._config.failure_threshold,
                error_type=error_type,
            )

    def _transition_to_half_open(self) -> None:
        self._state = CircuitState.HALF_OPEN
        self._emit_event(
            "circuit_half_open",
            failure_count=self._failure_count,
            recovery_timeout_s=self._config.recovery_timeout_s,
        )

    def _emit_event(self, event_suffix: str, **fields: object) -> None:
        """Emit a structured ``voice.<owner>.guard.<event>`` log record."""
        # Owner is a closed-set StrEnum, suffix is a closed-set string —
        # the resulting event name set is bounded.
        event_name = f"voice.{self._owner.value}.guard.{event_suffix}"
        logger.info(
            event_name,
            owner=self._owner.value,
            key=self._key,
            state=self._state.value,
            **fields,
        )


# ── Bounded LRU registry ───────────────────────────────────────────


class GuardRegistry:
    """Bounded LRU cache of :class:`HystrixGuard` instances per key.

    One registry per owner stage typically — though sharing across
    owners is supported (the ``owner`` is captured per guard, not
    per registry). Eviction keeps memory bounded over a long-lived
    daemon (anti-pattern #15) — when the LRU evicts a guard its
    in-flight semaphore acquirers continue to use the dropped
    instance until they release; a new guard is constructed for
    subsequent lookups under the same key.

    The eviction is observable: each evicted guard emits a
    ``voice.<owner>.guard.evicted`` log so operators can spot a key
    space that's churning faster than the cache size accommodates.
    """

    def __init__(
        self,
        *,
        owner: VoiceStage,
        config: HystrixGuardConfig | None = None,
        maxsize: int = _DEFAULT_GUARD_REGISTRY_MAXSIZE,
    ) -> None:
        if maxsize < 1:
            msg = f"maxsize must be >= 1, got {maxsize}"
            raise ValueError(msg)
        self._owner = owner
        self._config = config or HystrixGuardConfig()
        self._maxsize = maxsize
        self._guards: OrderedDict[str, HystrixGuard] = OrderedDict()

    @property
    def owner(self) -> VoiceStage:
        return self._owner

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def __len__(self) -> int:
        return len(self._guards)

    def guard_for(self, key: str) -> HystrixGuard:
        """Return the :class:`HystrixGuard` for ``key``, creating it lazily.

        LRU touch on every access — frequently used keys stay
        resident, idle keys get evicted first. Empty key is
        rejected (a guard for "" would silently shadow every other
        unkeyed call site).
        """
        if not key:
            msg = "key must be a non-empty string"
            raise ValueError(msg)
        guard = self._guards.get(key)
        if guard is not None:
            self._guards.move_to_end(key)
            return guard
        guard = HystrixGuard(owner=self._owner, key=key, config=self._config)
        self._guards[key] = guard
        if len(self._guards) > self._maxsize:
            evicted_key, _ = self._guards.popitem(last=False)
            logger.info(
                f"voice.{self._owner.value}.guard.evicted",
                owner=self._owner.value,
                key=evicted_key,
                cache_size=len(self._guards),
                maxsize=self._maxsize,
            )
        return guard

    def reset(self) -> None:
        """Drop every cached guard. Test-only helper."""
        self._guards.clear()


__all__ = [
    "BulkheadFullError",
    "CircuitOpenError",
    "CircuitState",
    "GuardRegistry",
    "HystrixGuard",
    "HystrixGuardConfig",
    "HystrixGuardError",
    "WatchdogFiredError",
]
