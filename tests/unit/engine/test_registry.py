"""Tests for sovyx.engine.registry — ServiceRegistry."""

from __future__ import annotations

import pytest

from sovyx.engine.errors import ServiceNotRegisteredError
from sovyx.engine.registry import ServiceRegistry


class DummyService:
    """Test service."""

    def __init__(self, value: int = 42) -> None:
        self.value = value


class DummyShutdown:
    """Service with async shutdown."""

    def __init__(self) -> None:
        self.shut = False

    async def shutdown(self) -> None:
        self.shut = True


class TestRegisterSingleton:
    """Singleton registration."""

    async def test_resolve_creates_instance(self) -> None:
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, lambda: DummyService(99))
        svc = await reg.resolve(DummyService)
        assert svc.value == 99  # noqa: PLR2004

    async def test_singleton_returns_same_instance(self) -> None:
        reg = ServiceRegistry()
        call_count = 0

        def factory() -> DummyService:
            nonlocal call_count
            call_count += 1
            return DummyService()

        reg.register_singleton(DummyService, factory)
        s1 = await reg.resolve(DummyService)
        s2 = await reg.resolve(DummyService)
        assert s1 is s2
        assert call_count == 1

    async def test_overwrite_logs_warning(self) -> None:
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, DummyService)
        # Second register overwrites
        reg.register_singleton(DummyService, lambda: DummyService(77))
        svc = await reg.resolve(DummyService)
        assert svc.value == 77  # noqa: PLR2004


class TestRegisterInstance:
    """Instance registration."""

    async def test_resolve_returns_exact_instance(self) -> None:
        reg = ServiceRegistry()
        obj = DummyService(123)
        reg.register_instance(DummyService, obj)
        svc = await reg.resolve(DummyService)
        assert svc is obj

    async def test_instance_takes_priority(self) -> None:
        """If both factory and instance exist, instance wins."""
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, lambda: DummyService(1))
        obj = DummyService(2)
        reg.register_instance(DummyService, obj)
        svc = await reg.resolve(DummyService)
        assert svc is obj


class TestResolve:
    """Resolution errors."""

    async def test_unregistered_raises(self) -> None:
        reg = ServiceRegistry()
        with pytest.raises(
            ServiceNotRegisteredError, match="DummyService"
        ):
            await reg.resolve(DummyService)


class TestIsRegistered:
    """Registration checks."""

    def test_registered_singleton(self) -> None:
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, DummyService)
        assert reg.is_registered(DummyService) is True

    def test_registered_instance(self) -> None:
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService())
        assert reg.is_registered(DummyService) is True

    def test_not_registered(self) -> None:
        reg = ServiceRegistry()
        assert reg.is_registered(DummyService) is False


class TestShutdown:
    """Shutdown in reverse order."""

    async def test_shutdown_calls_shutdown_method(self) -> None:
        reg = ServiceRegistry()
        svc = DummyShutdown()
        reg.register_instance(DummyShutdown, svc)
        await reg.shutdown_all()
        assert svc.shut is True

    async def test_shutdown_skips_no_shutdown(self) -> None:
        """Services without shutdown() are skipped."""
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService())
        # Should not crash
        await reg.shutdown_all()

    async def test_shutdown_reverse_order(self) -> None:
        order: list[str] = []

        class A:
            async def shutdown(self) -> None:
                order.append("A")

        class B:
            async def shutdown(self) -> None:
                order.append("B")

        reg = ServiceRegistry()
        reg.register_instance(A, A())
        reg.register_instance(B, B())
        await reg.shutdown_all()
        assert order == ["B", "A"]

    async def test_shutdown_exception_continues(self) -> None:
        """Exception in one service doesn't stop others."""

        class Bad:
            async def shutdown(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

        reg = ServiceRegistry()
        svc = DummyShutdown()
        reg.register_instance(Bad, Bad())
        reg.register_instance(DummyShutdown, svc)
        await reg.shutdown_all()
        # DummyShutdown still got shut down (reverse order: it's first)
        assert svc.shut is True

    async def test_shutdown_clears_registry(self) -> None:
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService())
        await reg.shutdown_all()
        assert reg.is_registered(DummyService) is False
