"""Frontend-error telemetry endpoint.

The dashboard's ``ErrorBoundary`` posts unhandled React render errors
to ``POST /api/telemetry/frontend-error`` so they show up in the same
structlog stream as backend errors. This is the dashboard's
``componentDidCatch`` replacement for an external Sentry/PostHog hook.

Payload is untrusted (comes from the browser). All fields are length-
capped and logged at WARNING level without raising. Rate-limited so a
crash loop in the SPA cannot flood the log file.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/telemetry",
    dependencies=[Depends(verify_token)],
    tags=["telemetry"],
)

# Soft rate limit — 20 reports per 60 s window, shared across all clients.
# A real crash in a lazy chunk will fire once per user navigation; anything
# beyond this is almost certainly a loop and we want to drop it quietly.
_WINDOW_S = 60.0
_MAX_PER_WINDOW = 20
_recent: deque[float] = deque(maxlen=_MAX_PER_WINDOW)


def _allow() -> bool:
    """Return True if this report falls inside the rate-limit budget."""
    now = time.monotonic()
    while _recent and now - _recent[0] > _WINDOW_S:
        _recent.popleft()
    if len(_recent) >= _MAX_PER_WINDOW:
        return False
    _recent.append(now)
    return True


class FrontendError(BaseModel):
    """Error payload sent by the dashboard's ErrorBoundary."""

    message: str = Field(..., max_length=1_000)
    name: str | None = Field(default=None, max_length=200)
    stack: str | None = Field(default=None, max_length=4_000)
    component_stack: str | None = Field(default=None, max_length=4_000)
    url: str | None = Field(default=None, max_length=2_000)
    user_agent: str | None = Field(default=None, max_length=500)


@router.post("/frontend-error")
async def report_frontend_error(payload: FrontendError) -> JSONResponse:
    """Record a frontend render error at WARNING level.

    Always returns 200 — the dashboard is best-effort about reporting its
    own crashes and we don't want a server-side failure to cascade into
    another ErrorBoundary render. When the rate limit is exceeded the
    report is silently dropped and the response signals that.
    """
    if not _allow():
        return JSONResponse({"ok": True, "dropped": True})

    logger.warning(
        "frontend_render_error",
        error_name=payload.name,
        error_message=payload.message,
        stack=payload.stack,
        component_stack=payload.component_stack,
        url=payload.url,
        user_agent=payload.user_agent,
    )
    return JSONResponse({"ok": True, "dropped": False})
