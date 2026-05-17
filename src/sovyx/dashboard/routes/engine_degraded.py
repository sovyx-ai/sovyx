"""Composite engine-degraded surface across voice + LLM + STT axes.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.6.

Replaces N independent log-grep workflows with one actionable payload.
Operators no longer need to correlate `bootstrap.py:735`
``no_llm_provider_detected`` + `voice/factory/_validate.py:542`
``voice.factory.stt_language_unsupported`` + `voice/health/_runtime_failover.py`
``voice.failover.ladder_complete{verdict=exhausted}`` by hand — the
composite endpoint surfaces all three (and any future degraded axis)
in one payload + drives the global dashboard banner.

Severity escalation per ADR-D6:

* 0 axes → ``composite_severity = None`` (banner hidden).
* 1 axis  → ``"warn"``.
* 2 axes → ``"error"``.
* 3+ axes OR auto-restart governor exhausted (Phase 2 §T2.2) →
  ``"critical"``.

Anti-pattern compliance:

* #18 — exposed through ``api.*`` JSON helper on the frontend
  (no raw ``fetch()`` consumers).
* #40 — paired Quality Gate 8 round-trip test at
  ``tests/dashboard/test_engine_degraded_boundary.py``.
* #42 — single composite surface; producers MUST consult
  :mod:`sovyx.engine._degraded_store` rather than emit independent
  log lines the operator must correlate.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
)
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/engine", dependencies=[Depends(verify_token)])


class ActionChipModel(BaseModel):
    """Operator-actionable button chip rendered inside the banner.

    Mirror of :class:`sovyx.engine._degraded_store.ActionChip` — the
    pydantic-side declaration keeps the OpenAPI schema explicit + the
    frontend zod twin (``ActionChipSchema`` in
    ``dashboard/src/types/schemas.ts``) gets a clean type to lock onto.
    """

    model_config = {"extra": "allow"}

    label_token: str
    action: str
    target: str
    style: str = "default"


class DegradedAxisModel(BaseModel):
    """One axis entry in the composite payload.

    Forward-additive: future axes (brain.embedding_model_unavailable,
    bridges.channel_failed, plugin.sandbox_quota_hit) extend this
    schema without a route migration thanks to
    ``model_config = {"extra": "allow"}``.
    """

    model_config = {"extra": "allow"}

    axis: str
    reason: str
    severity: str
    title_token: str
    body_token: str
    action_chips: list[ActionChipModel] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    first_observed_monotonic: float
    last_observed_monotonic: float
    occurrence_count: int


class AckStateModel(BaseModel):
    """Operator-acknowledgement state for the composite banner.

    Phase 1 ships the schema with default-empty values; Phase 3
    (``operator_acks`` SQLite table + ``POST /api/voice/degraded/ack``)
    populates the fields from
    :mod:`sovyx.engine._operator_acks_store`.

    Forward-additive — future fields (``ack_reason``, ``last_resurfaced_at``,
    ``operator_token_hash``) extend without a schema migration.
    """

    model_config = {"extra": "allow"}

    acked: bool = False
    acked_at_ts: int | None = None
    ttl_sec: int | None = None
    ttl_remaining_sec: int | None = None
    operator_id: str | None = None


class EngineDegradedResponse(BaseModel):
    """Top-level composite payload for ``GET /api/engine/degraded``.

    Consumed by the global ``<DegradedBannerGlobalMount>`` + per-page
    ``<DegradedBannerPerPageMount>`` components (Mission C4 §T1.10 /
    §T1.11) via the ``useEngineDegradedPoller`` hook.
    """

    model_config = {"extra": "allow"}

    axes: list[DegradedAxisModel] = Field(default_factory=list)
    composite_severity: str | None = None
    composite_axis_count: int = 0
    ack: AckStateModel = Field(default_factory=AckStateModel)


def _compute_composite_severity(distinct_axis_count: int) -> str | None:
    """Severity escalation per ADR-D6.

    Kept as a free function (not an enum) so the producer-side
    consumer at ``voice_status.get_voice_status`` can call it directly
    without a circular import on the response model.
    """
    if distinct_axis_count <= 0:
        return None
    if distinct_axis_count == 1:
        return "warn"
    if distinct_axis_count == 2:
        return "error"
    return "critical"


def _entry_to_axis_model(entry: DegradedEntry) -> DegradedAxisModel:
    return DegradedAxisModel(
        axis=entry.axis,
        reason=entry.reason,
        severity=entry.severity,
        title_token=entry.title_token,
        body_token=entry.body_token,
        action_chips=[
            ActionChipModel(
                label_token=c.label_token,
                action=c.action,
                target=c.target,
                style=c.style,
            )
            for c in entry.action_chips
        ],
        metadata=entry.metadata,
        first_observed_monotonic=entry.first_observed_monotonic,
        last_observed_monotonic=entry.last_observed_monotonic,
        occurrence_count=entry.occurrence_count,
    )


def _ack_reason_for_axis(axis: str, reason: str) -> str:
    """Mission C4 §Phase 3 — canonical ack-key shape.

    The store records degraded entries keyed by ``reason``; the
    operator-ack store records by an ack-reason that combines axis +
    reason to keep ack scope explicit (a future degraded entry with
    the same reason on a different axis would NOT auto-inherit the
    ack). Format: ``{axis}.{reason}``.
    """
    return f"{axis}.{reason}"


async def _resolve_operator_acks_store(
    request: Request,
) -> Any | None:  # noqa: ANN401 — concrete type imported lazily
    """Resolve the registry-backed OperatorAcksStore.

    Best-effort: pre-Phase-3 hosts (during rollback) lack the
    registration; this helper returns None so the endpoint degrades
    gracefully to a no-ack-state payload rather than 5xx-ing."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    try:
        from sovyx.engine._operator_acks_store import OperatorAcksStore

        if not registry.is_registered(OperatorAcksStore):
            return None
        return await registry.resolve(OperatorAcksStore)
    except Exception:  # noqa: BLE001
        logger.debug("operator_acks_store_resolve_failed")
        return None


def _aggregate_ack_state(
    axes: list[DegradedAxisModel],
    active_acks_by_key: dict[str, Any],
) -> AckStateModel:
    """Compute the top-level ``ack`` block for the response.

    The banner is "fully acked" when EVERY active axis has a matching
    active ack. If even one axis lacks an ack, the banner renders +
    the top-level ack reports ``acked=False`` so the operator can
    re-ack the composite.

    Returns the MOST RECENT (highest acked_at_ts) record's TTL fields
    for operator-visible reference; per-axis ack state is reported in
    ``DegradedAxisModel.metadata`` via the producer.
    """
    if not axes:
        return AckStateModel(acked=False)
    keys = [_ack_reason_for_axis(a.axis, a.reason) for a in axes]
    matching = [active_acks_by_key.get(k) for k in keys]
    # Narrow the union via a typed list — mypy strict cannot reason
    # through ``any(m is None for m in matching)`` to refine the
    # remaining elements, so build a non-None list explicitly.
    matched: list[Any] = [m for m in matching if m is not None]
    if len(matched) < len(keys):
        return AckStateModel(acked=False)
    now_ts = int(time.time())
    # All axes acked — surface the most-recent ack's TTL fields.
    latest = max(matched, key=lambda r: r.acked_at_ts)
    return AckStateModel(
        acked=True,
        acked_at_ts=int(latest.acked_at_ts),
        ttl_sec=int(latest.ttl_sec),
        ttl_remaining_sec=latest.ttl_remaining_sec(now_ts),
        operator_id=latest.operator_id or "",
    )


@router.get("/degraded", response_model=EngineDegradedResponse)
async def get_engine_degraded(request: Request) -> EngineDegradedResponse:
    """Composite degraded-state snapshot across all engine axes.

    Mission C4 §T1.6 — the single source-of-truth for the dashboard
    banner mount. Replaces N independent log-grep workflows with one
    actionable payload.

    Phase 3 enrichment: the ``ack`` field reflects the active
    operator-acknowledgement state from
    :class:`sovyx.engine._operator_acks_store.OperatorAcksStore`. The
    composite ack is "acked" iff EVERY active axis has a matching
    active ack; this prevents an operator from acking ONE axis +
    silently silencing the entire banner when other axes remain.

    Auth via the shared ``verify_token`` dependency on the router.
    Idempotent + cheap (in-memory snapshot + bounded SQLite lookup);
    safe to poll at 5 s cadence under the
    :func:`useEngineDegradedPoller` hook.
    """
    store = get_default_degraded_store()
    entries = store.snapshot()
    distinct_axes = sorted({e.axis for e in entries})
    axis_models = [_entry_to_axis_model(e) for e in entries]

    # Phase 3 — enrich with persisted ack state.
    acks_store = await _resolve_operator_acks_store(request)
    active_acks_by_key: dict[str, Any] = {}
    if acks_store is not None:
        try:
            active_acks = await acks_store.list_active_acks()
            active_acks_by_key = {r.reason: r for r in active_acks}
        except Exception:  # noqa: BLE001
            logger.debug("operator_acks_store_list_failed")

    return EngineDegradedResponse(
        axes=axis_models,
        composite_severity=_compute_composite_severity(len(distinct_axes)),
        composite_axis_count=len(distinct_axes),
        ack=_aggregate_ack_state(axis_models, active_acks_by_key),
    )


class AckRequestBody(BaseModel):
    """Body for ``POST /api/voice/degraded/ack``.

    Mission C4 §Phase 3 §T3.3.

    Acks EITHER a single axis (when ``reason`` is the canonical
    axis-reason composite, e.g. ``"voice.failover_ladder_exhausted"``)
    OR the whole composite banner (when ``reason="composite"`` — Phase 3
    extension that records one ack per ACTIVE axis at the current
    snapshot; convenient default for the banner's "Acknowledge" button).
    """

    model_config = {"extra": "allow"}

    reason: str
    """Either the composite-banner shorthand ``"composite"`` OR a
    canonical ``{axis}.{reason}`` ack key matching a degraded
    entry's axis + reason composite."""

    ttl_sec: int | None = None
    """Operator-chosen TTL. ``None`` falls back to
    :attr:`VoiceTuningConfig.degraded_banner_ack_default_ttl_sec`
    (default 3600 s)."""

    metadata: dict[str, Any] | None = None
    """Optional axis-specific context captured at ack time. Bounded
    by the endpoint's request-body parser; stored as JSON in the
    ``operator_acks`` table."""


class AckResponse(BaseModel):
    """Response for ``POST /api/voice/degraded/ack``."""

    model_config = {"extra": "allow"}

    ok: bool
    """``True`` when at least one ack record landed in the store."""

    reasons_acked: list[str] = Field(default_factory=list)
    """Canonical ack-keys recorded (one per axis when
    ``reason="composite"`` is acked, else single-element)."""

    acked_at_ts: int = 0
    """Unix epoch seconds of the ack."""

    ttl_sec: int = 0
    """TTL applied to all records in this ack batch."""

    ttl_remaining_sec: int = 0
    """Seconds remaining before re-surface (same as ``ttl_sec`` on
    fresh ack; lower on idempotent re-ack of an existing record)."""


def _resolve_default_ack_ttl_sec(request: Request) -> int:
    """Read the ack default TTL from the engine's VoiceTuningConfig
    via the engine state. Falls back to 3600 s (the knob's documented
    default) when unavailable.
    """
    try:
        engine_config = getattr(request.app.state, "engine_config", None)
        if engine_config is None:
            return 3600
        tuning = getattr(engine_config, "tuning", None)
        voice = getattr(tuning, "voice", None)
        if voice is None:
            return 3600
        explicit = getattr(voice, "degraded_banner_ack_default_ttl_sec", None)
        if isinstance(explicit, int) and explicit > 0:
            return explicit
    except Exception:  # noqa: BLE001
        pass
    return 3600


@router.post("/degraded/ack", response_model=AckResponse)
async def post_engine_degraded_ack(
    body: AckRequestBody,
    request: Request,
) -> AckResponse:
    """Mission C4 §Phase 3 §T3.3 — operator acknowledges the composite
    degraded banner.

    The ack persists to the ``operator_acks`` SQLite table so the
    banner stays muted across browser refreshes + multi-tab usage
    (Phase 3 §T3.1 + ADR-D2). On TTL expiry the Phase 3 re-surface
    scheduler removes the ack + emits
    ``voice.degraded_banner.resurfaced``; the next dashboard poll
    sees the unacked state + the banner re-renders.

    ``reason="composite"`` is the convenience shorthand for the
    banner's single-button "Acknowledge" action — records one ack
    per CURRENTLY-ACTIVE degraded axis under the canonical
    ``{axis}.{reason}`` key. Future degraded axes appearing AFTER
    the composite ack will surface fresh (not auto-acked).

    Returns 503 when the OperatorAcksStore is unavailable (pre-Phase-3
    rollback or registry failure); 422 on out-of-bounds ``ttl_sec``
    per ADR-D9.
    """
    # ttl_sec bounds per ADR-D9
    raw_ttl = body.ttl_sec
    if raw_ttl is None:
        ttl_sec = _resolve_default_ack_ttl_sec(request)
    elif not (60 <= int(raw_ttl) <= 86400):
        raise HTTPException(
            status_code=422,
            detail=(
                f"ttl_sec must be in [60, 86400]; got {raw_ttl}. "
                "Lower bound prevents pathological ack-loops; upper "
                "bound prevents permanent silencing (ADR-D9)."
            ),
        )
    else:
        ttl_sec = int(raw_ttl)

    acks_store = await _resolve_operator_acks_store(request)
    if acks_store is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="OperatorAcksStore not registered",
        )

    # Resolve which reasons to record.
    if body.reason == "composite":
        store = get_default_degraded_store()
        entries = store.snapshot()
        targets = [_ack_reason_for_axis(e.axis, e.reason) for e in entries]
    else:
        targets = [body.reason]

    if not targets:
        # No active degraded axes — nothing to ack. Return ok=False so
        # the frontend can detect + skip its optimistic-update.
        return AckResponse(
            ok=False,
            reasons_acked=[],
            acked_at_ts=int(time.time()),
            ttl_sec=ttl_sec,
            ttl_remaining_sec=ttl_sec,
        )

    operator_id = _extract_operator_id(request)
    recorded: list[Any] = []
    for target in targets:
        record = await acks_store.record_ack(
            reason=target,
            ttl_sec=ttl_sec,
            operator_id=operator_id,
            metadata=body.metadata or {},
        )
        recorded.append(record)

    latest = max(recorded, key=lambda r: r.acked_at_ts)
    now_ts = int(time.time())
    return AckResponse(
        ok=True,
        reasons_acked=[r.reason for r in recorded],
        acked_at_ts=int(latest.acked_at_ts),
        ttl_sec=ttl_sec,
        ttl_remaining_sec=latest.ttl_remaining_sec(now_ts),
    )


def _extract_operator_id(request: Request) -> str:
    """Mission C4 §Phase 3 — best-effort operator-id derivation.

    Hashes the bearer token's prefix so the audit trail can correlate
    acks to a session without storing the token itself. Empty string
    when no Authorization header is present (e.g. localhost-only
    setups). NOT a credential.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    import hashlib

    token = auth[7:].strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


__all__ = [
    "ActionChipModel",
    "AckStateModel",
    "DegradedAxisModel",
    "EngineDegradedResponse",
    "_compute_composite_severity",
    "router",
]
