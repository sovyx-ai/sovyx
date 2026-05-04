"""Shared utilities for dashboard modules.

Contains common logic used across status, brain, and conversations modules
to avoid DRY violations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from starlette.requests import Request

    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


# ── Mind-id resolution sources (Mission §Phase 1 T1.2) ────────────
# Reported under ``voice.source`` in
# ``voice.dashboard.voice_enable_mind_resolved`` so dashboards can
# distinguish where the active mind came from. The string values are
# part of the public observability vocabulary; renaming any of them
# is a breaking change for Grafana panels keyed on the field.
MIND_ID_SOURCE_APP_STATE = "app_state"
MIND_ID_SOURCE_MIND_MANAGER = "mind_manager"
MIND_ID_SOURCE_FALLBACK_DEFAULT = "fallback_default"


async def get_active_mind_id(registry: ServiceRegistry) -> str:
    """Get the first active mind ID from MindManager.

    Used by status, brain, and conversations modules for mind-scoped queries.
    Returns "default" if MindManager is unavailable.
    """
    try:
        from sovyx.engine.bootstrap import MindManager

        if registry.is_registered(MindManager):
            manager = await registry.resolve(MindManager)
            minds = manager.get_active_minds()
            if minds:
                return minds[0]
    except Exception:  # noqa: BLE001
        logger.debug("get_active_mind_id_failed")
    return "default"


async def resolve_active_mind_id_for_request(
    request: Request,
) -> tuple[str, str]:
    """Resolve the active mind id for a dashboard request.

    Resolution order (first hit wins):

    1. ``request.app.state.mind_id`` — the cached value the dashboard
       server populates at startup from
       :class:`sovyx.engine.bootstrap.MindManager`. Source string:
       :data:`MIND_ID_SOURCE_APP_STATE`.
    2. Live :func:`get_active_mind_id` lookup against the registry on
       ``request.app.state.registry``. Catches the multi-mind case
       where the cache may be stale. Source string:
       :data:`MIND_ID_SOURCE_MIND_MANAGER`.
    3. The literal sentinel ``"default"``. Source string:
       :data:`MIND_ID_SOURCE_FALLBACK_DEFAULT`.

    Returns ``(mind_id, source)`` so callers can emit structured
    telemetry citing where the resolution landed. Never raises —
    every step is wrapped in best-effort lookups (anti-pattern #33).

    Forensic anchor for the bug this resolves: the dashboard
    ``/api/voice/enable`` route at ``dashboard/routes/voice.py:1802``
    used to call ``getattr(request.app.state, "mind_id", "default")``
    while ``app.state.mind_id`` was never assigned anywhere in
    production code — the pipeline always operated under the phantom
    ``"default"`` mind even when the operator had created a real one.
    See ``c:\\Users\\guipe\\Downloads\\logs_01.txt`` line 1342 (every
    ``voice_pipeline_heartbeat`` carries ``mind_id=default`` despite
    the user's mind being ``jonny``).
    """
    cached = getattr(request.app.state, "mind_id", "")
    if isinstance(cached, str) and cached and cached != "default":
        return cached, MIND_ID_SOURCE_APP_STATE

    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            resolved = await get_active_mind_id(registry)
        except Exception:  # noqa: BLE001
            logger.debug("resolve_active_mind_id_for_request_failed")
        else:
            if resolved and resolved != "default":
                return resolved, MIND_ID_SOURCE_MIND_MANAGER

    # Last resort — preserves pre-T1.2 behaviour for fresh installs
    # where no mind has been initialised yet (genuine empty state).
    if isinstance(cached, str) and cached:
        # ``app.state.mind_id == "default"`` literal sentinel cached
        # by the server fallback — surface as app_state so dashboards
        # see "this came from the cache, not from a live registry
        # lookup that returned default".
        return cached, MIND_ID_SOURCE_APP_STATE
    return "default", MIND_ID_SOURCE_FALLBACK_DEFAULT
