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
* Cancellation drops a closing ``meta.canary.final=True`` so a graceful
  shutdown leaves a "we stopped on purpose" anchor in the stream.

The loop is registered with :func:`sovyx.observability.tasks.spawn` from
:mod:`sovyx.engine.bootstrap` (Phase 11+ Task 11+.10) so it appears in
the TaskRegistry and is cancelled cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
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

    def stop(self) -> None:
        """Signal the loop to exit on its next wake-up."""
        self._stop_event.set()

    async def run(self) -> None:
        """Background loop body — call via ``spawn()``.

        Wakes every ``canary_interval_seconds``, increments the
        monotonic ``tick_id``, and emits ``meta.canary.tick``. Honours
        cancellation by emitting a final tick with ``meta.canary.final=True``
        so the stream ends with an explicit shutdown marker rather than
        an unobserved silence.
        """
        interval = max(1, int(self._config.tuning.canary_interval_seconds))
        logger.info("meta.canary.started", **{"meta.canary.interval_seconds": interval})
        try:
            while not self._stop_event.is_set():
                self._emit_tick(final=False)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            self._emit_tick(final=True)
            raise
        else:
            self._emit_tick(final=True)
        finally:
            logger.info("meta.canary.stopped")

    def _emit_tick(self, *, final: bool) -> None:
        """Emit a single ``meta.canary.tick`` record with the next ``tick_id``.

        The wall-clock timestamp is formatted in ISO 8601 UTC with
        microsecond precision so the operator script can compute
        emit→ingest latency without parsing the envelope ``timestamp``
        (which carries the same instant via the structlog processor —
        the duplication is intentional, in case the envelope's
        timestamp is rewritten by a downstream forwarder).
        """
        self._tick_id += 1
        logger.info(
            "meta.canary.tick",
            **{
                "meta.canary.tick_id": self._tick_id,
                "meta.canary.timestamp": datetime.now(UTC).isoformat(),
                "meta.canary.final": final,
            },
        )


__all__ = ["CanaryEmitter"]
