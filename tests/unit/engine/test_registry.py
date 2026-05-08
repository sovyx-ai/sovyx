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


class TestDeregister:
    """Targeted removal of a single registration."""

    def test_deregister_instance_returns_true(self) -> None:
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService())
        assert reg.deregister(DummyService) is True
        assert reg.is_registered(DummyService) is False

    def test_deregister_singleton_returns_true(self) -> None:
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, DummyService)
        assert reg.deregister(DummyService) is True
        assert reg.is_registered(DummyService) is False

    def test_deregister_unknown_returns_false(self) -> None:
        reg = ServiceRegistry()
        assert reg.deregister(DummyService) is False

    def test_deregister_clears_init_order(self) -> None:
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService())
        reg.deregister(DummyService)
        reg.register_instance(DummyService, DummyService(value=13))
        # Re-registering after deregister leaves a single init order entry.
        assert len(reg._init_order) == 1  # noqa: SLF001


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
        reg = ServiceRegistry()
        # Anti-pattern #8: catch Exception and assert by class name — class
        # identity is unreliable under pytest-cov reimport.
        with pytest.raises(Exception, match="str") as exc:  # noqa: BLE001, PT011
            await reg.resolve(str)
        assert type(exc.value).__name__ == "ServiceNotRegisteredError"

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
        """Re-registering instance via ``replace_existing=True``
        doesn't duplicate init_order. Post-v0.31.4 GAP 3 closure,
        re-registration without ``replace_existing`` raises (see
        ``TestRegisterInstanceTeardownContract`` for the new contract);
        this test pins the deduplication invariant of init_order."""
        reg = ServiceRegistry()
        reg.register_instance(str, "a")
        reg.register_instance(str, "b", replace_existing=True)
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


class TestRegisterInstanceTeardownContract:
    """v0.31.4 GAP 3 closure: ``register_instance`` raises on duplicate
    instance + ``replace_instance`` awaits teardown.

    Pre-v0.31.4: ``register_instance`` silently overwrote, leaking
    the old instance as a zombie when it owned a running asyncio task
    (operator's dual-spawn ``audio-capture-consumer`` was the
    canonical case — two capture tasks fed the orchestrator
    simultaneously, producing 4× frame drops).
    """

    def test_duplicate_instance_raises_by_default(self) -> None:
        from sovyx.engine.errors import ServiceAlreadyRegisteredError

        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService(1))
        with pytest.raises(ServiceAlreadyRegisteredError, match="DummyService"):
            reg.register_instance(DummyService, DummyService(2))

    def test_duplicate_instance_with_replace_existing_overwrites(self) -> None:
        reg = ServiceRegistry()
        reg.register_instance(DummyService, DummyService(1))
        new = DummyService(2)
        reg.register_instance(DummyService, new, replace_existing=True)
        # Async resolve in a sync test — use the public sync access via _instances
        assert reg._instances["tests.unit.engine.test_registry.DummyService"] is new  # noqa: SLF001

    def test_factory_then_instance_does_not_raise(self) -> None:
        """Factory-first-then-instance is the canonical priority
        pattern — instance wins. Must NOT raise."""
        reg = ServiceRegistry()
        reg.register_singleton(DummyService, lambda: DummyService(1))
        # Should not raise; instance simply takes priority.
        reg.register_instance(DummyService, DummyService(2))

    @pytest.mark.asyncio()
    async def test_replace_instance_awaits_async_stop(self) -> None:
        """``replace_instance`` calls + awaits async stop() on the old."""
        teardown_log: list[str] = []

        class TornDownService:
            def __init__(self, label: str) -> None:
                self.label = label

            async def stop(self) -> None:
                teardown_log.append(f"stopped:{self.label}")

        reg = ServiceRegistry()
        old = TornDownService("old")
        new = TornDownService("new")
        reg.register_instance(TornDownService, old)
        await reg.replace_instance(TornDownService, new)
        assert teardown_log == ["stopped:old"]
        resolved = await reg.resolve(TornDownService)
        assert resolved is new

    @pytest.mark.asyncio()
    async def test_replace_instance_calls_sync_stop_too(self) -> None:
        """``replace_instance`` calls + handles sync stop() (rare but
        defensive). Some teardown methods are synchronous."""
        teardown_log: list[str] = []

        class SyncTornDownService:
            def stop(self) -> None:
                teardown_log.append("stopped")

        reg = ServiceRegistry()
        reg.register_instance(SyncTornDownService, SyncTornDownService())
        new = SyncTornDownService()
        await reg.replace_instance(SyncTornDownService, new)
        assert teardown_log == ["stopped"]

    @pytest.mark.asyncio()
    async def test_replace_instance_no_teardown_method_silent(self) -> None:
        """``replace_instance`` with no stop/cancel/aclose just overwrites
        cleanly (no error)."""

        class NoTeardownService:
            pass

        reg = ServiceRegistry()
        reg.register_instance(NoTeardownService, NoTeardownService())
        new = NoTeardownService()
        # Must not raise.
        await reg.replace_instance(NoTeardownService, new)
        assert (await reg.resolve(NoTeardownService)) is new

    @pytest.mark.asyncio()
    async def test_replace_instance_swallows_teardown_failure(self) -> None:
        """Teardown failure on the OLD instance must not block the new
        registration — the operator's enable flow always produces a
        working pipeline."""
        teardown_log: list[str] = []

        class ExplodingTeardownService:
            async def stop(self) -> None:
                teardown_log.append("attempted")
                raise RuntimeError("teardown blew up")

        reg = ServiceRegistry()
        reg.register_instance(ExplodingTeardownService, ExplodingTeardownService())
        new = ExplodingTeardownService()
        await reg.replace_instance(ExplodingTeardownService, new)
        # Old's stop was attempted; new still landed.
        assert teardown_log == ["attempted"]
        assert (await reg.resolve(ExplodingTeardownService)) is new

    @pytest.mark.asyncio()
    async def test_replace_instance_prefers_stop_over_cancel(self) -> None:
        """Order is stop → cancel → aclose. First match wins."""
        called: list[str] = []

        class MultiTeardownService:
            async def stop(self) -> None:
                called.append("stop")

            async def cancel(self) -> None:
                called.append("cancel")

            async def aclose(self) -> None:
                called.append("aclose")

        reg = ServiceRegistry()
        reg.register_instance(MultiTeardownService, MultiTeardownService())
        await reg.replace_instance(MultiTeardownService, MultiTeardownService())
        assert called == ["stop"]  # stop fired; cancel/aclose did NOT
