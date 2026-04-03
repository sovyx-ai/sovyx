"""Tests for sovyx.engine.health — HealthChecker."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from sovyx.engine.health import HealthChecker, HealthStatus
from sovyx.engine.registry import ServiceRegistry


def _registry_with(*services: tuple[type, object]) -> ServiceRegistry:
    """Create registry with mock services."""
    reg = ServiceRegistry()
    for iface, instance in services:
        reg.register_instance(iface, instance)
    return reg


class TestCheckAll:
    """Full health check."""

    async def test_returns_health_status(self) -> None:
        reg = ServiceRegistry()
        checker = HealthChecker(reg)
        status = await checker.check_all()

        assert isinstance(status, HealthStatus)
        assert len(status.checks) == 10  # noqa: PLR2004
        assert status.uptime_seconds >= 0

    async def test_healthy_with_critical_services(self) -> None:
        """Healthy when sqlite, event_bus, brain are ok."""
        from sovyx.brain.service import BrainService
        from sovyx.engine.events import EventBus
        from sovyx.persistence.manager import DatabaseManager

        # Mock DatabaseManager with working pool
        db = MagicMock()
        pool = MagicMock()

        class FakeConn:
            async def execute(self, sql: str) -> None:
                pass

            async def __aenter__(self) -> FakeConn:
                return self

            async def __aexit__(self, *a: object) -> None:
                pass

        pool.write = MagicMock(return_value=FakeConn())
        db.get_system_pool = MagicMock(return_value=pool)
        type(db).has_sqlite_vec = PropertyMock(return_value=True)

        reg = _registry_with(
            (DatabaseManager, db),
            (EventBus, EventBus()),
            (BrainService, MagicMock()),
        )
        checker = HealthChecker(reg)
        status = await checker.check_all()

        assert status.checks["sqlite_writable"] is True
        assert status.checks["event_bus"] is True
        assert status.checks["brain"] is True
        assert status.healthy is True

    async def test_unhealthy_without_critical(self) -> None:
        """Unhealthy when critical services missing."""
        reg = ServiceRegistry()
        checker = HealthChecker(reg)
        status = await checker.check_all()

        assert status.healthy is False
        assert status.checks["sqlite_writable"] is False
        assert status.checks["event_bus"] is False
        assert status.checks["brain"] is False


class TestLiveness:
    """Liveness check."""

    async def test_always_true(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        assert await checker.check_liveness() is True


class TestReadiness:
    """Readiness check."""

    async def test_not_ready_without_services(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        assert await checker.check_readiness() is False


class TestIndividualChecks:
    """Individual health checks."""

    async def test_disk_space(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_disk()
        assert isinstance(ok, bool)
        assert "MB" in detail

    async def test_memory_rss(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_memory()
        assert isinstance(ok, bool)
        assert "RSS" in detail or "unknown" in detail

    async def test_event_loop_lag(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_event_loop_lag()
        assert ok is True
        assert "ms" in detail

    async def test_telegram_not_configured(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_telegram()
        assert ok is True
        assert "not configured" in detail

    async def test_llm_not_registered(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_llm()
        assert ok is False

    async def test_llm_with_providers(self) -> None:
        from sovyx.llm.router import LLMRouter

        router = MagicMock()
        router._providers = [MagicMock(), MagicMock()]
        reg = _registry_with((LLMRouter, router))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_llm()
        assert ok is True
        assert "2 providers" in detail

    async def test_embedding_not_registered(self) -> None:
        checker = HealthChecker(ServiceRegistry())
        ok, detail = await checker._check_embedding()
        assert ok is False

    async def test_sqlite_vec_check(self) -> None:
        from sovyx.persistence.manager import DatabaseManager

        db = MagicMock()
        type(db).has_sqlite_vec = PropertyMock(return_value=False)
        reg = _registry_with((DatabaseManager, db))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_sqlite_vec()
        assert ok is False
        assert "not available" in detail

    async def test_uptime_tracking(self) -> None:
        import time

        start = time.monotonic()
        checker = HealthChecker(ServiceRegistry(), start_time=start)
        status = await checker.check_all()
        assert status.uptime_seconds >= 0

    async def test_check_exception_handled(self) -> None:
        """If a check raises, it's caught and reported as failed."""
        checker = HealthChecker(ServiceRegistry())
        # All checks should complete without raising
        status = await checker.check_all()
        assert isinstance(status, HealthStatus)

    async def test_sqlite_writable(self) -> None:
        """SQLite check with working pool."""
        from sovyx.persistence.manager import DatabaseManager

        db = MagicMock()
        pool = MagicMock()

        class FakeConn:
            async def execute(self, sql: str) -> None:
                pass

            async def __aenter__(self) -> FakeConn:
                return self

            async def __aexit__(self, *a: object) -> None:
                pass

        pool.write = MagicMock(return_value=FakeConn())
        db.get_system_pool = MagicMock(return_value=pool)
        reg = _registry_with((DatabaseManager, db))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_sqlite()
        assert ok is True

    async def test_sqlite_failure(self) -> None:
        """SQLite check fails on error."""
        from sovyx.persistence.manager import DatabaseManager

        db = MagicMock()
        db.get_system_pool = MagicMock(side_effect=RuntimeError("db down"))
        reg = _registry_with((DatabaseManager, db))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_sqlite()
        assert ok is False

    async def test_brain_registered(self) -> None:
        from sovyx.brain.service import BrainService

        reg = _registry_with((BrainService, MagicMock()))
        checker = HealthChecker(reg)
        ok, detail = await checker._check_brain()
        assert ok is True

    async def test_event_bus_registered(self) -> None:
        from sovyx.engine.events import EventBus

        reg = _registry_with((EventBus, EventBus()))
        checker = HealthChecker(reg)
        ok, detail = await checker._check_event_bus()
        assert ok is True

    async def test_embedding_loaded(self) -> None:
        from sovyx.brain.embedding import EmbeddingEngine

        engine = MagicMock()
        type(engine).is_loaded = PropertyMock(return_value=True)
        reg = _registry_with((EmbeddingEngine, engine))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_embedding()
        assert ok is True
        assert "loaded" in detail

    async def test_telegram_running(self) -> None:
        from sovyx.bridge.manager import BridgeManager

        adapter = MagicMock()
        type(adapter).is_running = PropertyMock(return_value=True)
        bridge = MagicMock()
        bridge._get_adapter = MagicMock(return_value=adapter)
        reg = _registry_with((BridgeManager, bridge))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_telegram()
        assert ok is True
        assert "connected" in detail

    async def test_telegram_disconnected(self) -> None:
        from sovyx.bridge.manager import BridgeManager

        adapter = MagicMock()
        type(adapter).is_running = PropertyMock(return_value=False)
        bridge = MagicMock()
        bridge._get_adapter = MagicMock(return_value=adapter)
        reg = _registry_with((BridgeManager, bridge))

        checker = HealthChecker(reg)
        ok, detail = await checker._check_telegram()
        assert ok is False
        assert "disconnected" in detail
