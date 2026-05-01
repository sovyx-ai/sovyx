"""Mind management endpoints — Phase 8 / T8.21 step 5.

Companion to the ``sovyx mind forget`` CLI; surfaces the same
:class:`sovyx.mind.forget.MindForgetService` over HTTP so dashboard
operators can drive right-to-erasure (GDPR Art. 17 / LGPD Art. 18 VI)
without dropping to a terminal.

Endpoints:

    POST /api/mind/{mind_id}/forget
        Wipes every per-mind row across the brain DB, the
        conversations DB, the system DB, and the voice consent
        ledger for ``mind_id``. The mind's *configuration* is
        preserved (lives outside per-mind DBs); only its DATA is
        destroyed. Operators can re-onboard the mind without
        re-creating its config.

Defense-in-depth confirmation:
    The request body MUST include ``confirm: <mind_id>`` — i.e. the
    operator types the mind id verbatim before the wipe runs. This
    matches GitHub's "type the repo name to delete" pattern and
    defends against:

      * CSRF / clickjacking — an attacker would have to know the
        target mind id AND get the operator to type it
      * A frontend bug that fires POST without the confirmation modal
      * Scripted callers that haven't read the docs

    A ``dry_run`` request still requires the confirmation field —
    consistency over convenience; the cost of typing the mind id
    twice is trivial vs. the cost of accidentally wiping production.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig

logger = get_logger(__name__)

router = APIRouter(prefix="/api/mind", dependencies=[Depends(verify_token)])


# ── Request / response models ────────────────────────────────────────


class ForgetMindRequest(BaseModel):
    """Body for ``POST /api/mind/{mind_id}/forget``."""

    confirm: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "The exact mind_id, typed verbatim by the operator. "
            "Required even for dry_run — defense against CSRF, "
            "frontend bugs, and accidental scripted invocations."
        ),
    )
    dry_run: bool = Field(
        False,
        description=(
            "When true, returns the count report without writing. "
            "The confirmation field is still required (consistency)."
        ),
    )


class ForgetMindResponse(BaseModel):
    """Response for ``POST /api/mind/{mind_id}/forget`` — mirrors
    :class:`sovyx.mind.forget.MindForgetReport` field-for-field plus
    the three aggregate properties.

    Every field is an int (counts) or bool (``dry_run``); JSON shape
    is stable for dashboard consumers.
    """

    mind_id: str
    concepts_purged: int
    relations_purged: int
    episodes_purged: int
    concept_embeddings_purged: int
    episode_embeddings_purged: int
    conversation_imports_purged: int
    consolidation_log_purged: int
    conversations_purged: int
    conversation_turns_purged: int
    daily_stats_purged: int
    consent_ledger_purged: int
    total_brain_rows_purged: int
    total_conversations_rows_purged: int
    total_system_rows_purged: int
    total_rows_purged: int
    dry_run: bool


# ── Helpers ──────────────────────────────────────────────────────────


def _resolve_engine_config(request: Request) -> EngineConfig | None:
    """Pull EngineConfig from app state (best-effort)."""
    return getattr(request.app.state, "engine_config", None)


def _resolve_data_dir(request: Request) -> Path:
    """Return the data dir from EngineConfig or the home-dir default."""
    engine_config = _resolve_engine_config(request)
    if engine_config is not None:
        return engine_config.database.data_dir
    return Path.home() / ".sovyx"


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/{mind_id}/forget", response_model=ForgetMindResponse)
async def post_mind_forget(
    request: Request,
    mind_id: str,
    body: ForgetMindRequest,
) -> ForgetMindResponse:
    """Right-to-erasure for a single mind.

    Wipes every per-mind row across the brain DB (concepts +
    relations cascade + episodes + conversation_imports cascade +
    embeddings + consolidation_log), the conversations DB
    (conversations + turns cascade), the system DB (daily_stats),
    and the voice consent ledger for ``mind_id``. Returns a
    :class:`ForgetMindResponse` with per-table counts so the
    operator's UI can render a forensic confirmation.

    Args:
        mind_id: Target mind (path parameter).
        body: Request body with the ``confirm`` field (must equal
            ``mind_id``) and the optional ``dry_run`` flag.

    Returns:
        :class:`ForgetMindResponse` with every per-table count + the
        four aggregate totals + the dry_run echo.

    Raises:
        HTTPException 400: ``confirm`` does not match ``mind_id``,
            or ``mind_id`` is empty / whitespace.
        HTTPException 404: The mind has no databases (never
            initialised — the operator named a mind that doesn't
            exist).
        HTTPException 503: The DatabaseManager isn't registered yet
            (boot in progress).
    """
    if not mind_id.strip():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="mind_id must be a non-empty string",
        )
    if body.confirm != mind_id:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=("confirm field must exactly match mind_id (defense against accidental wipe)"),
        )

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine registry not available — daemon still booting",
        )

    from sovyx.engine.errors import DatabaseConnectionError  # noqa: PLC0415
    from sovyx.engine.types import MindId  # noqa: PLC0415
    from sovyx.mind.forget import MindForgetService  # noqa: PLC0415
    from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415
    from sovyx.voice._consent_ledger import ConsentLedger  # noqa: PLC0415

    if not registry.is_registered(DatabaseManager):
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="DatabaseManager not registered — daemon still booting",
        )

    db_manager = await registry.resolve(DatabaseManager)
    mid = MindId(mind_id)

    try:
        brain_pool = db_manager.get_brain_pool(mid)
        conv_pool = db_manager.get_conversation_pool(mid)
    except DatabaseConnectionError as exc:
        # Per-mind DBs are initialised lazily; a missing mind is a
        # 404 rather than a 500 because it represents the operator
        # naming a mind that doesn't exist (or hasn't been onboarded
        # on this host yet).
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"mind not found: {mind_id}",
        ) from exc

    system_pool = db_manager.get_system_pool()

    # Match the daemon RPC handler's path resolution — both surfaces
    # operate on the same JSONL file, so the dashboard and CLI
    # produce identical effects.
    data_dir = _resolve_data_dir(request)
    ledger = ConsentLedger(path=data_dir / "voice" / "consent.jsonl")

    service = MindForgetService(
        brain_pool=brain_pool,
        conversations_pool=conv_pool,
        system_pool=system_pool,
        ledger=ledger,
    )
    report = await service.forget_mind(mid, dry_run=body.dry_run)

    logger.warning(
        "mind.forget.via_dashboard",
        mind_id=mind_id,
        **{
            "mind.dry_run": report.dry_run,
            "mind.total_rows_purged": report.total_rows_purged,
            "mind.consent_ledger_purged": report.consent_ledger_purged,
        },
    )
    return ForgetMindResponse(
        mind_id=str(report.mind_id),
        concepts_purged=report.concepts_purged,
        relations_purged=report.relations_purged,
        episodes_purged=report.episodes_purged,
        concept_embeddings_purged=report.concept_embeddings_purged,
        episode_embeddings_purged=report.episode_embeddings_purged,
        conversation_imports_purged=report.conversation_imports_purged,
        consolidation_log_purged=report.consolidation_log_purged,
        conversations_purged=report.conversations_purged,
        conversation_turns_purged=report.conversation_turns_purged,
        daily_stats_purged=report.daily_stats_purged,
        consent_ledger_purged=report.consent_ledger_purged,
        total_brain_rows_purged=report.total_brain_rows_purged,
        total_conversations_rows_purged=report.total_conversations_rows_purged,
        total_system_rows_purged=report.total_system_rows_purged,
        total_rows_purged=report.total_rows_purged,
        dry_run=report.dry_run,
    )
