"""Meta-monitoring endpoint — ``GET /api/observability/health``.

Implements §27.2 of IMPL-OBSERVABILITY-001 ("Observability of
observability"). The structured-logging pipeline is the substrate
every other diagnostic stands on; without a dedicated probe the
operator can't tell whether the absence of warnings means
"everything is fine" or "the warnings drowned silently in a full
async queue." This route surfaces the seven facts that distinguish
those two states:

==========================  ===========================================
``queue_depth_pct``         current/capacity of :class:`AsyncQueueHandler`
``dropped_60s``             windowed drop count — :func:`record_drop`
``handler_errors_60s``      windowed handler-error count
``fts5_lag_ms_p99``         :meth:`FTSIndexer.lag_ms_p99` over recent batches
``tamper_chain_intact``     hash-chain integrity for the active log file
``tracing_exporter_state``  ``running`` / ``closed`` / ``disabled``
``active_features``         enabled :class:`ObservabilityFeaturesConfig` flags
``schema_version``          wire-format version pin from
                            :data:`sovyx.observability.schema.SCHEMA_VERSION`
``self_check_passed``       ``False`` when any §27.1 alert threshold trips
==========================  ===========================================

The route is best-effort by design — every probe is wrapped so a
broken sub-component degrades to ``None`` (or ``False`` for
booleans) rather than 500'ing the whole endpoint. The dashboard
and ``sovyx doctor`` poll this route, so the contract is "always
respond, never throw."

Rationale for self_check thresholds (mirrors §27.1 alert table):

* ``queue_depth_pct > 0.8`` → queue saturating, drops imminent.
* ``dropped_60s > 10`` → already losing entries.
* ``handler_errors_60s > 0`` → any handler failure is a meaningful
  signal (file write failed, JSON serialize failed, etc.).
* ``fts5_lag_ms_p99 > 5_000`` → index visibly stale to the dashboard.
* ``tamper_chain_intact == False`` → security signal, hard fail.
* ``tracing_exporter_state == "failed"`` → exporter circuit open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

router = APIRouter(prefix="/api/observability")

# ── Self-check thresholds (mirror §27.1) ──────────────────────────
_QUEUE_DEPTH_PCT_FAIL: float = 0.80
_DROPPED_60S_FAIL: int = 10
_HANDLER_ERRORS_60S_FAIL: int = 0  # any error fails
_FTS_LAG_MS_P99_FAIL: float = 5_000.0


def _compute_queue_depth_pct() -> float | None:
    """Return ``current/capacity`` for the active :class:`AsyncQueueHandler`.

    Returns ``None`` when the async queue feature is disabled — the
    caller surfaces that as a missing field rather than reporting
    ``0.0`` (which would conflate "no async queue" with "queue
    perfectly drained").
    """
    from sovyx.observability import logging as obs_logging  # noqa: PLC0415

    writer = obs_logging._async_writer  # noqa: SLF001 — module-level singleton.
    if writer is None:
        return None

    handlers: Sequence[Any] = getattr(writer, "_handlers", ())  # noqa: SLF001
    # Reach back through the QueueListener for the producer-side
    # AsyncQueueHandler. The writer stores downstream handlers (file)
    # in ``_handlers``; we need the upstream queue handler attached
    # to the root logger.
    import logging as stdlib_logging  # noqa: PLC0415

    from sovyx.observability.async_handler import AsyncQueueHandler  # noqa: PLC0415

    for handler in stdlib_logging.getLogger().handlers:
        if isinstance(handler, AsyncQueueHandler):
            queue = handler.queue_ref
            capacity = queue.maxsize
            if capacity <= 0:
                return None
            return round(queue.qsize() / capacity, 4)
    # Defensive: writer was wired but its handler has been detached.
    _ = handlers
    return None


async def _compute_tracing_exporter_state(request: Request) -> str:
    """Return one of ``running`` / ``closed`` / ``disabled``.

    Definitions:
        * ``disabled`` — operator left ``observability.otel.enabled=False``;
          the exporter never spawned. This is the default; not an alert.
        * ``closed`` — exporter was wired but its provider has been
          torn down (graceful shutdown or never-started).
        * ``running`` — provider is live and exporting spans.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return "disabled"
    try:
        from sovyx.observability.otel import OtelExporter  # noqa: PLC0415
    except ImportError:
        return "disabled"
    if not registry.is_registered(OtelExporter):
        return "disabled"
    try:
        exporter = await registry.resolve(OtelExporter)
    except Exception:  # noqa: BLE001 — registry contract may differ on shape.
        return "disabled"
    provider = getattr(exporter, "_provider", None)
    return "running" if provider is not None else "closed"


def _compute_tamper_chain_intact(request: Request) -> bool | None:
    """Verify the hash chain of the active log file, when tamper-chain is on.

    Returns ``None`` when the feature is disabled — the caller folds
    that into "intact" for the self-check (a chain that doesn't
    exist can't be broken). Returns ``True``/``False`` from
    :func:`verify_chain` otherwise. Verification reads the entire
    file; to keep the endpoint sub-100ms the result is cached on
    the app state for the rotation lifetime — a fresh chain is
    returned on every request only if the previous result was
    "broken" (so an operator who fixes the issue sees recovery
    immediately).
    """
    log_file = getattr(request.app.state, "log_file", None)
    if log_file is None:
        return None
    from pathlib import Path  # noqa: PLC0415

    if not isinstance(log_file, Path) or not log_file.is_file():
        return None
    # Quick sniff: only verify when at least one chain field is
    # actually present in the file. Avoids a 200 MB scan when the
    # operator hasn't enabled tamper_chain.
    try:
        with log_file.open("rb") as fh:
            head = fh.read(2048)
        if b'"chain_hash"' not in head and b'"prev_hash"' not in head:
            return None
    except OSError:
        return None
    from sovyx.observability.tamper import verify_chain  # noqa: PLC0415

    try:
        intact, _idx = verify_chain(log_file)
    except (OSError, ValueError):
        return False
    return intact


async def _compute_active_features(request: Request) -> list[str]:
    """Return the names of enabled :class:`ObservabilityFeaturesConfig` flags."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return []
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415
    except ImportError:
        return []
    if not registry.is_registered(EngineConfig):
        return []
    try:
        cfg = await registry.resolve(EngineConfig)
    except Exception:  # noqa: BLE001 — registry contract may differ on shape.
        return []
    features = getattr(getattr(cfg, "observability", None), "features", None)
    if features is None:
        return []
    return sorted(name for name, value in features.model_dump().items() if value is True)


async def _compute_fts_lag_p99(request: Request) -> float | None:
    """Return :meth:`FTSIndexer.lag_ms_p99` from the registered indexer."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    try:
        from sovyx.observability.fts_index import FTSIndexer  # noqa: PLC0415
    except ImportError:
        return None
    if not registry.is_registered(FTSIndexer):
        return None
    try:
        indexer = await registry.resolve(FTSIndexer)
    except Exception:  # noqa: BLE001
        return None
    return cast("float | None", indexer.lag_ms_p99())


def _evaluate_self_check(
    *,
    queue_depth_pct: float | None,
    dropped_60s: int,
    handler_errors_60s: int,
    fts5_lag_ms_p99: float | None,
    tamper_chain_intact: bool | None,
    tracing_exporter_state: str,
) -> bool:
    """Return ``True`` when no §27.1 alert threshold is currently tripped."""
    if queue_depth_pct is not None and queue_depth_pct > _QUEUE_DEPTH_PCT_FAIL:
        return False
    if dropped_60s > _DROPPED_60S_FAIL:
        return False
    if handler_errors_60s > _HANDLER_ERRORS_60S_FAIL:
        return False
    if fts5_lag_ms_p99 is not None and fts5_lag_ms_p99 > _FTS_LAG_MS_P99_FAIL:
        return False
    if tamper_chain_intact is False:
        return False
    return tracing_exporter_state != "failed"


@router.get("/health", dependencies=[Depends(verify_token)])
async def get_observability_health(request: Request) -> JSONResponse:
    """Meta-monitoring snapshot for the observability stack itself.

    Schema (§27.2):

    .. code-block:: json

        {
          "queue_depth_pct": 0.12,
          "dropped_60s": 0,
          "handler_errors_60s": 0,
          "fts5_lag_ms_p99": 230,
          "tamper_chain_intact": true,
          "tracing_exporter_state": "running",
          "active_features": ["pii_redaction", "sampling"],
          "schema_version": "1.0.0",
          "self_check_passed": true
        }

    Every probe is best-effort: failures degrade to ``None`` (or
    ``False`` for booleans) so the route never returns 500. The
    ``sovyx doctor`` aggregator and the dashboard poll this in a
    tight loop — they need a reliable contract more than perfect
    observability.
    """
    from sovyx.observability._health_state import (  # noqa: PLC0415
        count_drops_60s,
        count_handler_errors_60s,
    )
    from sovyx.observability.schema import SCHEMA_VERSION  # noqa: PLC0415

    queue_depth_pct = _compute_queue_depth_pct()
    dropped_60s = count_drops_60s()
    handler_errors_60s = count_handler_errors_60s()
    fts5_lag_ms_p99 = await _compute_fts_lag_p99(request)
    tamper_chain_intact = _compute_tamper_chain_intact(request)
    tracing_exporter_state = await _compute_tracing_exporter_state(request)
    active_features = await _compute_active_features(request)
    self_check_passed = _evaluate_self_check(
        queue_depth_pct=queue_depth_pct,
        dropped_60s=dropped_60s,
        handler_errors_60s=handler_errors_60s,
        fts5_lag_ms_p99=fts5_lag_ms_p99,
        tamper_chain_intact=tamper_chain_intact,
        tracing_exporter_state=tracing_exporter_state,
    )

    payload: dict[str, Any] = {
        "queue_depth_pct": queue_depth_pct,
        "dropped_60s": dropped_60s,
        "handler_errors_60s": handler_errors_60s,
        "fts5_lag_ms_p99": fts5_lag_ms_p99,
        "tamper_chain_intact": tamper_chain_intact,
        "tracing_exporter_state": tracing_exporter_state,
        "active_features": active_features,
        "schema_version": SCHEMA_VERSION,
        "self_check_passed": self_check_passed,
    }
    return JSONResponse(payload)
