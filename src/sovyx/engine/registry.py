"""Sovyx ServiceRegistry — lightweight DI container.

Lazy singleton factories + pre-built instances.
Shutdown in reverse initialization order.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

from sovyx.engine.errors import ServiceNotRegisteredError
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class ServiceRegistry:
    """Lightweight DI container (~100 LOC).

    Two registration modes:
    - register_singleton(interface, factory): lazy instantiation on first resolve()
    - register_instance(interface, instance): ready instance, resolve() returns it

    Shutdown: reverse init order. Calls .shutdown() if it exists.
    Thread safety: not needed (single-threaded asyncio).
    """

    def __init__(self) -> None:
        self._factories: dict[type[object], Callable[..., object]] = {}
        self._instances: dict[type[object], object] = {}
        self._init_order: list[type[object]] = []

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
        if interface in self._factories or interface in self._instances:
            logger.warning(
                "service_overwritten",
                interface=interface.__name__,
            )
        self._factories[interface] = factory

    def register_instance(
        self,
        interface: type[T],
        instance: T,
    ) -> None:
        """Register a ready instance.

        Args:
            interface: The type/protocol to register.
            instance: Pre-built instance.
        """
        if interface in self._factories or interface in self._instances:
            logger.warning(
                "service_overwritten",
                interface=interface.__name__,
            )
        self._instances[interface] = instance
        if interface not in self._init_order:
            self._init_order.append(interface)

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
        # Check cached instances first
        if interface in self._instances:
            return self._instances[interface]  # type: ignore[return-value]

        # Check factories
        if interface in self._factories:
            factory = self._factories[interface]
            instance = factory()
            self._instances[interface] = instance
            if interface not in self._init_order:
                self._init_order.append(interface)
            return instance  # type: ignore[return-value]

        msg = f"Service not registered: {interface.__name__}"
        raise ServiceNotRegisteredError(msg)

    def is_registered(self, interface: type[object]) -> bool:
        """Check if interface has a registration."""
        return interface in self._factories or interface in self._instances

    async def shutdown_all(self) -> None:
        """Shutdown all services in reverse init order.

        Calls shutdown() (if exists) on each service.
        Exceptions logged but not propagated (best-effort).
        """
        for iface in reversed(self._init_order):
            instance = self._instances.get(iface)
            if instance is None:
                continue
            shutdown = getattr(instance, "shutdown", None)
            if shutdown is None:
                continue
            with contextlib.suppress(Exception):
                logger.debug(
                    "service_shutting_down",
                    service=iface.__name__,
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
