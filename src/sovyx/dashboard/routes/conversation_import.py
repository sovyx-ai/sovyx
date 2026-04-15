"""Conversation-import HTTP endpoints — POST start + GET progress.

Wires the ``sovyx.upgrade.conv_import`` subpackage into the dashboard
API. The POST endpoint streams the uploaded ``conversations.json``
to a temp file (same 100 MiB cap pattern as ``/api/import``), parses
it once to get ``conversations_total``, and then fires off a
background ``asyncio.Task`` that iterates each conversation through
:func:`summarize_and_encode`.

Progress is observable via polling — the GET endpoint returns a
snapshot of the ``ImportProgressTracker`` entry keyed by ``job_id``.
WebSocket streaming is out of scope for v1.

Auth: both endpoints require the dashboard Bearer token via the shared
:func:`verify_token` dependency.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_202_ACCEPTED, HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.upgrade.conv_import import (
    ChatGPTImporter,
    ConversationImporter,
    ConversationImportError,
    ImportProgressTracker,
    ImportState,
    RawConversation,
    source_hash,
    summarize_and_encode,
)

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.engine.types import MindId
    from sovyx.llm.router import LLMRouter
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])

# ── Upload limits (mirrors /api/import) ───────────────────────────

MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 MiB hard cap
_IMPORT_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MiB streaming chunk

# ── Platform registry ─────────────────────────────────────────────
#
# Adding Claude/Gemini/Obsidian later is a matter of dropping a new
# importer module in sovyx.upgrade.conv_import and registering it
# here. The endpoint stays unchanged.
_IMPORTERS: dict[str, type[ConversationImporter]] = {
    "chatgpt": ChatGPTImporter,
}


# ── POST /api/import/conversations ────────────────────────────────


@router.post("/import/conversations")
async def start_conversation_import(request: Request) -> JSONResponse:
    """Start a conversation-import background job.

    Expects ``multipart/form-data`` with:
        * ``platform`` — string, currently only ``"chatgpt"``.
        * ``file`` — the platform's export file (e.g.
          ``conversations.json`` from a ChatGPT data export).

    Returns ``202 Accepted`` with ``{job_id, conversations_total}`` so
    the client can immediately poll
    ``GET /api/import/{job_id}/progress``.

    The upload is capped at :data:`MAX_IMPORT_BYTES`. The file is
    streamed to a temp path (same pattern as ``/api/import``) and the
    background task is responsible for deleting it when done.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running — no registry available"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    tracker = _get_tracker(request)
    if tracker is None:
        return JSONResponse(
            {"error": "Import tracker not initialised"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return JSONResponse(
            {"error": "Expected multipart/form-data with platform + file fields"},
            status_code=422,
        )

    # Fast-reject via Content-Length before reading the body.
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr is not None:
        try:
            declared_size = int(content_length_hdr)
        except ValueError:
            declared_size = -1
        if declared_size > MAX_IMPORT_BYTES:
            return JSONResponse(
                {"error": f"Upload too large (declared {declared_size}, max {MAX_IMPORT_BYTES})"},
                status_code=413,
            )

    form = await request.form()
    platform_raw = form.get("platform")
    platform = platform_raw.strip().lower() if isinstance(platform_raw, str) else ""
    if platform not in _IMPORTERS:
        return JSONResponse(
            {
                "error": (
                    f"Platform '{platform}' not supported. Known: {sorted(_IMPORTERS.keys())}"
                ),
            },
            status_code=422,
        )

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse(
            {"error": "Missing 'file' field in multipart form"},
            status_code=422,
        )

    # Stream the upload to a temp file with cap enforcement.
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"sovyx-conv-import-{platform}-"))
    tmp_path = tmp_dir / f"{platform}-export.json"
    try:
        written = 0
        with tmp_path.open("wb") as out:
            while True:
                chunk = await upload.read(_IMPORT_CHUNK_BYTES)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                written += len(chunk)
                if written > MAX_IMPORT_BYTES:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return JSONResponse(
                        {"error": f"Upload exceeded max size of {MAX_IMPORT_BYTES} bytes"},
                        status_code=413,
                    )
                out.write(chunk)
    except (OSError, ValueError) as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.warning("conversation_import_upload_failed", error=str(exc), exc_info=True)
        return JSONResponse(
            {"error": f"Upload failed: {exc}"},
            status_code=500,
        )

    # Pre-parse just to count conversations for the progress bar.
    importer = _IMPORTERS[platform]()
    try:
        total = sum(1 for _ in importer.parse(tmp_path))
    except ConversationImportError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JSONResponse(
            {"error": str(exc)},
            status_code=422,
        )

    job_id = await tracker.start(platform)
    await tracker.update(job_id, conversations_total=total, state=ImportState.PENDING)

    # Fire off the background task. The task owns tmp_dir cleanup.
    asyncio.create_task(
        _run_import_job(
            job_id=job_id,
            platform=platform,
            tmp_path=tmp_path,
            tmp_dir=tmp_dir,
            registry=registry,
            tracker=tracker,
        ),
    )

    logger.info(
        "conversation_import_started",
        job_id=job_id,
        platform=platform,
        conversations_total=total,
    )
    return JSONResponse(
        {"job_id": job_id, "platform": platform, "conversations_total": total},
        status_code=HTTP_202_ACCEPTED,
    )


# ── GET /api/import/{job_id}/progress ─────────────────────────────


@router.get("/import/{job_id}/progress")
async def get_conversation_import_progress(
    job_id: str,
    request: Request,
) -> JSONResponse:
    """Return a snapshot of an import job's progress.

    Returns 404 for unknown job IDs (including jobs from a previous
    daemon process — v1 keeps progress state in memory only).
    """
    tracker = _get_tracker(request)
    if tracker is None:
        return JSONResponse(
            {"error": "Import tracker not initialised"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    status = await tracker.get(job_id)
    if status is None:
        return JSONResponse({"error": f"Unknown job_id: {job_id}"}, status_code=404)

    return JSONResponse(
        {
            "job_id": status.job_id,
            "platform": status.platform,
            "state": status.state.value,
            "conversations_total": status.conversations_total,
            "conversations_processed": status.conversations_processed,
            "conversations_skipped": status.conversations_skipped,
            "episodes_created": status.episodes_created,
            "concepts_learned": status.concepts_learned,
            "warnings": list(status.warnings),
            "error": status.error,
            "elapsed_ms": status.elapsed_ms(),
        },
    )


# ── Background worker ─────────────────────────────────────────────


async def _run_import_job(
    *,
    job_id: str,
    platform: str,
    tmp_path: Path,
    tmp_dir: Path,
    registry: ServiceRegistry,
    tracker: ImportProgressTracker,
) -> None:
    """Drive one conversation-import job from start to finish.

    Each conversation is encoded via ``summarize_and_encode``, dedup-
    checked via the ``conversation_imports`` table, and counted in the
    progress tracker. The job ends in COMPLETED or FAILED state; the
    tmp upload is cleaned up either way.
    """
    try:
        await tracker.update(job_id, state=ImportState.PARSING)

        brain = await _resolve_brain(registry)
        if brain is None:
            await tracker.finish(job_id, error="BrainService not registered")
            return

        pool = await _resolve_brain_pool(registry)
        if pool is None:
            await tracker.finish(job_id, error="Brain database pool unavailable")
            return

        llm_router = await _resolve_llm_router(registry)
        mind_id = await _resolve_active_mind(registry)
        if mind_id is None:
            await tracker.finish(job_id, error="No active mind")
            return

        importer = _IMPORTERS[platform]()
        await tracker.update(job_id, state=ImportState.PROCESSING)

        for conv in importer.parse(tmp_path):
            await _process_one_conversation(
                conv=conv,
                job_id=job_id,
                brain=brain,
                pool=pool,
                llm_router=llm_router,
                mind_id=mind_id,
                tracker=tracker,
            )

        await tracker.finish(job_id)
    except ConversationImportError as exc:
        await tracker.finish(job_id, error=f"Parse error: {exc}")
    except Exception as exc:  # noqa: BLE001 — top-level worker boundary, must not crash loop
        logger.exception("conversation_import_job_failed", job_id=job_id)
        await tracker.finish(job_id, error=f"Unexpected error: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _process_one_conversation(
    *,
    conv: RawConversation,
    job_id: str,
    brain: BrainService,
    pool: DatabasePool,
    llm_router: LLMRouter | None,
    mind_id: MindId,
    tracker: ImportProgressTracker,
) -> None:
    """Encode a single conversation, respecting dedup."""
    s_hash = source_hash(conv.platform, conv.conversation_id)

    if await _already_imported(pool, s_hash):
        await tracker.update(job_id, conversations_skipped_delta=1)
        return

    try:
        result = await summarize_and_encode(
            conv=conv,
            brain=brain,
            llm_router=llm_router,
            mind_id=mind_id,
        )
    except (ValueError, AttributeError) as exc:
        # Narrow: encode_episode emits ValueError on clamp/shape issues;
        # AttributeError covers stub-registry edge cases in tests.
        await tracker.update(
            job_id,
            conversations_processed_delta=1,
            warning=f"encode failed for {conv.conversation_id}: {exc}",
        )
        return

    await _record_import(
        pool=pool,
        source_hash_value=s_hash,
        platform=conv.platform,
        mind_id=str(mind_id),
        conversation_id=conv.conversation_id,
        episode_id=str(result.episode_id),
        title=conv.title,
        messages_count=len(conv.messages),
        concepts_learned=len(result.concept_ids),
    )

    await tracker.update(
        job_id,
        conversations_processed_delta=1,
        episodes_created_delta=1,
        concepts_learned_delta=len(result.concept_ids),
    )
    for w in result.warnings:
        await tracker.update(job_id, warning=w)


# ── Dedup table I/O ───────────────────────────────────────────────


async def _already_imported(pool: DatabasePool, source_hash_value: str) -> bool:
    """True when the source_hash is already present."""
    async with pool.read() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM conversation_imports WHERE source_hash = ? LIMIT 1",
            (source_hash_value,),
        )
        row = await cursor.fetchone()
    return row is not None


async def _record_import(
    *,
    pool: DatabasePool,
    source_hash_value: str,
    platform: str,
    mind_id: str,
    conversation_id: str,
    episode_id: str,
    title: str,
    messages_count: int,
    concepts_learned: int,
) -> None:
    """Insert a row into ``conversation_imports`` (idempotent on PK)."""
    async with pool.write() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO conversation_imports "
            "(source_hash, platform, mind_id, conversation_id, episode_id, "
            "title, messages_count, concepts_learned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_hash_value,
                platform,
                mind_id,
                conversation_id,
                episode_id,
                title,
                messages_count,
                concepts_learned,
            ),
        )
        await conn.commit()


# ── Registry-resolution helpers ───────────────────────────────────


def _get_tracker(request: Request) -> ImportProgressTracker | None:
    """Return the tracker stashed on app.state (wired by create_app)."""
    return getattr(request.app.state, "import_tracker", None)


async def _resolve_brain(registry: ServiceRegistry) -> BrainService | None:
    from sovyx.brain.service import BrainService as _BrainService

    if not registry.is_registered(_BrainService):
        return None
    try:
        return await registry.resolve(_BrainService)
    except Exception:  # noqa: BLE001 — registry boundary, best-effort lookup
        logger.debug("conversation_import_brain_unavailable", exc_info=True)
        return None


async def _resolve_brain_pool(registry: ServiceRegistry) -> DatabasePool | None:
    """Resolve the brain DB pool for the active mind (same pattern as activity.py)."""
    from sovyx.dashboard._shared import get_active_mind_id
    from sovyx.engine.types import MindId
    from sovyx.persistence.manager import DatabaseManager

    if not registry.is_registered(DatabaseManager):
        return None
    try:
        db = await registry.resolve(DatabaseManager)
        mind_id = await get_active_mind_id(registry)
        return db.get_brain_pool(MindId(mind_id))
    except Exception:  # noqa: BLE001 — registry boundary
        logger.debug("conversation_import_brain_pool_unavailable", exc_info=True)
        return None


async def _resolve_llm_router(registry: ServiceRegistry) -> LLMRouter | None:
    from sovyx.llm.router import LLMRouter as _LLMRouter

    if not registry.is_registered(_LLMRouter):
        return None
    try:
        return await registry.resolve(_LLMRouter)
    except Exception:  # noqa: BLE001 — fallback path exists when router missing
        logger.debug("conversation_import_llm_router_unavailable", exc_info=True)
        return None


async def _resolve_active_mind(registry: ServiceRegistry) -> MindId | None:
    from sovyx.dashboard._shared import get_active_mind_id
    from sovyx.engine.types import MindId

    try:
        mind_id_str = await get_active_mind_id(registry)
        return MindId(mind_id_str) if mind_id_str else None
    except Exception:  # noqa: BLE001 — active mind optional in some test setups
        logger.debug("conversation_import_active_mind_unavailable", exc_info=True)
        return None
