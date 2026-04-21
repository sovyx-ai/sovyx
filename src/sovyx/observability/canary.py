"""Synthetic-canary heartbeat — :class:`CanaryEmitter`.

Implements §27.3 of IMPL-OBSERVABILITY-001 ("canário sintético"). Every
``canary_interval_seconds`` (default 60 s) the daemon emits a single
``meta.canary.tick`` record carrying a monotonic ``tick_id`` plus a
wall-clock timestamp. An external operator script — outside this repo —
checks that the record reaches the log file (and the SIEM, if a
forwarder is wired) within the expected window. A missing tick is the
trivial signal "the logging pipeline stopped" that distinguishes a
quiet daemon from a dead one.

The emitter is intentionally minimal:

* No external dependencies — emits a single ``logger.info`` call per
  tick. The cost is bounded by the structured-logging path itself
  (envelope + PII + async-queue) which is already exercised by every
  other event in the system.
* Monotonic ``tick_id`` starts at 1 and increments by one per tick. A
  gap in the sequence (or a tick_id reset to 1 mid-stream) is a
  bootstrap event the operator script can correlate with the daemon's
  start time.
* ``meta.lag_ms`` carries the scheduler drift between expected vs
  actual fire. Sustained non-zero drift is the early warning that the
  event loop is starving long before a tick goes missing entirely.
* On cancellation the loop emits one final tick before propagating
  ``CancelledError`` so a graceful shutdown leaves an explicit anchor
  rather than an unobserved silence.

The loop is registered with :func:`sovyx.observability.tasks.spawn` from
:mod:`sovyx.engine.bootstrap` (Phase 11+ Task 11+.10) so it appears in
the TaskRegistry and is cancelled cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import ObservabilityConfig

logger = get_logger(__name__)


class CanaryEmitter:
    """Periodically emit ``meta.canary.tick`` so an external probe can detect log-pipeline outages.

    Wire from bootstrap with :func:`sovyx.observability.tasks.spawn`. The
    interval is read from
    :attr:`ObservabilityTuningConfig.canary_interval_seconds` (default
    60 s, bounded 5–3600 s).
    """

    def __init__(self, observability_config: ObservabilityConfig) -> None:
        self._config = observability_config
        self._stop_event = asyncio.Event()
        self._tick_id = 0
        self._interval_s: float = 0.0
        self._next_tick_at: datetime | None = None

    def stop(self) -> None:
        """Signal the loop to exit on its next wake-up."""
        self._stop_event.set()

    async def run(self) -> None:
        """Background loop body — call via ``spawn()``.

        Wakes every ``canary_interval_seconds``, increments the
        monotonic ``tick_id``, and emits ``meta.canary.tick``. Honours
        cancellation by emitting a final tick so the stream ends with
        an explicit shutdown marker rather than an unobserved silence.
        """
        self._interval_s = max(1.0, float(self._config.tuning.canary_interval_seconds))
        logger.info(
            "meta.canary.started",
            **{"meta.canary.interval_seconds": int(self._interval_s)},
        )
        try:
            while not self._stop_event.is_set():
                self._emit_tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
        except asyncio.CancelledError:
            self._emit_tick()
            raise
        else:
            self._emit_tick()
        finally:
            logger.info("meta.canary.stopped")

    def _emit_tick(self) -> None:
        """Emit a single ``meta.canary.tick`` record with the next ``tick_id``.

        Schema fields (per ``log_schema/meta.canary.tick.json``):

        * ``meta.tick_id``  — monotonic counter starting at 1.
        * ``meta.timestamp`` — ISO-8601 UTC wall clock at emit. Duplicates
          the envelope ``timestamp`` on purpose: a downstream forwarder
          may rewrite the envelope timestamp during ingest, but the
          operator script needs the *original* emit instant to compute
          end-to-end latency.
        * ``meta.lag_ms`` — drift between when the tick was scheduled
          (previous tick + ``interval``) and when it actually fired.
          ``0.0`` on the very first tick (no scheduled-at to compare).
          Sustained non-zero lag is the early warning that the event
          loop is starving — long before a tick goes missing entirely.
        """
        now = datetime.now(UTC)
        if self._next_tick_at is None:
            lag_ms = 0.0
        else:
            lag_ms = max(0.0, (now - self._next_tick_at).total_seconds() * 1000.0)

        self._tick_id += 1
        logger.info(
            "meta.canary.tick",
            **{
                "meta.tick_id": self._tick_id,
                "meta.timestamp": now.isoformat(),
                "meta.lag_ms": lag_ms,
            },
        )

        self._next_tick_at = now + timedelta(seconds=self._interval_s)


__all__ = ["CanaryEmitter"]
