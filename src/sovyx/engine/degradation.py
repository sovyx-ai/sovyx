"""Graceful degradation manager (Blueprint §13)."""

from __future__ import annotations

import shutil
from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.engine.events import EventBus
    from sovyx.engine.health import HealthChecker

logger = get_logger(__name__)


class DegradationLevel(IntEnum):
    """System degradation levels."""

    HEALTHY = auto()
    DEGRADED = auto()
    CRITICAL = auto()


class ComponentStatus:
    """Track status of a degradable component."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.healthy = True
        self.fallback_active = False
        self.last_error: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "healthy": self.healthy,
            "fallback_active": self.fallback_active,
            "last_error": self.last_error,
        }


class DegradationManager:
    """Centralize fallback chains and monitor system health.

    Degradation matrix (Blueprint §13):
        - sqlite-vec missing → FTS5-only search
        - All LLM providers down → template response
        - Telegram disconnect → exponential backoff
        - Disk < 100MB → read-only mode warning
        - OOM risk → trigger consolidation prune
    """

    def __init__(
        self,
        event_bus: EventBus,
        health_checker: HealthChecker | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._health_checker = health_checker
        self._components: dict[str, ComponentStatus] = {}
        self._fallbacks: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._disk_threshold_mb = 100

    def register_fallback(
        self,
        component: str,
        fallback: Callable[..., Awaitable[Any]],
    ) -> None:
        """Register a fallback handler for a component.

        Args:
            component: Component name (e.g., 'sqlite-vec', 'embedding').
            fallback: Async callable to invoke on failure.
        """
        self._components[component] = ComponentStatus(component)
        self._fallbacks[component] = fallback
        logger.debug("fallback_registered", component=component)

    async def handle_failure(self, component: str, error: Exception) -> None:
        """Handle component failure by activating fallback.

        Args:
            component: Failed component name.
            error: The exception that occurred.
        """
        status = self._components.get(component)
        if status is None:
            logger.warning(
                "unknown_component_failure",
                component=component,
                error=str(error),
            )
            return

        status.healthy = False
        status.last_error = str(error)

        fallback = self._fallbacks.get(component)
        if fallback is not None:
            try:
                await fallback()
                status.fallback_active = True
                logger.info(
                    "fallback_activated",
                    component=component,
                    error=str(error),
                )
            except Exception:
                logger.exception("fallback_activation_failed", component=component)
        else:
            logger.warning("no_fallback_available", component=component)

    async def handle_recovery(self, component: str) -> None:
        """Mark component as recovered.

        Args:
            component: Recovered component name.
        """
        status = self._components.get(component)
        if status is not None:
            status.healthy = True
            status.fallback_active = False
            status.last_error = ""
            logger.info("component_recovered", component=component)

    def check_disk_space(self) -> bool:
        """Check if disk space is above threshold.

        Returns:
            True if enough disk space, False if critically low.
        """
        usage = shutil.disk_usage("/")
        free_mb = usage.free // (1024 * 1024)
        if free_mb < self._disk_threshold_mb:
            logger.warning(
                "disk_space_low",
                free_mb=free_mb,
                threshold_mb=self._disk_threshold_mb,
            )
            return False
        return True

    @property
    def level(self) -> DegradationLevel:
        """Current degradation level based on component states."""
        if not self._components:
            return DegradationLevel.HEALTHY

        unhealthy = [s for s in self._components.values() if not s.healthy]
        if not unhealthy:
            return DegradationLevel.HEALTHY

        # Critical if more than half of components are down
        if len(unhealthy) > len(self._components) // 2:
            return DegradationLevel.CRITICAL

        return DegradationLevel.DEGRADED

    def status(self) -> dict[str, object]:
        """Get full degradation status."""
        return {
            "level": self.level.name,
            "components": {name: s.to_dict() for name, s in self._components.items()},
            "disk_ok": self.check_disk_space(),
        }
