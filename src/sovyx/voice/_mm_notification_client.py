"""Cross-OS shim for the Windows ``IMMNotificationClient`` device-change
listener.

Voice Windows Paranoid Mission §C — push-based default-device-change
recovery (Mission spec
``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``).
Replaces the legacy 5-second polling loop
(``watchdog_default_device_poll_s``) with sub-second
``IMMNotificationClient::OnDefaultDeviceChanged`` notifications.

The cross-OS shim ships in v0.24.0 (foundation phase) with the
contract + factory + non-Windows / disabled no-op surface. The
actual Windows COM bindings — ``comtypes``-based
``IMMDeviceEnumerator`` registration via
``RegisterEndpointNotificationCallback`` — land in v0.25.0 wire-up
(mission task T31, this commit). The Windows listener is gated by
``mm_notification_listener_enabled`` (default False through v0.25.0
opt-in adoption phase; default-flip planned for v0.26.0).

**Critical threading contract (anti-pattern #29 in CLAUDE.md):**
``IMMNotificationClient`` callbacks fire on the dedicated MMDevice
notifier thread, NOT the asyncio loop. Per Microsoft's documented
contract, callback bodies MUST be non-blocking — calling
``Stop`` / ``SetRecordingDevice`` / ``Init`` / ``Start`` inside a
callback will deadlock the entire Windows audio service. The pattern
enforced by ``tools/lint_imm_callbacks.py`` is: callback body limited
to primitive ops, a single ``loop.call_soon_threadsafe(...)`` post,
and ``return 0`` (S_OK). The lint runs as a CI gate on every pytest
run so a regression that reintroduces a blocking call is caught
before it ever reaches an operator's machine.

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

import asyncio
import sys
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_hotplug_listener_registered

if TYPE_CHECKING:
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


# ── Windows COM constants (MSDN-canonical) ──────────────────────────


# CLSID for ``CLSID_MMDeviceEnumerator``. Documented in
# ``mmdeviceapi.h`` in the Windows SDK; the Audio Endpoint
# component server creates instances of this class. Stable across
# every Windows version since Vista.
_CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

# IID for ``IMMDeviceEnumerator``. Used as the requested interface
# when activating the MMDeviceEnumerator class via
# ``CoCreateInstance``.
_IID_IMM_DEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"

# IID for ``IMMNotificationClient``. The COM interface our
# subclass implements; the audio engine's notifier thread uses
# this IID via ``QueryInterface`` after we register.
_IID_IMM_NOTIFICATION_CLIENT = "{7991EEC9-7E89-4D85-8390-6C703CEC60C0}"

# ``EDataFlow`` enum values.
_EDATAFLOW_E_RENDER = 0
_EDATAFLOW_E_CAPTURE = 1

# ``ERole`` enum values.
_EROLE_E_CONSOLE = 0
_EROLE_E_MULTIMEDIA = 1
_EROLE_E_COMMUNICATIONS = 2

# DEVICE_STATE_* bitfield values from the audio engine. The
# Windows audio service publishes one of these (or a bitmask
# union) when an endpoint transitions state.
_DEVICE_STATE_ACTIVE = 0x00000001
_DEVICE_STATE_DISABLED = 0x00000002
_DEVICE_STATE_NOT_PRESENT = 0x00000004
_DEVICE_STATE_UNPLUGGED = 0x00000008


def _build_com_bindings() -> tuple[type, type, type] | None:
    """Lazy-build the ``comtypes`` interface + COMObject definitions.

    Returns a ``(IMMNotificationClient, IMMDeviceEnumerator,
    PROPERTYKEY)`` tuple if ``comtypes`` is available on this
    interpreter, ``None`` otherwise. Called once per
    :meth:`WindowsMMNotificationListener.register`; the result is
    cached on the listener instance so subsequent register calls
    don't re-resolve the import.

    Defining the comtypes interface classes inside this function
    (rather than at module top-level) is the v0.25.0 contract that
    keeps the module importable on non-Windows / slim-CI hosts
    where ``comtypes`` isn't installed. The interfaces define the
    vtable shape from
    `mmdeviceapi.h <https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/>`__
    — method order MUST exactly match the C header so
    ``comtypes`` resolves the vtable correctly.
    """
    try:
        import ctypes
        from ctypes import POINTER, c_uint, c_wchar_p

        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    except ImportError:
        return None

    class _Propertykey(ctypes.Structure):
        """``PROPERTYKEY`` struct passed by-value to OnPropertyValueChanged."""

        _fields_ = (("fmtid", GUID), ("pid", c_uint))

    class _IMMNotificationClient(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed; mypy strict disallow_subclassing_any
        """The COM interface our COMObject subclass implements.

        Method order MUST match ``mmdeviceapi.h``:

        1. OnDeviceStateChanged
        2. OnDeviceAdded
        3. OnDeviceRemoved
        4. OnDefaultDeviceChanged
        5. OnPropertyValueChanged
        """

        _iid_ = GUID(_IID_IMM_NOTIFICATION_CLIENT)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "OnDeviceStateChanged",
                (["in"], c_wchar_p, "device_id"),
                (["in"], c_uint, "new_state"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OnDeviceAdded",
                (["in"], c_wchar_p, "device_id"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OnDeviceRemoved",
                (["in"], c_wchar_p, "device_id"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OnDefaultDeviceChanged",
                (["in"], c_uint, "data_flow"),
                (["in"], c_uint, "role"),
                (["in"], c_wchar_p, "default_device_id"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OnPropertyValueChanged",
                (["in"], c_wchar_p, "device_id"),
                (["in"], _Propertykey, "key"),
            ),
        ]

    class _IMMDeviceEnumerator(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed; mypy strict disallow_subclassing_any
        """Subset of ``mmdeviceapi.h`` we actually call.

        Vtable order MUST match ``mmdeviceapi.h``:

        1. EnumAudioEndpoints
        2. GetDefaultAudioEndpoint
        3. GetDevice
        4. RegisterEndpointNotificationCallback
        5. UnregisterEndpointNotificationCallback
        """

        _iid_ = GUID(_IID_IMM_DEVICE_ENUMERATOR)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "EnumAudioEndpoints",
                (["in"], c_uint, "data_flow"),
                (["in"], c_uint, "state_mask"),
                (["out"], POINTER(POINTER(IUnknown)), "device_collection"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetDefaultAudioEndpoint",
                (["in"], c_uint, "data_flow"),
                (["in"], c_uint, "role"),
                (["out"], POINTER(POINTER(IUnknown)), "endpoint"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetDevice",
                (["in"], c_wchar_p, "id"),
                (["out"], POINTER(POINTER(IUnknown)), "device"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "RegisterEndpointNotificationCallback",
                (["in"], POINTER(_IMMNotificationClient), "client"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "UnregisterEndpointNotificationCallback",
                (["in"], POINTER(_IMMNotificationClient), "client"),
            ),
        ]

    return (_IMMNotificationClient, _IMMDeviceEnumerator, _Propertykey)


class WindowsMMNotificationListener:
    """Windows-only ``IMMNotificationClient`` subscriber.

    **v0.25.0 wire-up.** :meth:`register` lazy-imports ``comtypes``,
    resolves the ``IMMDeviceEnumerator`` COM class, instantiates an
    inner :class:`_ComNotificationClient` (our COMObject subclass
    implementing ``IMMNotificationClient``), and calls
    ``RegisterEndpointNotificationCallback`` on the enumerator. From
    that point on the Windows audio service invokes our 5 callbacks
    on its dedicated MMDevice notifier thread; each callback marshals
    the event onto the asyncio loop via ``call_soon_threadsafe`` and
    returns ``S_OK`` immediately (anti-pattern #29 contract,
    AST-enforced by ``tools/lint_imm_callbacks.py``).

    **Defensive design — no path crashes the daemon.** Every COM
    boundary is wrapped in ``try/except BaseException``. On any
    failure (``ImportError`` for missing ``comtypes``, ``COMError``
    for marshalling / driver issues, transient ``OSError``) the
    listener falls through to the no-op semantics: the metric
    counter records the registered=False outcome with a stable
    error tag, and a structured WARN surfaces the cause. The
    cross-OS factory's
    :class:`NoopMMNotificationListener` continues to honour
    register/unregister silently in those cases. Worst case is
    "device-change auto-recovery off"; the daemon never crashes.

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
        # Live COM resources held by register() — released in
        # unregister(). ``None`` means no live registration. Typed
        # as ``Any`` because the comtypes-generated POINTER types
        # are dynamic; the mypy override on ``comtypes.*`` keeps
        # this from polluting the strict-type surface.
        self._enumerator: Any = None
        self._com_client: Any = None

    def register(self) -> None:  # noqa: PLR0911 — every COM boundary needs its own early-return path
        """Register the COM callback with the audio engine.

        Idempotent: calling :meth:`register` twice is a no-op on the
        second call. On any failure path (missing comtypes, COM
        marshalling error, driver bug) the listener falls through
        to the no-op semantics — see class docstring for the
        defensive-design contract.
        """
        if self._registered:
            return

        # Step 1 — resolve comtypes + define interface classes.
        bindings = _build_com_bindings()
        if bindings is None:
            logger.warning(
                "voice.mm_notification_client.comtypes_unavailable",
                reason=(
                    "comtypes import failed; cannot register the IMM "
                    "listener. Install via `pip install sovyx[voice]` on "
                    "Windows or accept degraded device-change detection "
                    "(the watchdog's polling fallback continues to run)."
                ),
            )
            record_hotplug_listener_registered(
                registered=False,
                error="comtypes_import_failed",
            )
            self._registered = True
            return

        imm_notification_client_cls, imm_device_enumerator_cls, _ = bindings

        # Step 2 — define the concrete COMObject subclass. Lazy
        # because ``comtypes.COMObject`` only exists when comtypes
        # imported successfully.
        try:
            from comtypes import COMObject
        except ImportError:
            # Defensive — should never hit since _build_com_bindings
            # already succeeded, but the ``except ImportError`` here
            # documents the lazy-import contract for readers.
            record_hotplug_listener_registered(
                registered=False,
                error="comtypes_import_failed",
            )
            self._registered = True
            return

        class _ComNotificationClient(COMObject):  # type: ignore[misc]  # comtypes COMObject is Any-typed; mypy strict disallow_subclassing_any
            """Concrete IMMNotificationClient implementation.

            Each callback method MUST be non-blocking — the COM
            notifier thread blocks until the method returns, and
            blocking callbacks deadlock the entire Windows audio
            service. Anti-pattern #29 enforced by
            ``tools/lint_imm_callbacks.py``: body is restricted to
            primitive ops + a single ``call_soon_threadsafe(...)``
            + ``return 0`` (S_OK).
            """

            _com_interfaces_ = [imm_notification_client_cls]

            def __init__(  # noqa: D107 — internal class
                self,
                listener: WindowsMMNotificationListener,
            ) -> None:
                super().__init__()
                self._listener = listener

            def OnDefaultDeviceChanged(  # noqa: N802, PLR0913 — COM signature
                self,
                this: object,
                data_flow: int,
                role: int,
                default_device_id: str | None,
            ) -> int:
                # Filter: only eCapture + eCommunications. Other
                # flows don't affect the capture pipeline.
                if (
                    data_flow == _EDATAFLOW_E_CAPTURE
                    and role == _EROLE_E_COMMUNICATIONS
                    and default_device_id is not None
                ):
                    self._listener._loop.call_soon_threadsafe(
                        self._listener._dispatch_default_capture_changed,
                        default_device_id,
                    )
                return 0  # S_OK

            def OnDeviceStateChanged(  # noqa: N802 — COM signature
                self,
                this: object,
                device_id: str | None,
                new_state: int,
            ) -> int:
                if device_id is not None:
                    self._listener._loop.call_soon_threadsafe(
                        self._listener._dispatch_device_state_changed,
                        device_id,
                        new_state,
                    )
                return 0  # S_OK

            def OnDeviceAdded(  # noqa: N802 — COM signature
                self,
                this: object,
                device_id: str | None,  # noqa: ARG002 — accepted by contract; not dispatched
            ) -> int:
                # OnDeviceAdded fires on every USB hot-plug; we
                # don't dispatch it (the OS surfaces the new
                # endpoint via OnDefaultDeviceChanged when it
                # becomes the default — the more useful event).
                return 0  # S_OK

            def OnDeviceRemoved(  # noqa: N802 — COM signature
                self,
                this: object,
                device_id: str | None,  # noqa: ARG002 — accepted by contract; not dispatched
            ) -> int:
                # Same rationale as OnDeviceAdded.
                return 0  # S_OK

            def OnPropertyValueChanged(  # noqa: N802 — COM signature
                self,
                this: object,
                device_id: str | None,
                key: object,
            ) -> int:
                if self._listener._on_property_value_changed is not None and device_id is not None:
                    # Convert PROPERTYKEY to a stable string repr —
                    # the consumer doesn't need the raw struct, just
                    # a key the dashboard can colour.
                    try:
                        key_str = f"{getattr(key, 'fmtid', '')!s}.{getattr(key, 'pid', 0)}"
                    except BaseException:  # noqa: BLE001 — must NEVER raise out of COM thread
                        key_str = "unknown"
                    self._listener._loop.call_soon_threadsafe(
                        self._listener._dispatch_property_value_changed,
                        device_id,
                        key_str,
                    )
                return 0  # S_OK

        # Step 3 — activate the MMDeviceEnumerator + register.
        try:
            import comtypes.client

            enumerator = comtypes.client.CreateObject(
                _CLSID_MM_DEVICE_ENUMERATOR,
                interface=imm_device_enumerator_cls,
            )
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.error(
                "voice.mm_notification_client.create_object_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            record_hotplug_listener_registered(
                registered=False,
                error="create_object_failed",
            )
            self._registered = True
            return

        com_client = _ComNotificationClient(self)
        try:
            enumerator.RegisterEndpointNotificationCallback(com_client)
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.error(
                "voice.mm_notification_client.register_callback_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            record_hotplug_listener_registered(
                registered=False,
                error="register_callback_failed",
            )
            self._registered = True
            return

        self._enumerator = enumerator
        self._com_client = com_client
        self._registered = True
        logger.info(
            "voice.mm_notification_client.registered",
            mm_device_enumerator=_CLSID_MM_DEVICE_ENUMERATOR,
        )
        record_hotplug_listener_registered(registered=True)

    def unregister(self) -> None:
        """Tear the COM subscription down. Idempotent + defensive.

        Calls ``UnregisterEndpointNotificationCallback`` on the live
        enumerator, then releases both COM resources. Any failure is
        logged and absorbed — shutdown must not block on a wedged
        COM thread (the audio service deadlock contract is
        symmetric: blocking inside ``Unregister`` from the asyncio
        loop is also catastrophic on a wedged notifier).
        """
        if not self._registered:
            return
        self._registered = False

        enumerator = self._enumerator
        com_client = self._com_client
        self._enumerator = None
        self._com_client = None

        if enumerator is not None and com_client is not None:
            try:
                enumerator.UnregisterEndpointNotificationCallback(com_client)
            except BaseException as exc:  # noqa: BLE001 — shutdown path; never propagate
                logger.warning(
                    "voice.mm_notification_client.unregister_callback_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # ── Dispatcher trampolines ──────────────────────────────────────
    # These run on the asyncio loop (called via call_soon_threadsafe
    # from the COM notifier thread). They wrap the user-supplied
    # async callbacks in ``ensure_future`` so the COM thread stays
    # decoupled from the loop's task scheduling.

    def _dispatch_default_capture_changed(self, device_id: str) -> None:
        try:
            coro = self._on_default_capture_changed(device_id)
            asyncio.ensure_future(coro, loop=self._loop)
        except BaseException as exc:  # noqa: BLE001 — observability isolation
            logger.error(
                "voice.mm_notification_client.dispatch_default_capture_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _dispatch_device_state_changed(self, device_id: str, new_state: int) -> None:
        try:
            coro = self._on_device_state_changed(device_id, new_state)
            asyncio.ensure_future(coro, loop=self._loop)
        except BaseException as exc:  # noqa: BLE001 — observability isolation
            logger.error(
                "voice.mm_notification_client.dispatch_device_state_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _dispatch_property_value_changed(self, device_id: str, key: str) -> None:
        if self._on_property_value_changed is None:
            return
        try:
            coro = self._on_property_value_changed(device_id, key)
            asyncio.ensure_future(coro, loop=self._loop)
        except BaseException as exc:  # noqa: BLE001 — observability isolation
            logger.error(
                "voice.mm_notification_client.dispatch_property_value_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )


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
