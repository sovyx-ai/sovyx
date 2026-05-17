"""TTL re-surface scheduler for operator-ack expiry.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 3 §T3.5.

A periodic background task that scans :class:`OperatorAcksStore` for
expired acks, removes them, and emits one
``voice.degraded_banner.resurfaced`` event per removed record so
dashboards see the banner re-surface within one poll interval (5 s
per :func:`useEngineDegradedPoller`).

Design:

* **Cadence**: 30 s default. Long enough that the scheduler isn't a
  noticeable system load; short enough that the operator sees the
  banner re-surface within ``ack_ttl_sec + 30 s`` worst-case
  (well within the F5 falsifiability gate's ``± 5 s`` tolerance
  because TTLs are typically ≥ 60 s).
* **Kill-switch**: disabled when the OperatorAcksStore is unavailable
  (pre-Phase-3 hosts during rollback). The scheduler logs DEBUG +
  short-circuits cleanly.
* **Idempotent**: bulk-removes per cycle via
  :meth:`OperatorAcksStore.prune_expired`; no per-row race because
  the prune query holds the write lock for the duration.

Anti-pattern compliance:

* #14 — every iteration awaits the prune (no time.sleep blocking).
* #15 — bounded by the OperatorAcksStore's cardinality (≤ 8 typical).
* #34 — kill-switch via :attr:`VoiceTuningConfig.degraded_banner_resurface_enabled`
  default-ON: the scheduler is ALWAYS started when the store is
  present; operators can disable via the env var. Default-ON because
  the feature IS the re-surfacing behavior — disabling it means acks
  become permanent (operator must restart sovyx to clear).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine._operator_acks_store import OperatorAcksStore

logger = get_logger(__name__)

DEFAULT_PRUNE_INTERVAL_S = 30.0


class AckResurfaceScheduler:
    """Background scanner for ack expiry → re-surface telemetry.

    Owned by the engine lifecycle. Started after OperatorAcksStore is
    registered; stopped before the registry shuts down. The store is
    passed at construction time (registry resolution at start-time
    would race the bootstrap lifecycle).
    """

    def __init__(
        self,
        acks_store: OperatorAcksStore,
        *,
        prune_interval_s: float = DEFAULT_PRUNE_INTERVAL_S,
    ) -> None:
        self._store = acks_store
        self._interval_s = max(5.0, prune_interval_s)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._loop(),
            name="voice-ack-resurface-scheduler",
        )
        logger.info(
            "voice.degraded_banner.resurface_scheduler_started",
            **{"voice.interval_s": self._interval_s},
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, timeout=5.0)
        self._task = None
        logger.info("voice.degraded_banner.resurface_scheduler_stopped")

    async def shutdown(self) -> None:
        """Alias for :meth:`stop` so :meth:`ServiceRegistry.shutdown_all`
        invokes our teardown without an explicit lifecycle wiring."""
        await self.stop()

    async def tick_once(self) -> int:
        """Single prune pass — public for tests + manual triggers.

        Returns the count of removed (re-surfaced) records.
        """
        try:
            removed = await self._store.prune_expired()
        except Exception:  # noqa: BLE001 — scheduler must not crash
            logger.warning(
                "voice.degraded_banner.resurface_prune_failed",
                exc_info=True,
            )
            return 0
        now_ts = int(time.time())
        for record in removed:
            logger.info(
                "voice.degraded_banner.resurfaced",
                **{
                    "voice.reason": record.reason,
                    "voice.acked_at_ts": int(record.acked_at_ts),
                    "voice.ttl_sec": int(record.ttl_sec),
                    "voice.expired_at_ts": int(
                        record.acked_at_ts + record.ttl_sec,
                    ),
                    "voice.now_ts": now_ts,
                    "voice.operator_id": record.operator_id or "",
                },
            )
        return len(removed)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "voice.degraded_banner.resurface_loop_tick_failed",
                    exc_info=True,
                )
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                raise


__all__ = ["AckResurfaceScheduler", "DEFAULT_PRUNE_INTERVAL_S"]
