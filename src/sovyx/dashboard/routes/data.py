"""Data portability — export/import endpoints.

Imports the active mind as a ``.sovyx-mind`` ZIP (with a 100 MiB hard cap
enforced via both ``Content-Length`` fast-reject and streaming chunk
accumulation to defeat lying/missing headers).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Upload Limits ──

MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 MiB — hard cap on /api/import uploads.
_IMPORT_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MiB streaming read chunk size.


router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/export")
async def export_mind_endpoint(request: Request) -> Response:
    """Export the active mind as a .sovyx-mind ZIP archive download.

    Returns a streaming ZIP file attachment.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard._shared import get_active_mind_id
    from sovyx.dashboard.export_import import export_mind

    mind_id = await get_active_mind_id(registry)
    try:
        archive_path = await export_mind(registry, mind_id)
    except RuntimeError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception:  # noqa: BLE001
        logger.exception("export_mind_failed")
        return JSONResponse(
            {"error": "Export failed"},
            status_code=500,
        )

    return FileResponse(
        path=str(archive_path),
        media_type="application/zip",
        filename=f"{mind_id}.sovyx-mind",
        headers={"Content-Disposition": f'attachment; filename="{mind_id}.sovyx-mind"'},
    )


@router.post("/import")
async def import_mind_endpoint(request: Request) -> JSONResponse:
    """Import a mind from an uploaded .sovyx-mind ZIP archive.

    Expects multipart/form-data with a ``file`` field containing the
    archive. Optional query param ``overwrite=true`` to replace an
    existing mind.

    Upload is capped at ``MAX_IMPORT_BYTES`` (100 MiB). The cap is
    enforced both via the ``Content-Length`` header (fast reject) and
    via streaming chunk accumulation (defeats missing/lying headers).
    """
    import shutil
    import tempfile

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    overwrite = request.query_params.get("overwrite", "").lower() in (
        "true",
        "1",
        "yes",
    )

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return JSONResponse(
            {"error": "Expected multipart/form-data with a 'file' field"},
            status_code=422,
        )

    # Fast reject via Content-Length header (before reading body).
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr is not None:
        try:
            declared_size = int(content_length_hdr)
        except ValueError:
            declared_size = -1
        if declared_size > MAX_IMPORT_BYTES:
            return JSONResponse(
                {
                    "error": (
                        f"Upload too large (declared {declared_size} bytes, "
                        f"max {MAX_IMPORT_BYTES})"
                    ),
                },
                status_code=413,
                headers={"Content-Length": "0"},
            )

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse(
            {"error": "Missing 'file' in form data"},
            status_code=422,
        )

    # Stream upload to disk with cap — defeats lying/missing Content-Length.
    tmp_dir = Path(tempfile.mkdtemp(prefix="sovyx-import-"))
    tmp_path = tmp_dir / "upload.sovyx-mind"
    try:
        written = 0
        with tmp_path.open("wb") as out:
            while True:
                chunk = await upload.read(_IMPORT_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_IMPORT_BYTES:
                    return JSONResponse(
                        {"error": (f"Upload exceeded max size of {MAX_IMPORT_BYTES} bytes")},
                        status_code=413,
                    )
                out.write(chunk)

        from sovyx.dashboard.export_import import import_mind

        result = await import_mind(registry, tmp_path, overwrite=overwrite)
        return JSONResponse({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        logger.exception("import_mind_failed")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
