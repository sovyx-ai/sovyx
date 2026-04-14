"""Tests for sovyx.engine.registry — ServiceRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock

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
        with pytest.raises(ServiceNotRegisteredError, match="DummyService"):
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


class TestRegistryCoverageGaps:
    """Cover remaining registry paths."""

    @pytest.mark.asyncio()
    async def test_resolve_unregistered_raises(self) -> None:
        """Resolve raises ServiceNotRegisteredError for unknown interface."""
        from sovyx.engine.errors import ServiceNotRegisteredError

        reg = ServiceRegistry()
        with pytest.raises(ServiceNotRegisteredError, match="str"):
            await reg.resolve(str)

    @pytest.mark.asyncio()
    async def test_shutdown_skips_none_instances(self) -> None:
        """shutdown_all skips interfaces with no cached instance."""
        reg = ServiceRegistry()
        # Register factory but never resolve (no cached instance)
        reg.register_singleton(str, lambda: "hello")
        # Should not raise
        await reg.shutdown_all()

    @pytest.mark.asyncio()
    async def test_shutdown_skips_no_shutdown_method(self) -> None:
        """shutdown_all skips instances without shutdown method."""
        reg = ServiceRegistry()
        reg.register_instance(str, "hello")
        # str has no shutdown() — should skip silently
        await reg.shutdown_all()

    @pytest.mark.asyncio()
    async def test_shutdown_calls_async_shutdown(self) -> None:
        """shutdown_all handles async shutdown methods."""
        reg = ServiceRegistry()
        mock_svc = AsyncMock()
        mock_svc.shutdown = AsyncMock()
        reg.register_instance(type(mock_svc), mock_svc)
        await reg.shutdown_all()
        mock_svc.shutdown.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_shutdown_suppresses_exception(self) -> None:
        """shutdown_all logs but doesn't propagate shutdown errors."""
        reg = ServiceRegistry()
        mock_svc = AsyncMock()
        mock_svc.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        reg.register_instance(type(mock_svc), mock_svc)
        # Should not raise
        await reg.shutdown_all()

    @pytest.mark.asyncio()
    async def test_register_factory_then_resolve(self) -> None:
        """Register via factory, resolve creates and caches."""
        reg = ServiceRegistry()
        reg.register_singleton(list, lambda: [1, 2, 3])
        result = await reg.resolve(list)
        assert result == [1, 2, 3]
        # Second resolve returns same cached instance
        result2 = await reg.resolve(list)
        assert result is result2

    def test_init_order_no_duplicates(self) -> None:
        """Re-registering instance doesn't duplicate init_order."""
        reg = ServiceRegistry()
        reg.register_instance(str, "a")
        reg.register_instance(str, "b")
        str_key = "builtins.str"
        assert reg._init_order.count(str_key) == 1  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_resolve_factory_twice_no_duplicate_order(self) -> None:
        """Resolving factory twice doesn't duplicate init_order."""
        reg = ServiceRegistry()
        reg.register_singleton(list, lambda: [1, 2])
        await reg.resolve(list)
        # Manually re-add to factories to force the branch
        list_key = "builtins.list"
        reg._factories[list_key] = lambda: [3, 4]  # noqa: SLF001
        del reg._instances[list_key]  # noqa: SLF001
        await reg.resolve(list)
        assert reg._init_order.count(list_key) == 1  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_shutdown_non_callable_shutdown_attr(self) -> None:
        """shutdown_all handles non-callable shutdown attribute."""
        reg = ServiceRegistry()

        class FakeService:
            shutdown = "not callable"

        svc = FakeService()
        reg.register_instance(FakeService, svc)
        # Should not raise — skips non-callable
        await reg.shutdown_all()
