"""Default-input-device watcher contract for ADR §4.4.3.

When the user changes the default mic from Sound Settings (or a game
audio app switches it programmatically) the watchdog must switch the
active endpoint, invalidate stale ComboStore state for the previous
default, and re-cascade the new default. Native notification paths
exist on every platform (``IMMNotificationClient`` on Windows,
PulseAudio/PipeWire ``default-source-changed`` on Linux,
``kAudioHardwarePropertyDefaultInputDevice`` on macOS) but all three
are heavy — COM objects, D-Bus subscriptions, CoreAudio property
listeners — and polling the default device through ``sounddevice`` at
modest cadence (tuning: ``watchdog_default_device_poll_s`` — 5 s by
default) is sufficient for personal-daemon UX.

The default watcher is intentionally decoupled from the hot-plug
listener (both on Windows and elsewhere): a user changing their
default mic is a logically distinct event from plugging/unplugging a
device, and the two handlers have different downstream contracts.

Concrete polling backend: :class:`PollingDefaultDeviceWatcher`. Callers
can provide their own ``query_default`` to feed test-doubles or native
notifications (Sprint 4) without changing the interface.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.health.contract import HotplugEvent, HotplugEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


_DEFAULT_POLL_S = _VoiceTuning().watchdog_default_device_poll_s
"""Cadence used by :class:`PollingDefaultDeviceWatcher` when the caller
does not override it."""


class DefaultDeviceWatcher(Protocol):
    """Platform-agnostic default-input-device change source (ADR §4.4.3)."""

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        """Begin watching and forward transitions to ``on_event``."""
        ...

    async def stop(self) -> None:
        """Cancel the watcher. Must be idempotent."""
        ...


class NoopDefaultDeviceWatcher:
    """Watcher that never reports a default-change. Used when the
    runtime-resilience kill-switch is off or when the underlying
    default-query helper is unavailable.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        del on_event
        if not self._started:
            logger.info("voice_default_device_watcher_noop", reason=self._reason)
            self._started = True

    async def stop(self) -> None:
        self._started = False


class PollingDefaultDeviceWatcher:
    """Generic poller for the default input device.

    Args:
        query_default: Sync callable returning a stable identifier of
            the current default input (device name, index, or any
            hashable). Called on an asyncio ``to_thread`` so a slow
            PortAudio enumeration cannot stall the event loop.
        poll_interval_s: How often the identifier is re-read.

    The watcher fires one :class:`HotplugEvent` with kind
    ``DEFAULT_DEVICE_CHANGED`` whenever the value returned by
    ``query_default`` differs from the previous read. The first read
    after ``start`` establishes the baseline and does not fire an event.
    Exceptions from ``query_default`` are swallowed with a WARNING so
    PortAudio hiccups do not kill the poller.
    """

    def __init__(
        self,
        *,
        query_default: Callable[[], object],
        poll_interval_s: float | None = None,
    ) -> None:
        self._query = query_default
        self._interval = poll_interval_s if poll_interval_s is not None else _DEFAULT_POLL_S
        if self._interval <= 0:
            msg = f"poll_interval_s must be > 0, got {self._interval}"
            raise ValueError(msg)
        self._task: asyncio.Task[None] | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._started = True
        self._task = spawn(self._run(on_event), name="voice-default-device-watcher")
        logger.info(
            "voice_default_device_watcher_started",
            poll_interval_s=self._interval,
        )

    async def stop(self) -> None:
        self._started = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        last: object | None = None
        seeded = False
        while self._started:
            try:
                current = await asyncio.to_thread(self._query)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — transient enumerate errors must not kill poller
                logger.debug("voice_default_device_query_raised", exc_info=True)
                current = None
            if not seeded:
                last = current
                seeded = True
            elif current is not None and current != last:
                logger.info(
                    "voice_default_device_changed",
                    previous=str(last),
                    current=str(current),
                )
                logger.info(
                    "audio.device.default_changed",
                    **{
                        "voice.previous": str(last) if last is not None else None,
                        "voice.current": str(current),
                        "voice.poll_interval_s": self._interval,
                    },
                )
                last = current
                try:
                    await on_event(
                        HotplugEvent(
                            kind=HotplugEventKind.DEFAULT_DEVICE_CHANGED,
                            endpoint_guid=None,
                            device_friendly_name=str(current),
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("voice_default_device_dispatch_failed", exc_info=True)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return


__all__ = [
    "DefaultDeviceWatcher",
    "NoopDefaultDeviceWatcher",
    "PollingDefaultDeviceWatcher",
]
