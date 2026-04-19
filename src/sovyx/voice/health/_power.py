"""Power-event listener contract for ADR §4.4.4.

Every platform-specific listener (``_power_win`` on Windows;
Linux/macOS implementations land in Sprint 4) honours the
:class:`PowerEventListener` protocol so the watchdog stays platform-agnostic.

Design mirrors :mod:`~sovyx.voice.health._hotplug`:

* :meth:`PowerEventListener.start` installs the OS subscription and
  starts forwarding :class:`~sovyx.voice.health.contract.PowerEvent`
  instances into the caller's asyncio callback. Idempotent.
* :meth:`PowerEventListener.stop` tears the subscription down. Idempotent
  and must honour a short timeout — shutdown cannot be blocked by a
  stuck OS thread.

When ``runtime_resilience_enabled`` is ``False`` or the platform-specific
helper is absent (``pywin32`` on Windows, D-Bus on Linux, pyobjc on
macOS) callers receive :class:`NoopPowerEventListener` which answers
``start``/``stop`` without side-effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice.health.contract import PowerEvent

logger = get_logger(__name__)


class PowerEventListener(Protocol):
    """Platform-agnostic OS power-management event source (ADR §4.4.4)."""

    async def start(
        self,
        on_event: Callable[[PowerEvent], Awaitable[None]],
    ) -> None:
        """Install the OS subscription and forward events to ``on_event``."""
        ...

    async def stop(self) -> None:
        """Tear the OS subscription down. Must be idempotent."""
        ...


class NoopPowerEventListener:
    """Listener that consumes the contract without touching the OS.

    Used as the watchdog's fallback whenever the native backend is not
    available — on Windows when ``USER32`` / message-only windows are
    unavailable (Nano Server), on Linux when ``dbus-next`` is missing,
    on macOS for the whole of Sprints 1-3 (native backend lands in
    Sprint 4), and everywhere when ``tuning.voice.runtime_resilience_enabled``
    is ``False``.

    Logs a single INFO line on the first :meth:`start` so operators
    tailing ``sovyx.log`` can see the degraded mode at a glance.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    async def start(
        self,
        on_event: Callable[[PowerEvent], Awaitable[None]],
    ) -> None:
        del on_event  # intentionally discarded
        if not self._started:
            logger.info("voice_power_listener_noop", reason=self._reason)
            self._started = True

    async def stop(self) -> None:
        self._started = False


__all__ = [
    "NoopPowerEventListener",
    "PowerEventListener",
]
