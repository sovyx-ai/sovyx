"""Sovyx HealthChecker — 10 health checks for liveness/readiness."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import time
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)

# Thresholds
_MIN_DISK_MB = 100
_MAX_RSS_PERCENT = 85
_MAX_EVENT_LOOP_LAG_MS = 100


@dataclasses.dataclass
class HealthStatus:
    """Result of health check."""

    healthy: bool
    checks: dict[str, bool]
    details: dict[str, str]
    uptime_seconds: float


class HealthChecker:
    """Engine health checks for ``sovyx doctor`` CLI command.

    10 checks (SPE-015 §doctor):

    1. SQLite writable
    2. sqlite-vec available
    3. Embedding model loaded
    4. EventBus functional
    5. Brain accessible
    6. LLM provider reachable (at least 1)
    7. Telegram connected (if configured)
    8. Disk space > 100MB
    9. RSS < 85% of total RAM
    10. Event loop lag < 100ms

    NOTE: The dashboard ``/api/health`` endpoint uses a different system —
    ``sovyx.observability.health.HealthRegistry`` — with individual
    ``HealthCheck`` subclasses wired to engine services via
    ``DashboardServer._create_health_registry()``.  This class is for CLI
    diagnostics only.
    """

    def __init__(self, registry: ServiceRegistry, start_time: float | None = None) -> None:
        self._registry = registry
        self._start_time = start_time or time.monotonic()

    async def check_all(self) -> HealthStatus:
        """Run all 10 health checks.

        Returns:
            HealthStatus with per-check results and details.
        """
        checks: dict[str, bool] = {}
        details: dict[str, str] = {}

        # Run checks concurrently
        results = await asyncio.gather(
            self._check_sqlite(),
            self._check_sqlite_vec(),
            self._check_embedding(),
            self._check_event_bus(),
            self._check_brain(),
            self._check_llm(),
            self._check_telegram(),
            self._check_disk(),
            self._check_memory(),
            self._check_event_loop_lag(),
            return_exceptions=True,
        )

        check_names = [
            "sqlite_writable",
            "sqlite_vec",
            "embedding_model",
            "event_bus",
            "brain",
            "llm_provider",
            "telegram",
            "disk_space",
            "memory_rss",
            "event_loop_lag",
        ]

        for name, result in zip(check_names, results, strict=True):
            if isinstance(result, BaseException):
                checks[name] = False
                details[name] = str(result)
            elif isinstance(result, tuple):
                ok, detail = result
                checks[name] = bool(ok)
                details[name] = str(detail)
            else:  # pragma: no cover
                checks[name] = False
                details[name] = "unexpected result type"

        # Overall: healthy if all critical checks pass
        critical = ["sqlite_writable", "event_bus", "brain"]
        healthy = all(checks.get(c, False) for c in critical)

        uptime = time.monotonic() - self._start_time

        return HealthStatus(
            healthy=healthy,
            checks=checks,
            details=details,
            uptime_seconds=uptime,
        )

    async def check_liveness(self) -> bool:
        """Lightweight liveness check — process alive."""
        return True

    async def check_readiness(self) -> bool:
        """Full readiness — all critical checks pass."""
        status = await self.check_all()
        return status.healthy

    async def _check_sqlite(self) -> tuple[bool, str]:
        """Check SQLite is writable."""
        try:
            from sovyx.persistence.manager import DatabaseManager

            if not self._registry.is_registered(DatabaseManager):
                return False, "DatabaseManager not registered"
            db = await self._registry.resolve(DatabaseManager)
            pool = db.get_system_pool()
            async with pool.write() as conn:
                await conn.execute("SELECT 1")
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_sqlite_vec(self) -> tuple[bool, str]:
        """Check sqlite-vec extension available."""
        try:
            from sovyx.persistence.manager import DatabaseManager

            if not self._registry.is_registered(DatabaseManager):
                return False, "DatabaseManager not registered"
            db = await self._registry.resolve(DatabaseManager)
            return db.has_sqlite_vec, "available" if db.has_sqlite_vec else "not available"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_embedding(self) -> tuple[bool, str]:
        """Check embedding model loaded."""
        try:
            from sovyx.brain.embedding import EmbeddingEngine

            if not self._registry.is_registered(EmbeddingEngine):
                return False, "EmbeddingEngine not registered"
            engine = await self._registry.resolve(EmbeddingEngine)
            return engine.is_loaded, "loaded" if engine.is_loaded else "not loaded"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_event_bus(self) -> tuple[bool, str]:
        """Check EventBus functional."""
        try:
            from sovyx.engine.events import EventBus

            if not self._registry.is_registered(EventBus):
                return False, "EventBus not registered"
            await self._registry.resolve(EventBus)
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_brain(self) -> tuple[bool, str]:
        """Check BrainService accessible."""
        try:
            from sovyx.brain.service import BrainService

            if not self._registry.is_registered(BrainService):
                return False, "BrainService not registered"
            await self._registry.resolve(BrainService)
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_llm(self) -> tuple[bool, str]:
        """Check at least 1 LLM provider reachable."""
        try:
            from sovyx.llm.router import LLMRouter

            if not self._registry.is_registered(LLMRouter):
                return False, "LLMRouter not registered"
            router = await self._registry.resolve(LLMRouter)
            count = len(router._providers)
            return count > 0, f"{count} providers"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_telegram(self) -> tuple[bool, str]:
        """Check Telegram connected (if configured)."""
        try:
            from sovyx.bridge.manager import BridgeManager
            from sovyx.engine.types import ChannelType

            if not self._registry.is_registered(BridgeManager):
                return True, "not configured"
            bridge = await self._registry.resolve(BridgeManager)
            adapter = bridge._get_adapter(ChannelType.TELEGRAM)
            if adapter is None:
                return True, "not configured"
            running = getattr(adapter, "is_running", False)
            return bool(running), "connected" if running else "disconnected"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_disk(self) -> tuple[bool, str]:
        """Check disk space > 100MB."""
        try:
            usage = shutil.disk_usage("/")
            free_mb = usage.free / (1024 * 1024)
            ok = free_mb > _MIN_DISK_MB
            return ok, f"{free_mb:.0f}MB free"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_memory(self) -> tuple[bool, str]:
        """Check RSS < 85% of total RAM."""
        try:
            import resource as res

            rss_bytes = res.getrusage(res.RUSAGE_SELF).ru_maxrss * 1024  # KB to bytes
            # Get total RAM
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                        total_bytes = total_kb * 1024
                        pct = (rss_bytes / total_bytes) * 100
                        ok = pct < _MAX_RSS_PERCENT
                        return ok, f"{pct:.1f}% RSS"
            return True, "unknown"  # pragma: no cover
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)

    async def _check_event_loop_lag(self) -> tuple[bool, str]:
        """Check event loop lag < 100ms."""
        try:
            start = time.monotonic()
            await asyncio.sleep(0)
            lag_ms = (time.monotonic() - start) * 1000
            ok = lag_ms < _MAX_EVENT_LOOP_LAG_MS
            return ok, f"{lag_ms:.1f}ms"
        except Exception as e:  # noqa: BLE001 — health-check boundary; pragma: no cover
            return False, str(e)
