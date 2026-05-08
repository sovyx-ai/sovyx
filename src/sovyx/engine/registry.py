"""Sovyx ServiceRegistry — lightweight DI container.

Lazy singleton factories + pre-built instances.
Shutdown in reverse initialization order.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from sovyx.engine.errors import (
    ServiceAlreadyRegisteredError,
    ServiceNotRegisteredError,
)
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


def _key(interface: type) -> str:
    # Qualified name survives module reimport under pytest-cov/xdist — plain
    # class identity does not.
    return f"{interface.__module__}.{interface.__qualname__}"


class ServiceRegistry:
    """Lightweight DI container (~100 LOC).

    Two registration modes:
    - register_singleton(interface, factory): lazy instantiation on first resolve()
    - register_instance(interface, instance): ready instance, resolve() returns it

    Shutdown: reverse init order. Calls .shutdown() if it exists.
    Thread safety: not needed (single-threaded asyncio).
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., object]] = {}
        self._instances: dict[str, object] = {}
        self._init_order: list[str] = []

    def register_singleton(
        self,
        interface: type[T],
        factory: Callable[..., T],
    ) -> None:
        """Register factory. Instance created lazily on first resolve().

        Args:
            interface: The type/protocol to register.
            factory: Callable that returns an instance (sync or async).
        """
        key = _key(interface)
        if key in self._factories or key in self._instances:
            logger.warning(
                "service_overwritten",
                interface=interface.__name__,
            )
        self._factories[key] = factory

    def register_instance(
        self,
        interface: type[T],
        instance: T,
        *,
        replace_existing: bool = False,
    ) -> None:
        """Register a ready instance.

        Args:
            interface: The type/protocol to register.
            instance: Pre-built instance.
            replace_existing: When True, an existing instance is
                replaced (without teardown — synchronous registration
                cannot await async teardown). When False (default),
                duplicate registration raises
                :class:`ServiceAlreadyRegisteredError` so accidental
                double-registration is loud rather than silent.

        Raises:
            ServiceAlreadyRegisteredError: when ``replace_existing``
                is False and the interface is already registered AS AN
                INSTANCE (not just as a factory). Factory-then-instance
                is the canonical "instance takes priority" pattern and
                does NOT raise.

        Note:
            Sites that legitimately need to replace a running instance
            (e.g. voice pipeline re-enable) should call
            :meth:`replace_instance` instead — that method awaits the
            old instance's teardown method (``stop`` / ``cancel`` /
            ``aclose``) before overwriting, preventing the zombie-task
            class of bugs (v0.31.4 GAP 3 — operator's dual-spawn
            ``audio-capture-consumer``).
        """
        key = _key(interface)
        if key in self._instances:
            if not replace_existing:
                raise ServiceAlreadyRegisteredError(
                    f"Service {interface.__name__!r} already registered as an "
                    f"instance. Pass replace_existing=True OR call "
                    f"replace_instance() (which also tears down the old "
                    f"instance via stop/cancel/aclose) to override."
                )
            logger.warning(
                "service_overwritten",
                interface=interface.__name__,
                replace_existing=True,
            )
        self._instances[key] = instance
        if key not in self._init_order:
            self._init_order.append(key)

    async def replace_instance(
        self,
        interface: type[T],
        instance: T,
    ) -> None:
        """Replace an existing instance + await teardown of the old one.

        v0.31.4 GAP 3 closure: pre-v0.31.4 ``register_instance``
        silently overwrote, leaking the old instance as a zombie if
        it owned a running asyncio task (operator's dual-spawn
        ``audio-capture-consumer`` was the canonical case — two
        capture tasks fed the same orchestrator simultaneously,
        producing 4× frame drops + chaotic VAD input). The async
        ``replace_instance`` looks for a teardown method on the old
        instance (``stop`` / ``cancel`` / ``aclose``) and awaits it
        before overwriting. If no teardown method exists, behaviour
        matches ``register_instance(replace_existing=True)`` (silent
        overwrite + warn).

        Args:
            interface: The type/protocol to register.
            instance: New instance to register.

        Note:
            ``stop`` / ``cancel`` / ``aclose`` are awaited if they
            return an awaitable; called as sync if they return a
            non-awaitable (defensive — some teardown methods are
            sync). Exceptions during teardown are logged + swallowed
            so the new instance always lands; teardown failure on the
            OLD instance must NOT block re-registration of the new.
        """
        import inspect

        key = _key(interface)
        old = self._instances.get(key)
        if old is not None:
            for method_name in ("stop", "cancel", "aclose"):
                method = getattr(old, method_name, None)
                if method is None or not callable(method):
                    continue
                try:
                    result = method()
                    if inspect.isawaitable(result):
                        await result
                    logger.info(
                        "service_teardown_succeeded",
                        interface=interface.__name__,
                        method=method_name,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Teardown failure must not block new registration —
                    # swallow + log so the operator's enable flow always
                    # produces a working pipeline. The zombie risk is
                    # bounded because the old instance is dropped from
                    # the registry below regardless.
                    logger.warning(
                        "service_teardown_failed",
                        interface=interface.__name__,
                        method=method_name,
                        reason=str(exc)[:200],
                    )
                break
            logger.info(
                "service_replaced",
                interface=interface.__name__,
            )
        self._instances[key] = instance
        if key not in self._init_order:
            self._init_order.append(key)

    async def resolve(self, interface: type[T]) -> T:
        """Resolve interface to instance.

        Singleton: creates on first call, caches.
        Instance: returns directly.

        Args:
            interface: The type to resolve.

        Returns:
            The registered instance.

        Raises:
            ServiceNotRegisteredError: If interface not registered.
        """
        key = _key(interface)
        # Check cached instances first
        if key in self._instances:
            return cast("T", self._instances[key])

        # Check factories
        if key in self._factories:
            factory = self._factories[key]
            instance = factory()
            self._instances[key] = instance
            if key not in self._init_order:
                self._init_order.append(key)
            return cast("T", instance)

        msg = f"Service not registered: {interface.__name__}"
        raise ServiceNotRegisteredError(msg)

    def is_registered(self, interface: type[object]) -> bool:
        """Check if interface has a registration."""
        key = _key(interface)
        return key in self._factories or key in self._instances

    def deregister(self, interface: type[object]) -> bool:
        """Remove any registration for *interface*.

        Returns:
            ``True`` if something was removed, ``False`` if the
            interface wasn't registered. Does **not** call
            ``shutdown()`` on the instance — the caller is expected to
            stop the service first.
        """
        key = _key(interface)
        removed = False
        if key in self._instances:
            del self._instances[key]
            removed = True
        if key in self._factories:
            del self._factories[key]
            removed = True
        if key in self._init_order:
            self._init_order.remove(key)
        return removed

    async def shutdown_all(self) -> None:
        """Shutdown all services in reverse init order.

        Calls shutdown() (if exists) on each service.
        Exceptions logged but not propagated (best-effort).
        """
        for key in reversed(self._init_order):
            instance = self._instances.get(key)
            if instance is None:
                continue
            shutdown = getattr(instance, "shutdown", None)
            if shutdown is None:
                continue
            with contextlib.suppress(Exception):
                logger.debug(
                    "service_shutting_down",
                    service=key,
                )
                if callable(shutdown):
                    result = shutdown()
                    # Handle async shutdown
                    if hasattr(result, "__await__"):
                        await result

        self._instances.clear()
        self._factories.clear()
        self._init_order.clear()
        logger.info("registry_shutdown_complete")
