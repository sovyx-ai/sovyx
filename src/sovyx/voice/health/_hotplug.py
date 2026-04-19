"""Common hot-plug listener contract used by :mod:`watchdog`.

Every platform-specific listener (:mod:`_hotplug_win`,
:mod:`_hotplug_linux`, :mod:`_hotplug_mac`) must honour the
:class:`HotplugListener` protocol so the watchdog stays platform-agnostic.

The listener lifecycle is intentionally small:

* :meth:`HotplugListener.start` installs the OS subscription and begins
  forwarding :class:`~sovyx.voice.health.contract.HotplugEvent` instances
  into the caller's asyncio callback. It must be idempotent — a second
  ``start`` with the same callback is a no-op.
* :meth:`HotplugListener.stop` tears the subscription down. It must be
  idempotent — the factory tears down during daemon shutdown even on
  the path where ``start`` raised. Under no circumstances may ``stop``
  block the event loop; any join/wait must honour a short timeout and
  fall back to fire-and-forget so a stuck OS thread can never prevent
  shutdown.

Listeners must never dispatch directly onto the caller's event loop —
they always marshal via :func:`asyncio.run_coroutine_threadsafe` /
:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` so the OS-owned
worker thread never touches loop state concurrently.

When ``runtime_resilience_enabled`` is ``False`` or the platform's
native helper (``pywin32`` on Windows, ``pyudev`` on Linux) is absent,
callers receive :class:`NoopHotplugListener` which answers ``start`` /
``stop`` without side-effects; this keeps the factory glue identical
regardless of the platform state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice.health.contract import HotplugEvent

logger = get_logger(__name__)


class HotplugListener(Protocol):
    """Platform-agnostic OS audio-device hot-plug event source."""

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        """Install the OS subscription and forward events to ``on_event``."""
        ...

    async def stop(self) -> None:
        """Tear the OS subscription down. Must be idempotent."""
        ...


class NoopHotplugListener:
    """Listener that consumes the contract without touching the OS.

    Used as the watchdog's fallback whenever the native backend is not
    available — on Linux when :mod:`pyudev` isn't installed, on Windows
    when :mod:`pywin32` isn't installed, on macOS for the whole of
    Sprint 2 (native backend lands in Sprint 4 Task #28), and
    everywhere when ``tuning.voice.runtime_resilience_enabled`` is
    ``False``.

    The class logs a single INFO line on the first :meth:`start` so an
    operator tailing ``sovyx.log`` can tell at a glance whether the
    daemon is running with degraded hot-plug awareness.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        del on_event  # intentionally discarded
        if not self._started:
            logger.info("voice_hotplug_listener_noop", reason=self._reason)
            self._started = True

    async def stop(self) -> None:
        self._started = False


__all__ = [
    "HotplugListener",
    "NoopHotplugListener",
]
