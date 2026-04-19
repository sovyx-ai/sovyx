"""Linux ``udev``-backed hot-plug listener.

ADR §4.4.2 — subscribe to ``subsystem="sound"`` on a
:class:`pyudev.Context` and translate every ``add`` / ``remove``
notification into a :class:`HotplugEvent`.

``pyudev`` is an optional dependency: the daemon ships with voice-core
extras but a user on a Linux-minimal install (no ``libudev``) can still
run Sovyx. When the import fails we degrade gracefully to
:class:`NoopHotplugListener` so the rest of the watchdog keeps
working.

The real listener runs the blocking :meth:`pyudev.Monitor.poll` on a
daemon thread and marshals events onto the asyncio loop via
:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` — never mutate
loop state from the worker directly.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener
from sovyx.voice.health.contract import HotplugEvent, HotplugEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


_POLL_TIMEOUT_S = 1.0
"""How long the worker blocks on :meth:`Monitor.poll` between checks.

Short enough that :meth:`stop` observes the shutdown flag within a
second; long enough that we don't spin-busy the CPU on an idle rig.
"""


class LinuxHotplugListener:
    """pyudev-backed subscriber. Initialised by :func:`build_linux_hotplug_listener`."""

    def __init__(self, *, pyudev_module: Any) -> None:  # noqa: ANN401 — external pyudev
        self._pyudev = pyudev_module
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: Callable[[HotplugEvent], Awaitable[None]] | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._monitor: Any | None = None
        self._context: Any | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._on_event = on_event
        try:
            ctx = self._pyudev.Context()
            monitor = self._pyudev.Monitor.from_netlink(ctx)
            monitor.filter_by(subsystem="sound")
            monitor.start()
        except Exception as exc:  # noqa: BLE001 — surface to caller + degrade
            logger.warning(
                "voice_hotplug_listener_start_failed",
                platform="linux",
                error=str(exc),
                exc_info=True,
            )
            return
        self._context = ctx
        self._monitor = monitor
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sovyx-voice-hotplug-linux",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        logger.info("voice_hotplug_listener_started", platform="linux")

    async def stop(self) -> None:
        thread = self._thread
        self._started = False
        self._stop_flag.set()
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
            if thread.is_alive():
                logger.error(
                    "voice_hotplug_listener_stop_timeout",
                    platform="linux",
                )
        self._thread = None
        self._monitor = None
        self._context = None

    def _run(self) -> None:
        monitor = self._monitor
        if monitor is None:
            return
        while not self._stop_flag.is_set():
            try:
                device = monitor.poll(timeout=_POLL_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — transient udev errors must not kill thread
                logger.debug("voice_hotplug_listener_poll_raised", exc_info=True)
                continue
            if device is None:
                continue
            action = getattr(device, "action", None)
            if action == "add":
                kind = HotplugEventKind.DEVICE_ADDED
            elif action == "remove":
                kind = HotplugEventKind.DEVICE_REMOVED
            else:
                continue
            friendly = _friendly_name(device)
            event = HotplugEvent(
                kind=kind,
                endpoint_guid=None,
                device_friendly_name=friendly or None,
                device_interface_name=getattr(device, "device_path", None) or None,
            )
            self._dispatch(event)

    def _dispatch(self, event: HotplugEvent) -> None:
        loop = self._loop
        on_event = self._on_event
        if loop is None or on_event is None:
            return
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(on_event(event), loop=loop),
            )
        except RuntimeError:
            logger.debug("voice_hotplug_dispatch_loop_closed", exc_info=True)


def _friendly_name(device: Any) -> str:  # noqa: ANN401 — pyudev.Device is external
    """Best-effort friendly name for a udev sound device.

    ALSA exposes the label through ``ID_MODEL`` / ``ID_MODEL_FROM_DATABASE``
    on the parent USB device; a built-in PCI card only has the kernel
    driver name. Fall back to the device path when nothing else is
    available so the watchdog still has a stable identifier.
    """
    for key in ("ID_MODEL_FROM_DATABASE", "ID_MODEL", "NAME"):
        value = device.get(key) if hasattr(device, "get") else None
        if value:
            return str(value)
    return str(getattr(device, "device_path", "") or "")


def build_linux_hotplug_listener() -> HotplugListener:
    """Return a :class:`LinuxHotplugListener`, or no-op when ``pyudev`` is absent."""
    try:
        import pyudev  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "voice_hotplug_listener_unavailable",
            platform="linux",
            reason="pyudev_not_installed",
        )
        return NoopHotplugListener(reason="pyudev not installed")
    return LinuxHotplugListener(pyudev_module=pyudev)


__all__ = [
    "LinuxHotplugListener",
    "build_linux_hotplug_listener",
]
