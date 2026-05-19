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

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability._resource_registry import (
    _HEALTH_SNAPSHOT_FIELDS,
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


__all__ = [
    "EngineResourcesResponse",
    "ResourceCohortMetrics",
    "_build_response",
    "router",
]
