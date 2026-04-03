"""Tests for sovyx.engine.degradation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sovyx.engine.degradation import (
    ComponentStatus,
    DegradationLevel,
    DegradationManager,
)
from sovyx.engine.events import EventBus


@pytest.fixture()
def event_bus() -> EventBus:
    """Event bus fixture."""
    return EventBus()


@pytest.fixture()
def manager(event_bus: EventBus) -> DegradationManager:
    """DegradationManager fixture."""
    return DegradationManager(event_bus)


class TestComponentStatus:
    """ComponentStatus tests."""

    def test_initial_state(self) -> None:
        status = ComponentStatus("test")
        assert status.healthy is True
        assert status.fallback_active is False
        assert status.last_error == ""

    def test_to_dict(self) -> None:
        status = ComponentStatus("test")
        d = status.to_dict()
        assert d["name"] == "test"
        assert d["healthy"] is True


class TestDegradationLevel:
    """DegradationLevel tests."""

    def test_healthy_when_no_components(self, manager: DegradationManager) -> None:
        assert manager.level == DegradationLevel.HEALTHY

    def test_healthy_when_all_ok(self, manager: DegradationManager) -> None:
        manager.register_fallback("a", AsyncMock())
        manager.register_fallback("b", AsyncMock())
        assert manager.level == DegradationLevel.HEALTHY

    async def test_degraded_when_one_fails(self, manager: DegradationManager) -> None:
        manager.register_fallback("a", AsyncMock())
        manager.register_fallback("b", AsyncMock())
        manager.register_fallback("c", AsyncMock())
        await manager.handle_failure("a", RuntimeError("down"))
        assert manager.level == DegradationLevel.DEGRADED

    async def test_critical_when_majority_fails(self, manager: DegradationManager) -> None:
        manager.register_fallback("a", AsyncMock())
        manager.register_fallback("b", AsyncMock())
        await manager.handle_failure("a", RuntimeError("down"))
        await manager.handle_failure("b", RuntimeError("down"))
        assert manager.level == DegradationLevel.CRITICAL


class TestHandleFailure:
    """handle_failure tests."""

    async def test_activates_fallback(self, manager: DegradationManager) -> None:
        fallback = AsyncMock()
        manager.register_fallback("sqlite-vec", fallback)
        await manager.handle_failure("sqlite-vec", RuntimeError("not found"))
        fallback.assert_awaited_once()
        assert manager._components["sqlite-vec"].fallback_active is True
        assert manager._components["sqlite-vec"].healthy is False

    async def test_unknown_component(self, manager: DegradationManager) -> None:
        # Should not raise
        await manager.handle_failure("unknown", RuntimeError("oops"))

    async def test_fallback_failure_handled(self, manager: DegradationManager) -> None:
        fallback = AsyncMock(side_effect=RuntimeError("fallback broke"))
        manager.register_fallback("test", fallback)
        # Should not raise even if fallback fails
        await manager.handle_failure("test", RuntimeError("original"))
        assert manager._components["test"].fallback_active is False


class TestHandleRecovery:
    """handle_recovery tests."""

    async def test_recovery_resets_state(self, manager: DegradationManager) -> None:
        manager.register_fallback("test", AsyncMock())
        await manager.handle_failure("test", RuntimeError("down"))
        assert manager._components["test"].healthy is False

        await manager.handle_recovery("test")
        assert manager._components["test"].healthy is True
        assert manager._components["test"].fallback_active is False

    async def test_recovery_unknown_component(self, manager: DegradationManager) -> None:
        # Should not raise
        await manager.handle_recovery("nonexistent")


class TestDiskSpace:
    """Disk space check tests."""

    def test_disk_space_ok(self, manager: DegradationManager) -> None:
        # Typical system has > 100MB free
        assert manager.check_disk_space() is True

    def test_disk_space_low(self, manager: DegradationManager) -> None:
        with patch("sovyx.engine.degradation.shutil.disk_usage") as mock:
            mock.return_value = type("Usage", (), {"free": 50 * 1024 * 1024})()  # 50MB
            assert manager.check_disk_space() is False


class TestStatus:
    """Status reporting tests."""

    async def test_full_status(self, manager: DegradationManager) -> None:
        manager.register_fallback("sqlite-vec", AsyncMock())
        manager.register_fallback("embedding", AsyncMock())
        await manager.handle_failure("sqlite-vec", RuntimeError("missing"))

        status = manager.status()
        assert status["level"] == "DEGRADED"
        assert "sqlite-vec" in status["components"]  # type: ignore[operator]
        assert "disk_ok" in status

    def test_status_empty(self, manager: DegradationManager) -> None:
        status = manager.status()
        assert status["level"] == "HEALTHY"
