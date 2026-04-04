"""Shared utilities for dashboard modules.

Contains common logic used across status, brain, and conversations modules
to avoid DRY violations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


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
