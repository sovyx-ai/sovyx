"""Cross-OS shim for the Windows ``IMMNotificationClient`` device-change
listener.

Voice Windows Paranoid Mission §C — push-based default-device-change
recovery (Mission spec
``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``).
Replaces the legacy 5-second polling loop
(``watchdog_default_device_poll_s``) with sub-second
``IMMNotificationClient::OnDefaultDeviceChanged`` notifications.

This module ships in v0.24.0 (foundation phase) with the contract +
factory + non-Windows / disabled no-op surface. The actual Windows
COM bindings (comtypes-based ``IMMDeviceEnumerator`` registration via
``RegisterEndpointNotificationCallback``) land in the v0.25.0 wire-up
phase (mission task T31). Until then, the Windows listener exists as
a placeholder that records the registration-not-wired metric and
logs once so operators can see the foundation is in place but the
listener is not yet active.

**Critical threading contract (anti-pattern #29 in CLAUDE.md):** when
the v0.25.0 wire-up lands, ``IMMNotificationClient`` callbacks fire on
the dedicated MMDevice notifier thread, NOT the asyncio loop. Per
Microsoft's documented contract, callback bodies MUST be non-blocking
— calling ``Stop`` / ``SetRecordingDevice`` / ``Init`` / ``Start``
inside a callback will deadlock the entire Windows audio service.
The pattern enforced by ``tools/lint_imm_callbacks.py`` (also lands
in wire-up) is: callback body limited to primitive ops, a single
``loop.call_soon_threadsafe(...)`` post, and ``return 0`` (S_OK).

Cross-OS contract: on non-Windows platforms the factory returns
``NoopMMNotificationListener`` which logs once at registration and
silently honours the lifecycle interface. Linux + macOS already have
push-based default-device events through their respective hot-plug
detectors (``_hotplug_linux.py`` + ``_hotplug_mac.py``); the IMM
listener is purely a Windows polling-loop replacement.

See:

* ``docs-internal/ADR-voice-imm-notification-recovery.md`` for the
  full design rationale + the deferred IPolicyConfig disable_sysfx
  decision.
* ``docs/modules/voice-troubleshooting-windows.md`` for the
  operator-facing flag flip procedure
  (``SOVYX_TUNING__VOICE__MM_NOTIFICATION_LISTENER_ENABLED=true``).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_hotplug_listener_registered

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


# ── Public type aliases for callback signatures ─────────────────────────


# Each callback is invoked from the asyncio loop (the listener marshals
# COM-thread events via call_soon_threadsafe before invoking these). On
# non-Windows / disabled paths these are never called.
DefaultCaptureChangedCallback = "Callable[[str], Awaitable[None]]"
"""Async callback signature for ``OnDefaultDeviceChanged`` events
filtered to ``flow=eCapture, role=eCommunications``.

Receives the new default capture endpoint's GUID string. The capture
task converts this into a ``request_device_change_restart(...)`` call
in the v0.25.0 wire-up.
"""

DeviceStateChangedCallback = "Callable[[str, int], Awaitable[None]]"
"""Async callback signature for ``OnDeviceStateChanged`` events
(device GUID, new state code: 0x1=DEVICE_STATE_ACTIVE, 0x2=DISABLED,
0x4=NOTPRESENT, 0x8=UNPLUGGED, 0xF=ALL)."""

PropertyValueChangedCallback = "Callable[[str, str], Awaitable[None]] | None"
"""Optional async callback for ``OnPropertyValueChanged`` events
(device GUID, property key string). Used to detect Voice Clarity APO
toggles in Windows Sound Settings; v0.26.0+ feature."""


@runtime_checkable
class MMNotificationListener(Protocol):
    """Platform-agnostic IMMNotificationClient subscriber contract.

    Lifecycle:

    * :meth:`register` installs the COM subscription (Windows) or
      logs a single INFO line and returns (non-Windows / disabled).
      Idempotent — a second ``register`` with the same callbacks is
      a no-op.
    * :meth:`unregister` tears the subscription down. Idempotent — the
      capture task tears down during ``stop()`` even on the path where
      ``register`` raised. Under no circumstances may ``unregister``
      block the event loop or the COM thread; the v0.25.0 wire-up
      will use a short-timeout join + fire-and-forget on stuck COM
      threads.
    """

    def register(self) -> None:
        """Install the OS subscription. Implementations log a single
        line on first call so operators can see the daemon's listener
        state at a glance."""
        ...

    def unregister(self) -> None:
        """Tear the OS subscription down. Must be idempotent."""
        ...


class NoopMMNotificationListener:
    """Cross-OS / disabled-flag fallback.

    Used in three cases:

    1. ``sys.platform != "win32"`` — Linux + macOS use their
       respective hot-plug detectors instead.
    2. ``mm_notification_listener_enabled=False`` (foundation-phase
       default through v0.25.0).
    3. ``comtypes`` is not installed (slim CI runners or a stripped-
       down Windows SKU).

    Logs a single INFO line on first :meth:`register` so an operator
    tailing ``sovyx.log`` can tell at a glance whether the daemon is
    running with degraded device-change awareness. Fires the
    ``voice.hotplug.listener.registered{registered=false}`` counter so
    fleet dashboards can split active vs no-op rates.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    def register(self) -> None:
        if self._started:
            return
        logger.info(
            "voice.mm_notification_client.noop_register",
            reason=self._reason,
        )
        record_hotplug_listener_registered(registered=False, error=self._reason)
        self._started = True

    def unregister(self) -> None:
        # Idempotent — never errors regardless of whether register was
        # called. The capture task's lifecycle calls unregister
        # unconditionally inside try/finally during stop().
        self._started = False


class WindowsMMNotificationListener:
    """Windows-only ``IMMNotificationClient`` subscriber.

    **v0.24.0 (foundation phase) — placeholder.** The full
    ``IMMDeviceEnumerator`` registration via
    ``RegisterEndpointNotificationCallback`` lands in v0.25.0 wire-up
    (mission task T31). In v0.24.0 this class's :meth:`register`
    records the
    ``voice.hotplug.listener.registered{registered=false,
    error=not_yet_wired_v024}`` counter and logs a WARN so operators
    flipping the flag prematurely see exactly why nothing happens.

    **v0.25.0+ wire-up contract (documented now to lock in the
    design):**

    * COM callbacks fire on the dedicated MMDevice notifier thread.
    * Callback bodies MUST be non-blocking (anti-pattern #29):
      primitive ops + ``self._loop.call_soon_threadsafe(dispatcher,
      *args)`` + ``return 0`` (S_OK). Anything else risks a Windows
      audio service deadlock.
    * Filter callbacks to ``flow=eCapture, role=eCommunications`` so
      headphone / speaker hot-plug events don't queue per-device-
      change restarts on a capture pipeline that's not affected.
    * Lazy-import ``comtypes`` inside :meth:`register` so non-Windows
      module imports stay clean (Linux / macOS CI workers run this
      file's import path without comtypes installed).

    Args:
        loop: The asyncio loop the COM thread will marshal events
            onto via ``call_soon_threadsafe``. Captured at
            construction time so the COM callback never has to look
            it up via :func:`asyncio.get_event_loop` (which fails on
            non-loop threads).
        on_default_capture_changed: Async callback invoked on the
            asyncio loop after a filtered ``OnDefaultDeviceChanged``
            (flow=eCapture, role=eCommunications) event.
        on_device_state_changed: Async callback invoked on the
            asyncio loop after an ``OnDeviceStateChanged`` event.
        on_property_value_changed: Optional async callback invoked
            on the asyncio loop after a ``OnPropertyValueChanged``
            event. Used to detect Voice Clarity toggles; default
            ``None`` because most callers don't need it.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_default_capture_changed: Callable[[str], Awaitable[None]],
        on_device_state_changed: Callable[[str, int], Awaitable[None]],
        on_property_value_changed: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._loop = loop
        self._on_default_capture_changed = on_default_capture_changed
        self._on_device_state_changed = on_device_state_changed
        self._on_property_value_changed = on_property_value_changed
        self._registered = False

    def register(self) -> None:
        """v0.24.0 placeholder — no actual COM registration.

        The v0.25.0 wire-up replaces this body with:

        .. code-block:: python

            import comtypes.client
            from comtypes import GUID, COMObject

            self._enumerator = comtypes.client.CreateObject(
                GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
                interface=...,  # IMMDeviceEnumerator
            )
            self._client = _ComNotificationClient(...)
            self._enumerator.RegisterEndpointNotificationCallback(
                self._client,
            )
            self._registered = True
            record_hotplug_listener_registered(registered=True)

        For v0.24.0 we log + record the not-wired metric. Operators
        flipping ``SOVYX_TUNING__VOICE__MM_NOTIFICATION_LISTENER_ENABLED=true``
        prematurely see the foundation is in place but the COM
        bindings are deferred.
        """
        if self._registered:
            return
        logger.warning(
            "voice.mm_notification_client.windows_register_not_wired",
            reason="v0.24.0 foundation phase ships the cross-OS shim "
            "+ contract; full IMMDeviceEnumerator registration via "
            "RegisterEndpointNotificationCallback lands in v0.25.0 "
            "wire-up (mission task T31). Listener is currently a "
            "no-op even when the flag is True.",
            target_version="v0.25.0",
        )
        record_hotplug_listener_registered(
            registered=False,
            error="not_yet_wired_v024",
        )
        self._registered = True

    def unregister(self) -> None:
        # v0.24.0 placeholder — no COM resource to release. v0.25.0
        # wire-up will UnregisterEndpointNotificationCallback +
        # release the enumerator + COMObject inside try/finally,
        # honouring a short timeout to avoid blocking shutdown when
        # the COM thread is wedged.
        self._registered = False


def create_listener(
    loop: asyncio.AbstractEventLoop,
    on_default_capture_changed: Callable[[str], Awaitable[None]],
    on_device_state_changed: Callable[[str, int], Awaitable[None]],
    on_property_value_changed: Callable[[str, str], Awaitable[None]] | None = None,
    *,
    enabled: bool = False,
) -> MMNotificationListener:
    """Factory — return the right listener for the current platform + flag.

    The cross-OS shim contract:

    * ``sys.platform != "win32"`` → :class:`NoopMMNotificationListener`
      with ``reason="non_windows_platform"``. Linux + macOS hot-plug
      events flow through their dedicated detectors.
    * ``enabled=False`` (foundation default through v0.25.0
      promotion) → :class:`NoopMMNotificationListener` with
      ``reason="flag_disabled"``.
    * Windows + ``enabled=True`` →
      :class:`WindowsMMNotificationListener`. In v0.24.0 this is the
      placeholder that logs + records the not-wired metric; in
      v0.25.0 wire-up it becomes the real COM subscriber.

    The ``enabled`` flag should always come from the resolved
    ``EngineConfig.tuning.voice.mm_notification_listener_enabled``
    setting, NOT from a parallel master switch (anti-pattern #12).

    Args:
        loop: asyncio loop used by the Windows path to marshal COM-
            thread events. Required even on non-Windows so the call
            site doesn't need to branch.
        on_default_capture_changed: Async callback for filtered
            ``OnDefaultDeviceChanged`` events (capture role).
        on_device_state_changed: Async callback for
            ``OnDeviceStateChanged`` events.
        on_property_value_changed: Optional callback for
            ``OnPropertyValueChanged`` events.
        enabled: Master gate — when ``False`` the factory always
            returns the no-op listener regardless of platform.

    Returns:
        A listener honouring the :class:`MMNotificationListener`
        protocol. The caller invokes ``register()`` / ``unregister()``
        without branching on the concrete type.
    """
    if not enabled:
        return NoopMMNotificationListener(reason="flag_disabled")
    if sys.platform != "win32":
        return NoopMMNotificationListener(reason="non_windows_platform")
    return WindowsMMNotificationListener(
        loop=loop,
        on_default_capture_changed=on_default_capture_changed,
        on_device_state_changed=on_device_state_changed,
        on_property_value_changed=on_property_value_changed,
    )


__all__ = [
    "MMNotificationListener",
    "NoopMMNotificationListener",
    "WindowsMMNotificationListener",
    "create_listener",
]
