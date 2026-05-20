"""Engine resource-hygiene surface — operator-actionable snapshot of the
ResourceCohortGovernor inputs.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T3.1.

Closes the operator-visibility half of forensic-audit §H4. Pre-mission,
the +1.1 GB RSS / +105 thread spike at L909 left operators with ONLY
raw ``self.health.snapshot`` log lines + zero structured API surface
to query the resource state from the dashboard / CLI / external
monitoring.

This route exposes the 28 canonical snapshot fields documented in
:data:`sovyx.observability._resource_registry._HEALTH_SNAPSHOT_FIELDS`
as a structured JSON response with a stable forward-additive schema
(``extra="allow"`` per CLAUDE.md anti-pattern #40). The Phase 1.D
ResourceCohortGovernor enrichment (budget verdicts, heap-snapshot
manifests, circuit-breaker state) layers on top of this baseline
without a schema migration.

Anti-pattern compliance:

* #18 — exposed via ``api.*`` JSON helper on the frontend (no raw
  ``fetch()`` consumers — frontend zod twin lives at
  ``dashboard/src/types/schemas.ts``).
* #40 — paired Quality Gate 8 round-trip test at
  ``tests/integration/dashboard/test_engine_resources_boundary.py``
  asserts the producer dict shape (``ResourceRegistry.snapshot_fields()``)
  validates cleanly through ``EngineResourcesResponse.model_validate``.
* #42 — single endpoint surface; future cohort-governor producers
  read this endpoint rather than emitting their own.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.status import HTTP_404_NOT_FOUND

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability._resource_cohort_governor import (
    get_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import (
    _HEALTH_SNAPSHOT_FIELDS,
    CohortAxis,
    get_default_resource_registry,
)
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/engine", dependencies=[Depends(verify_token)])


class ResourceCohortMetrics(BaseModel):
    """Per-cohort registry metrics block.

    Mirrors the H4 fields under ``to_thread.*``, ``lock_dict.*``,
    ``onnx.*``, ``gc.*``, ``tracemalloc.*``, ``exception_cohort.*``
    sections of :data:`_HEALTH_SNAPSHOT_FIELDS`.

    Forward-additive: ``extra="allow"`` permits Phase 1.D / future H4
    extensions to add fields without breaking older clients.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # to_thread
    to_thread_pool_size: int = Field(0, alias="to_thread.pool_size")
    # Mission H4 §3 F2 + §22 v0.49.32 — canonical alias of pool_size per
    # spec literal naming (Python ThreadPoolExecutor exposes only
    # ``len(_threads)`` so both fields surface the same source counter).
    to_thread_active_workers: int = Field(0, alias="to_thread.active_workers")
    to_thread_queue_depth: int = Field(0, alias="to_thread.queue_depth")
    to_thread_max_workers: int = Field(0, alias="to_thread.max_workers")
    to_thread_dispatch_count_total: int = Field(0, alias="to_thread.dispatch_count_total")
    to_thread_dispatch_count_per_label: dict[str, int] = Field(
        default_factory=dict, alias="to_thread.dispatch_count_per_label"
    )
    # lock_dict
    lock_dict_total_cardinality: int = Field(0, alias="lock_dict.total_cardinality")
    lock_dict_per_owner: dict[str, int] = Field(default_factory=dict, alias="lock_dict.per_owner")
    lock_dict_instance_count: int = Field(0, alias="lock_dict.instance_count")
    # onnx
    onnx_session_count: int = Field(0, alias="onnx.session_count")
    onnx_session_labels: list[str] = Field(default_factory=list, alias="onnx.session_labels")
    # gc
    gc_collections_by_gen: list[int] = Field(default_factory=list, alias="gc.collections_by_gen")
    gc_objects_count: int = Field(0, alias="gc.objects_count")
    # tracemalloc
    tracemalloc_is_tracing: bool = Field(False, alias="tracemalloc.is_tracing")
    tracemalloc_current_kb: int = Field(0, alias="tracemalloc.current_kb")
    tracemalloc_peak_kb: int = Field(0, alias="tracemalloc.peak_kb")
    # exception_cohort
    exception_cohort_retained_bytes_estimate: int = Field(
        0, alias="exception_cohort.retained_bytes_estimate"
    )
    exception_cohort_distinct_group_id_count: int = Field(
        0, alias="exception_cohort.distinct_group_id_count"
    )
    exception_cohort_last_observation_monotonic: float = Field(
        0.0, alias="exception_cohort.last_observation_monotonic"
    )


class EngineResourcesResponse(BaseModel):
    """Single-shot snapshot of the engine resource state.

    Field-name shape mirrors :data:`_HEALTH_SNAPSHOT_FIELDS` for
    consumer parity with the structured log stream — operators can
    correlate a ``GET /api/engine/resources`` response with the
    in-process ``self.health.snapshot`` records by field key.

    Forward-additive via ``extra="allow"`` (anti-pattern #40 — Quality
    Gate 8 verifies the producer-to-boundary round-trip).
    """

    model_config = ConfigDict(extra="allow")

    observed_at_unix: float
    cohorts: ResourceCohortMetrics
    canonical_field_count: int
    legacy_alias_count: int


def _build_response() -> EngineResourcesResponse:
    """Assemble the live snapshot response from the resource registry."""
    registry = get_default_resource_registry()
    fields = registry.snapshot_fields()
    # ResourceCohortMetrics consumes the dotted-key shape directly via
    # `populate_by_name`. We pass the dict as-is so any extra keys
    # forward-additively land in `extra`.
    cohorts = ResourceCohortMetrics.model_validate(fields)
    canonical_count = sum(
        1 for spec in _HEALTH_SNAPSHOT_FIELDS.values() if spec.legacy_alias is None
    )
    legacy_count = sum(
        1 for spec in _HEALTH_SNAPSHOT_FIELDS.values() if spec.legacy_alias is not None
    )
    return EngineResourcesResponse(
        observed_at_unix=time.time(),
        cohorts=cohorts,
        canonical_field_count=canonical_count,
        legacy_alias_count=legacy_count,
    )


@router.get("/resources", response_model=EngineResourcesResponse)
async def get_engine_resources() -> EngineResourcesResponse:
    """Return the live engine resource cohort snapshot.

    Mission H4 §T3.1. The endpoint:

    * Reads the in-process :class:`ResourceRegistry` singleton.
    * Builds a forward-additive pydantic envelope with per-cohort
      counters (ONNX session count, LRULockDict cardinality,
      asyncio.to_thread dispatch counts, gc / tracemalloc /
      exception_cohort retention).
    * Returns the JSON envelope alongside the canonical /
      legacy-alias field counts so operators can verify the SSoT
      ↔ producer parity from a single response.

    The Phase 1.D ResourceCohortGovernor extends this response with
    budget verdicts + heap-snapshot manifests + circuit-breaker
    state — ``extra="allow"`` preserves backward compat for clients
    pinned to the Phase 1.C schema.
    """
    return _build_response()


class CohortAckRequest(BaseModel):
    """Mission H4 §8 T4.1(e) + §ADR-D14 — operator-ack request body.

    Clears the circuit-breaker for ``cohort`` and records an ack
    timestamp in the governor's in-memory state. Operators see this
    chip on the ``<DegradedBanner>`` axis="engine_resources" entry.
    """

    cohort: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="One of CohortAxis values (rss_growth / thread_count / "
        "lock_dict_cardinality / onnx_session / exception_cohort).",
    )


class CohortAckResponse(BaseModel):
    """Acknowledgement response with the new breaker state."""

    cohort: str
    breaker_engaged: bool
    acked_at_unix: float


@router.post(
    "/resources/cohort/ack",
    response_model=CohortAckResponse,
)
async def post_cohort_ack(body: CohortAckRequest) -> CohortAckResponse:
    """Mission H4 §8 T4.1(e) — operator clears the cohort circuit-breaker.

    Per Mission H4 §0 item #12. Mirrors the C4
    ``POST /api/voice/degraded/ack`` shape but routes through the
    governor's in-process state rather than the SQLite ack-store —
    the breach state is per-cohort + ephemeral (governor is process-
    local, restart wipes), not persistent like the voice-degraded acks.

    Validates ``cohort`` against :class:`CohortAxis` and returns 422
    on unknown values; otherwise clears the breaker + returns the new
    state.
    """
    try:
        axis = CohortAxis(body.cohort)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown cohort {body.cohort!r}; valid: {sorted(a.value for a in CohortAxis)}"
            ),
        ) from exc
    governor = get_default_resource_cohort_governor()
    governor.clear_breaker(axis)
    return CohortAckResponse(
        cohort=axis.value,
        breaker_engaged=governor.is_breaker_engaged(axis),
        acked_at_unix=time.time(),
    )


def _diagnostics_dir() -> Path:
    """Resolve ~/.sovyx/diagnostics/ — mirrors the governor's helper."""
    return Path.home() / ".sovyx" / "diagnostics"


@router.get("/resources/heap-snapshot/{timestamp}")
async def get_heap_snapshot(timestamp: int) -> dict[str, object]:
    """Mission H4 §0 item #11 — serve a persisted heap-snapshot JSON.

    The governor persists snapshots to ``~/.sovyx/diagnostics/heap-
    snapshot-<ts>.json`` when ``observability.features.tracemalloc=True``
    and an RSS_GROWTH cohort fires. Rotation keeps the last 10 files;
    older timestamps return 404 (the snapshot was rotated out).

    Path-traversal: ``timestamp`` is constrained to int by FastAPI;
    Path concat is safe — no user-controlled string segments.
    """
    path = _diagnostics_dir() / f"heap-snapshot-{timestamp}.json"
    if not path.is_file():
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"heap-snapshot-{timestamp}.json not found — may have been "
                "rotated past the heap_snapshot_max_files cap (default 10) "
                "OR tracemalloc was not enabled when the cohort fired."
            ),
        )
    try:
        loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "engine.resources.heap_snapshot_read_failed",
            path=str(path),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="heap-snapshot file unreadable") from exc


@router.get("/resources/thread-snapshot/{timestamp}")
async def get_thread_snapshot(timestamp: int) -> dict[str, str]:
    """Mission H4 §0 item — serve a persisted thread-snapshot text file.

    The governor persists snapshots to ``~/.sovyx/diagnostics/thread-
    snapshot-<ts>.txt`` when a THREAD_COUNT cohort fires (no
    tracemalloc gate — thread snapshots are always available via
    ``sys._current_frames()`` + ``threading.enumerate()``).
    """
    path = _diagnostics_dir() / f"thread-snapshot-{timestamp}.txt"
    if not path.is_file():
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"thread-snapshot-{timestamp}.txt not found — may have been "
                "rotated past the thread_snapshot_max_files cap (default 10)."
            ),
        )
    try:
        return {"content": path.read_text(encoding="utf-8"), "timestamp": str(timestamp)}
    except OSError as exc:
        logger.warning(
            "engine.resources.thread_snapshot_read_failed",
            path=str(path),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="thread-snapshot file unreadable") from exc


__all__ = [
    "CohortAckRequest",
    "CohortAckResponse",
    "EngineResourcesResponse",
    "ResourceCohortMetrics",
    "_build_response",
    "router",
]
