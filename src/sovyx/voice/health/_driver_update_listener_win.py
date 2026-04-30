"""Detect Windows audio driver updates mid-session via WMI subscription.

Phase 5 / T5.49 — foundation. Provides a structured event stream when
Windows updates an audio device's driver while the daemon is running.
The wire-up that consumes these events to trigger a graceful re-cascade
ships separately as T5.50 per ``feedback_staged_adoption``.

Why driver-update detection matters:

* Windows Update + manufacturer auto-updaters can swap an audio
  driver mid-session. The PortAudio + IMMDevice surfaces hold
  references to the OLD driver's endpoint; the next capture
  attempt may fail silently (deaf signal) or surface a generic
  ``E_INVALIDARG`` from WASAPI without indicating that the
  underlying cause was a driver swap.
* Sovyx's ComboStore caches a winning combo per endpoint. After
  a driver swap, the cached combo's behavioural assumptions
  (sample rate, latency, exclusive support) may be stale — the
  cascade should re-run on the new driver, not blindly replay
  the old combo.
* Operators tracking deaf-signal incidents need driver-update
  events as forensic evidence: a sudden spike in
  ``voice.health.deaf.warnings_total`` correlated with a
  ``voice.driver_update.detected`` event points at a regressed
  driver release rather than at Sovyx's bypass logic.

Implementation:

* WMI ``__InstanceModificationEvent WITHIN 2`` subscription
  filtered to ``Win32_PnPEntity`` instances belonging to the
  audio class (``ClassGuid = {4d36e96c-e325-11ce-bfc1-08002be10318}``,
  the canonical "Sound, video and game controllers" GUID since
  Windows XP).
* Subscription runs on a dedicated worker thread that calls
  ``CoInitializeEx(COINIT_MULTITHREADED)`` so the WMI sink
  callback can fire without blocking the asyncio loop.
* The sink (``IWbemObjectSink::Indicate``) extracts the modified
  PnP entity's ``PNPDeviceID`` + ``Name`` + ``DriverVersion`` and
  marshals a :class:`DriverUpdateEvent` onto the asyncio loop via
  :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.
* The sink body is non-blocking (anti-pattern #29 contract,
  same as :mod:`sovyx.voice._mm_notification_client`): it only
  reads VARIANT props via ``IWbemClassObject::Get``, builds the
  dataclass, posts the marshal call, and returns ``S_OK``.

Cross-OS contract:

* On non-Windows platforms the factory returns
  :class:`NoopDriverUpdateListener` which logs once and silently
  honours the lifecycle interface. Linux uses udev hot-plug
  events for analogous functionality (``_hotplug_linux.py``);
  macOS uses CoreAudio property listeners (deferred to
  Phase 5 macOS block).
* When ``comtypes`` isn't installed (slim CI runners, stripped-
  down Windows SKU), the Windows path degrades to no-op with a
  structured WARN.

Threading + lifecycle:

* :meth:`WindowsDriverUpdateListener.register` spawns the worker
  thread + returns immediately. The thread initialises COM,
  builds the WMI subscription, and blocks on a stop event until
  :meth:`unregister` is called.
* :meth:`unregister` signals the stop event and joins the
  worker thread with a 2 s timeout. If the WMI service is wedged
  (rare; usually only on a corrupted Windows install) the
  daemon shutdown does NOT hang — the thread is daemon=True and
  leaks safely on process exit.
* The sink's ``Indicate`` callback fires on a WMI-worker thread
  that is DIFFERENT from our dedicated thread. The sink's body
  is structured so the wrong-thread case still works: only
  ``call_soon_threadsafe`` interactions with the asyncio loop +
  ``IWbemClassObject::Get`` reads on the passed-in interface
  pointer.

This module is the FOUNDATION (T5.49). The consumer wire-up
(T5.50) lands in a separate commit so a single change doesn't
bundle "foundation + N call-site adoptions" per
``feedback_staged_adoption``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import sys
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


# ── Public types ────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class DriverUpdateEvent:
    """Structured payload emitted when an audio device's driver changes.

    Attributes:
        device_id: The PnP device-instance ID of the affected device
            (``USB\\VID_xxxx&PID_xxxx\\<serial>`` for USB audio,
            ``HDAUDIO\\FUNC_xx&VEN_xxxx&DEV_xxxx`` for onboard codecs,
            etc.). Stable across the device's lifetime; survives
            replug + driver update.
        friendly_name: Human-readable device name from
            ``Win32_PnPEntity.Name`` — what Windows shows in Sound
            Settings.
        new_driver_version: ``Win32_PnPEntity.DriverVersion`` AFTER
            the modification. Empty string when the property isn't
            available (some entities omit it). The OLD version is
            NOT carried in this dataclass — WMI's
            ``__InstanceModificationEvent.PreviousInstance`` is
            available to consumers that need diff context, but the
            common downstream pattern is "trigger re-cascade on any
            driver change" which doesn't need the old version.
        detected_at: UTC timestamp when the sink received the WMI
            event. Used for forensic correlation against deaf-signal
            incidents.
    """

    device_id: str
    friendly_name: str
    new_driver_version: str
    detected_at: _dt.datetime


@runtime_checkable
class DriverUpdateListener(Protocol):
    """Lifecycle contract for the driver-update subscription.

    * :meth:`register` installs the WMI subscription (Windows) or
      logs a single INFO line and returns (non-Windows / disabled).
      Idempotent — a second ``register`` is a no-op.
    * :meth:`unregister` tears the subscription down + joins the
      worker thread. Idempotent.
    """

    def register(self) -> None:
        """Install the OS subscription. One INFO line on first call
        so operators can see the daemon's listener state at a glance.
        """
        ...

    def unregister(self) -> None:
        """Tear the OS subscription down. MUST be idempotent."""
        ...


# ── Cross-OS / disabled fallback ────────────────────────────────────


class NoopDriverUpdateListener:
    """Cross-OS / disabled-flag fallback.

    Used in three cases:

    1. ``sys.platform != "win32"`` — Linux uses udev hot-plug
       events for analogous functionality; macOS uses CoreAudio
       property listeners (deferred to Phase 5 macOS block).
    2. ``enabled=False`` (foundation-phase default through v0.28.0
       opt-in adoption window).
    3. ``comtypes`` not installed (slim-CI runners + stripped-down
       Windows SKUs).

    Logs a single INFO line on first :meth:`register` so an
    operator tailing ``sovyx.log`` can tell at a glance whether
    the daemon is running with degraded driver-update awareness.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    def register(self) -> None:
        if self._started:
            return
        logger.info(
            "voice.driver_update_listener.noop_register",
            reason=self._reason,
        )
        self._started = True

    def unregister(self) -> None:
        # Idempotent — never errors regardless of whether register
        # was called. Voice pipeline shutdown calls unregister
        # unconditionally inside try/finally.
        self._started = False


# ── Windows COM constants (mmdeviceapi.h + wbemcli.h) ──────────────


# CLSID for ``WbemLocator`` — entry point for WMI access.
# Documented in ``wbemcli.h``. Stable since Windows 2000.
_CLSID_WBEM_LOCATOR = "{4590F811-1D3A-11D0-891F-00AA004B2E24}"

# IID for ``IWbemLocator``.
_IID_IWBEM_LOCATOR = "{DC12A687-737F-11CF-884D-00AA004B2E24}"

# IID for ``IWbemServices``.
_IID_IWBEM_SERVICES = "{9556DC99-828C-11CF-A37E-00AA003240C7}"

# IID for ``IWbemObjectSink`` — the sink interface our COMObject
# subclass implements.
_IID_IWBEM_OBJECT_SINK = "{7C857801-7381-11CF-884D-00AA004B2E24}"

# IID for ``IWbemClassObject`` — the type WMI passes to the sink
# on each event.
_IID_IWBEM_CLASS_OBJECT = "{DC12A681-737F-11CF-884D-00AA004B2E24}"

# WMI namespace for hardware introspection.
_WMI_NAMESPACE_CIMV2 = r"ROOT\CIMV2"

# Canonical ClassGuid for "Sound, video and game controllers" —
# the device class that contains audio drivers. Documented at
# https://learn.microsoft.com/en-us/windows-hardware/drivers/install/system-defined-device-setup-classes-available-to-vendors
# Stable since Windows XP.
_CLASS_GUID_AUDIO = "{4d36e96c-e325-11ce-bfc1-08002be10318}"

# WMI query for audio-class driver modifications. WITHIN 2 means
# "poll the underlying CIM repository every 2 seconds and emit on
# any modification". Polling interval is a trade-off: lower = more
# CPU + more responsive; higher = less CPU + worse latency.
# 2 seconds matches Microsoft's recommended default for hardware
# event subscriptions.
_WMI_QUERY = (
    "SELECT * FROM __InstanceModificationEvent WITHIN 2 "
    "WHERE TargetInstance ISA 'Win32_PnPEntity' "
    f"AND TargetInstance.ClassGuid = '{_CLASS_GUID_AUDIO}'"
)

# ``CoInitializeEx`` flags — multi-threaded apartment so the WMI
# sink can fire on its own worker thread without us serialising
# through a single COM thread. Single-threaded apartment would
# require a message pump, which we don't run on this thread.
_COINIT_MULTITHREADED = 0x0

# ``IWbemLocator::ConnectServer`` flags — 0 means use the current
# process's user identity for the connection, which is what we
# want (the daemon runs under the operator's user).
_WBEM_CONNECT_SERVER_FLAGS = 0

# ``IWbemServices::ExecNotificationQueryAsync`` flags. Combination
# of ``WBEM_FLAG_SEND_STATUS`` (0x80) lets us see registration
# success via SetStatus, and ``WBEM_FLAG_BIDIRECTIONAL`` (0x0)
# is the default.
_WBEM_FLAG_SEND_STATUS = 0x80


# ── comtypes interface bindings (lazy-loaded per call) ──


def _build_wmi_bindings() -> tuple[Any, Any, Any, Any] | None:
    """Lazy-resolve the comtypes interface definitions for WMI.

    Returns a ``(IWbemLocator, IWbemServices, IWbemObjectSink,
    IWbemClassObject)`` 4-tuple when comtypes is importable,
    ``None`` otherwise. Defining the comtypes classes inside this
    function (rather than module top-level) keeps this module
    importable on non-Windows / slim-CI hosts where comtypes
    isn't installed.

    The vtable order MUST match Microsoft's ``wbemcli.h`` exactly.
    comtypes resolves vtable slots positionally — reordering
    methods would silently invoke the wrong native function.
    """
    try:
        from ctypes import POINTER, c_int, c_long, c_void_p, c_wchar_p

        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    except ImportError:
        return None

    class _IWbemClassObject(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed
        """Subset of ``IWbemClassObject`` we read.

        The full vtable is large (~30 methods); we only need
        ``Get`` to extract property values from the modified PnP
        entity. ``GetNames`` / ``Put`` / etc. are not used.

        Vtable order MUST match ``wbemcli.h``:

        1. GetQualifierSet
        2. Get
        3. Put
        4. Delete
        5. GetNames
        6. BeginEnumeration
        7. Next
        ...
        """

        _iid_ = GUID(_IID_IWBEM_CLASS_OBJECT)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "GetQualifierSet",
                (["out"], POINTER(POINTER(IUnknown)), "qualifier_set"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "Get",
                (["in"], c_wchar_p, "name"),
                (["in"], c_long, "flags"),
                # The VARIANT out-param is opaque — comtypes
                # marshals it as a Python value when possible.
                # We type it as c_void_p here and read it back as
                # a string via the helper.
                (["in", "out"], c_void_p, "value"),
                (["in", "out"], POINTER(c_int), "type"),
                (["in", "out"], POINTER(c_long), "flavor"),
            ),
        ]

    class _IWbemObjectSink(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed
        """Sink interface — our COMObject subclass implements this.

        WMI calls back via ``Indicate`` for each event matching the
        subscription query, and via ``SetStatus`` for registration
        progress / errors.

        Vtable order MUST match ``wbemcli.h``:

        1. Indicate
        2. SetStatus
        """

        _iid_ = GUID(_IID_IWBEM_OBJECT_SINK)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "Indicate",
                (["in"], c_long, "object_count"),
                (["in"], POINTER(POINTER(_IWbemClassObject)), "objects"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "SetStatus",
                (["in"], c_long, "flags"),
                (["in"], HRESULT, "result"),
                (["in"], c_wchar_p, "param"),
                (["in"], POINTER(_IWbemClassObject), "object_param"),
            ),
        ]

    class _IWbemServices(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed
        """Subset of ``IWbemServices`` we call.

        We only need ``ExecNotificationQueryAsync`` (start the
        subscription) and ``CancelAsyncCall`` (stop it). The full
        interface has 21 methods.

        Vtable order MUST match ``wbemcli.h``:

        1. OpenNamespace
        2. CancelAsyncCall
        3. QueryObjectSink
        4. GetObject
        5. GetObjectAsync
        6. PutClass
        7. PutClassAsync
        8. DeleteClass
        9. DeleteClassAsync
        10. CreateClassEnum
        11. CreateClassEnumAsync
        12. PutInstance
        13. PutInstanceAsync
        14. DeleteInstance
        15. DeleteInstanceAsync
        16. CreateInstanceEnum
        17. CreateInstanceEnumAsync
        18. ExecQuery
        19. ExecQueryAsync
        20. ExecNotificationQuery
        21. ExecNotificationQueryAsync
        22. ExecMethod
        23. ExecMethodAsync

        We define stubs for the methods preceding the ones we
        call, since comtypes vtable positioning requires the full
        prefix to be defined.
        """

        _iid_ = GUID(_IID_IWBEM_SERVICES)
        _methods_ = [
            # 1. OpenNamespace — stub.
            COMMETHOD(
                [],
                HRESULT,
                "OpenNamespace",
                (["in"], c_wchar_p, "namespace"),
                (["in"], c_long, "flags"),
                (["in"], POINTER(IUnknown), "context"),
                (["out"], POINTER(POINTER(IUnknown)), "working_namespace"),
                (["out"], POINTER(POINTER(IUnknown)), "result"),
            ),
            # 2. CancelAsyncCall — used to stop subscription.
            COMMETHOD(
                [],
                HRESULT,
                "CancelAsyncCall",
                (["in"], POINTER(_IWbemObjectSink), "sink"),
            ),
            # 3-20. Stubs (we don't call these but the vtable
            # positions matter).
            COMMETHOD([], HRESULT, "QueryObjectSink"),
            COMMETHOD([], HRESULT, "GetObject"),
            COMMETHOD([], HRESULT, "GetObjectAsync"),
            COMMETHOD([], HRESULT, "PutClass"),
            COMMETHOD([], HRESULT, "PutClassAsync"),
            COMMETHOD([], HRESULT, "DeleteClass"),
            COMMETHOD([], HRESULT, "DeleteClassAsync"),
            COMMETHOD([], HRESULT, "CreateClassEnum"),
            COMMETHOD([], HRESULT, "CreateClassEnumAsync"),
            COMMETHOD([], HRESULT, "PutInstance"),
            COMMETHOD([], HRESULT, "PutInstanceAsync"),
            COMMETHOD([], HRESULT, "DeleteInstance"),
            COMMETHOD([], HRESULT, "DeleteInstanceAsync"),
            COMMETHOD([], HRESULT, "CreateInstanceEnum"),
            COMMETHOD([], HRESULT, "CreateInstanceEnumAsync"),
            COMMETHOD([], HRESULT, "ExecQuery"),
            COMMETHOD([], HRESULT, "ExecQueryAsync"),
            COMMETHOD([], HRESULT, "ExecNotificationQuery"),
            # 21. ExecNotificationQueryAsync — start subscription.
            COMMETHOD(
                [],
                HRESULT,
                "ExecNotificationQueryAsync",
                (["in"], c_wchar_p, "query_language"),
                (["in"], c_wchar_p, "query"),
                (["in"], c_long, "flags"),
                (["in"], POINTER(IUnknown), "context"),
                (["in"], POINTER(_IWbemObjectSink), "sink"),
            ),
        ]

    class _IWbemLocator(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed
        """``IWbemLocator`` — entry point.

        Vtable order MUST match ``wbemcli.h``:

        1. ConnectServer
        """

        _iid_ = GUID(_IID_IWBEM_LOCATOR)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "ConnectServer",
                (["in"], c_wchar_p, "network_resource"),
                (["in"], c_wchar_p, "user"),
                (["in"], c_wchar_p, "password"),
                (["in"], c_wchar_p, "locale"),
                (["in"], c_long, "security_flags"),
                (["in"], c_wchar_p, "authority"),
                (["in"], POINTER(IUnknown), "context"),
                (["out"], POINTER(POINTER(_IWbemServices)), "services"),
            ),
        ]

    return (_IWbemLocator, _IWbemServices, _IWbemObjectSink, _IWbemClassObject)


# ── Windows listener implementation ────────────────────────────────


class WindowsDriverUpdateListener:
    """Windows-only WMI subscription for audio driver updates.

    Spawns a dedicated daemon thread that initialises COM, builds
    the WMI subscription, and blocks until :meth:`unregister` sets
    the stop event. The WMI sink (``IWbemObjectSink::Indicate``)
    fires on a WMI-internal worker thread; the sink body is
    structured to be non-blocking and marshals events onto the
    asyncio loop via ``call_soon_threadsafe``.

    **Defensive design** — same contract as
    :class:`~sovyx.voice._mm_notification_client.WindowsMMNotificationListener`:
    every COM boundary is wrapped in ``try/except BaseException``.
    On any failure (missing comtypes, COM marshalling error, WMI
    service unavailable) the listener falls through to no-op
    semantics. The daemon never crashes.

    Args:
        loop: The asyncio loop the WMI sink will marshal events
            onto via ``call_soon_threadsafe``. Captured at
            construction time so the COM thread never has to
            look it up.
        on_driver_changed: Async callback invoked on the asyncio
            loop after a filtered ``Win32_PnPEntity`` modification
            event for an audio-class device.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_driver_changed: Callable[[DriverUpdateEvent], Awaitable[None]],
    ) -> None:
        self._loop = loop
        self._on_driver_changed = on_driver_changed
        self._registered = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Live COM resources held by the worker thread. Read-only
        # from the asyncio thread; the worker thread releases them
        # on exit. The asyncio thread sees ``None`` when the worker
        # hasn't started.
        self._sink: Any = None
        self._services: Any = None

    def register(self) -> None:
        """Spawn the worker thread + start the WMI subscription.

        Idempotent: the second call is a no-op. On any startup
        failure (missing comtypes, WMI service down, etc.) the
        worker thread emits a structured WARN + exits cleanly;
        ``self._registered`` flips True so subsequent ``register``
        calls don't re-spawn the failing thread.
        """
        if self._registered:
            return
        self._registered = True

        bindings = _build_wmi_bindings()
        if bindings is None:
            logger.warning(
                "voice.driver_update_listener.comtypes_unavailable",
                reason=(
                    "comtypes import failed; cannot register the WMI "
                    "driver-update subscription. Install via `pip "
                    "install comtypes` or `pip install sovyx[voice]` "
                    "on Windows. The daemon continues with degraded "
                    "driver-update awareness — fallback is the next "
                    "deaf-signal heartbeat eventually surfacing the "
                    "stale combo."
                ),
            )
            return

        self._thread = threading.Thread(
            target=self._run_worker,
            args=(bindings,),
            name="sovyx-wmi-driver-watch",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "voice.driver_update_listener.registered",
            wmi_namespace=_WMI_NAMESPACE_CIMV2,
            class_guid=_CLASS_GUID_AUDIO,
        )

    def unregister(self) -> None:
        """Signal the worker to stop + join with bounded timeout.

        Idempotent. If the WMI service is wedged (rare; usually
        only on a corrupted Windows install) the join times out
        after 2 s and the thread leaks safely on process exit
        (``daemon=True``). The caller never blocks indefinitely.
        """
        if not self._registered:
            return
        self._registered = False

        self._stop_event.set()

        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning(
                    "voice.driver_update_listener.thread_join_timeout",
                    reason="WMI worker thread did not stop within 2s",
                )

    def _run_worker(self, bindings: tuple[Any, Any, Any, Any]) -> None:
        """Worker thread entry point.

        Sequence:

        1. ``CoInitializeEx(MULTITHREADED)`` so the WMI sink can
           fire on its own worker thread without us serialising.
        2. ``CoCreateInstance(WbemLocator)`` →
           ``ConnectServer(ROOT\\CIMV2)`` →
           ``ExecNotificationQueryAsync(query, sink)``.
        3. Block on ``self._stop_event`` until ``unregister``.
        4. ``CancelAsyncCall(sink)`` → release services →
           ``CoUninitialize``.

        Every COM boundary is wrapped — a failure at any step
        emits a structured WARN + exits cleanly. The daemon
        never sees an unhandled exception from this thread.
        """
        locator_cls, _services_cls, sink_cls, class_object_cls = bindings

        # Step 1 — CoInitializeEx.
        try:
            import comtypes

            comtypes.CoInitializeEx(_COINIT_MULTITHREADED)
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.warning(
                "voice.driver_update_listener.coinitialize_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        try:
            # Step 2 — locator → services → subscription.
            locator = self._create_locator(locator_cls)
            if locator is None:
                return

            services = self._connect_server(locator)
            if services is None:
                return
            self._services = services

            sink = self._build_sink(sink_cls, class_object_cls)
            self._sink = sink

            if not self._exec_subscription(services, sink):
                return

            # Step 3 — block until unregister.
            self._stop_event.wait()

            # Step 4 — cancel + cleanup.
            self._cancel_subscription(services, sink)
        finally:
            try:
                comtypes.CoUninitialize()
            except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
                logger.debug(
                    "voice.driver_update_listener.couninitialize_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    def _create_locator(
        self,
        locator_cls: Any,  # noqa: ANN401 — comtypes-generated COM interface classes are dynamically typed; over-specifying with a Protocol would reject the legitimate runtime types and break tests
    ) -> Any | None:  # noqa: ANN401 — IWbemLocator IUnknown POINTER from comtypes
        """Create the IWbemLocator entry point. Returns None on failure."""
        try:
            import comtypes.client

            return comtypes.client.CreateObject(
                _CLSID_WBEM_LOCATOR,
                interface=locator_cls,
            )
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.warning(
                "voice.driver_update_listener.create_locator_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def _connect_server(self, locator: Any) -> Any | None:  # noqa: ANN401 — see _create_locator rationale
        """Connect to ROOT\\CIMV2 namespace. Returns None on failure."""
        try:
            return locator.ConnectServer(
                _WMI_NAMESPACE_CIMV2,
                None,  # user — current process identity
                None,  # password
                None,  # locale
                _WBEM_CONNECT_SERVER_FLAGS,
                None,  # authority
                None,  # context
            )
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.warning(
                "voice.driver_update_listener.connect_server_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def _build_sink(self, sink_cls: Any, class_object_cls: Any) -> Any:  # noqa: ANN401 — see _create_locator rationale
        """Build the COMObject sink subclass."""
        try:
            from comtypes import (
                COMObject,
            )
        except ImportError:
            # Defensive — _build_wmi_bindings already succeeded.
            return None

        listener = self

        class _ComSink(COMObject):  # type: ignore[misc]  # comtypes COMObject is Any-typed
            """Concrete IWbemObjectSink implementation.

            Each ``Indicate`` callback fires on a WMI-internal
            worker thread. The body MUST be non-blocking
            (anti-pattern #29 contract) — only short property
            reads + one ``call_soon_threadsafe`` post.
            """

            _com_interfaces_ = [sink_cls]

            def Indicate(  # noqa: N802 — COM signature
                self,
                this: object,  # noqa: ARG002 — unused IUnknown self
                object_count: int,
                objects: Any,  # noqa: ANN401 — array-of-IUnknown POINTERs from comtypes
            ) -> int:
                """Process one or more WMI events.

                ``objects`` is an array of ``IWbemClassObject``
                pointers. For ``__InstanceModificationEvent``,
                each object's ``TargetInstance`` property is the
                modified ``Win32_PnPEntity`` instance.
                """
                for i in range(object_count):
                    try:
                        event_object = objects[i]
                        target_instance = _read_target_instance(event_object, class_object_cls)
                        if target_instance is None:
                            continue
                        device_id = _read_string_property(target_instance, "PNPDeviceID")
                        if not device_id:
                            continue
                        friendly_name = _read_string_property(target_instance, "Name") or ""
                        driver_version = (
                            _read_string_property(target_instance, "DriverVersion") or ""
                        )
                        event = DriverUpdateEvent(
                            device_id=device_id,
                            friendly_name=friendly_name,
                            new_driver_version=driver_version,
                            detected_at=_dt.datetime.now(_dt.UTC),
                        )
                        listener._loop.call_soon_threadsafe(
                            listener._dispatch_event,
                            event,
                        )
                    except BaseException as exc:  # noqa: BLE001 — sink must NEVER raise to COM
                        logger.error(
                            "voice.driver_update_listener.indicate_failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                return 0  # S_OK

            def SetStatus(  # noqa: N802 — COM signature
                self,
                this: object,  # noqa: ARG002 — unused IUnknown self
                flags: int,  # noqa: ARG002 — informational
                result: int,
                param: str | None,  # noqa: ARG002 — informational
                object_param: Any,  # noqa: ARG002, ANN401 — informational IUnknown POINTER
            ) -> int:
                """Receive registration / cancellation status from WMI.

                ``result`` is an HRESULT — non-zero means the
                subscription failed. Surface failures as structured
                WARN; success silently.
                """
                if result != 0:
                    logger.warning(
                        "voice.driver_update_listener.set_status_error",
                        hresult=hex(result & 0xFFFFFFFF),
                    )
                return 0  # S_OK

        return _ComSink()

    def _exec_subscription(self, services: Any, sink: Any) -> bool:  # noqa: ANN401 — see _create_locator rationale
        """Start the WMI notification subscription. Returns False on failure."""
        try:
            services.ExecNotificationQueryAsync(
                "WQL",
                _WMI_QUERY,
                _WBEM_FLAG_SEND_STATUS,
                None,  # context
                sink,
            )
        except BaseException as exc:  # noqa: BLE001 — defensive at COM boundary
            logger.warning(
                "voice.driver_update_listener.exec_subscription_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        return True

    def _cancel_subscription(self, services: Any, sink: Any) -> None:  # noqa: ANN401 — see _create_locator rationale
        """Cancel the async WMI subscription. Best-effort."""
        try:
            services.CancelAsyncCall(sink)
        except BaseException as exc:  # noqa: BLE001 — shutdown path
            logger.debug(
                "voice.driver_update_listener.cancel_subscription_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _dispatch_event(self, event: DriverUpdateEvent) -> None:
        """Invoke the user callback on the asyncio loop.

        Wraps the user's coroutine in ``ensure_future`` so the
        WMI thread stays decoupled from the loop's scheduling.
        Errors in the user callback are isolated and logged
        without re-raising into the WMI thread.
        """
        try:
            coro = self._on_driver_changed(event)
            asyncio.ensure_future(coro, loop=self._loop)
        except BaseException as exc:  # noqa: BLE001 — observability isolation
            logger.error(
                "voice.driver_update_listener.dispatch_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                device_id=event.device_id,
            )


# ── VARIANT property-reading helpers ────────────────────────────────


def _read_target_instance(event_object: Any, class_object_cls: Any) -> Any | None:  # noqa: ANN401 — comtypes IWbemClassObject is dynamically typed; see WindowsDriverUpdateListener._create_locator for rationale
    """Extract ``TargetInstance`` from a ``__InstanceModificationEvent``.

    The event object's ``TargetInstance`` property is itself an
    ``IWbemClassObject`` representing the modified Win32_PnPEntity
    instance. Reading it returns the embedded class object so we
    can read its scalar properties (``PNPDeviceID``, ``Name``, etc.).

    Best-effort: returns None on any read failure rather than
    raising back into the WMI thread. The post-VARIANT-unwrap
    logic lives in :func:`_unwrap_variant_to_class_object` so the
    COM-bound ``Get`` call and the QueryInterface logic can be
    tested independently — testability via small pure helpers per
    anti-pattern #20.
    """
    try:
        from ctypes import byref, c_int, c_long

        from comtypes.automation import (
            VARIANT,
        )

        value = VARIANT()
        ctype = c_int()
        flavor = c_long()
        event_object.Get(
            "TargetInstance",
            0,
            byref(value),
            byref(ctype),
            byref(flavor),
        )
    except BaseException:  # noqa: BLE001 — best-effort read; COM call may raise
        return None

    return _unwrap_variant_to_class_object(value.value, class_object_cls)


def _unwrap_variant_to_class_object(
    unwrapped: Any,  # noqa: ANN401 — VARIANT-unwrapped value is intentionally untyped
    class_object_cls: Any,  # noqa: ANN401 — comtypes-generated COM interface class
) -> Any | None:  # noqa: ANN401 — IWbemClassObject POINTER from comtypes
    """Pure helper — convert a VARIANT-unwrapped Python value into
    an ``IWbemClassObject`` via ``QueryInterface``.

    Extracted from :func:`_read_target_instance` so the post-
    VARIANT-unwrap logic is testable without ctypes / comtypes
    interop. Tests pass a plain mock with a ``QueryInterface``
    attribute and pin the contract without materialising a real
    ``VARIANT`` struct.

    Returns:
        The QueryInterface result on success, ``None`` when:

        * ``unwrapped`` is ``None`` (VT_NULL / missing property).
        * ``QueryInterface`` raises (alien object doesn't
          implement the requested interface).
    """
    if unwrapped is None:
        return None
    try:
        return unwrapped.QueryInterface(class_object_cls)
    except BaseException:  # noqa: BLE001 — best-effort QI; alien COM objects may raise
        return None


def _read_string_property(class_object: Any, property_name: str) -> str | None:  # noqa: ANN401 — comtypes IWbemClassObject is dynamically typed
    """Read a wide-string property from an IWbemClassObject.

    Returns None when the property is absent, NULL, or non-string
    typed. Best-effort: never raises. The VARIANT unwrap →
    string-or-None coercion lives in
    :func:`_unwrap_variant_to_string` so the type-coercion rules
    are testable without ctypes interop.
    """
    try:
        from ctypes import byref, c_int, c_long

        from comtypes.automation import (
            VARIANT,
        )

        value = VARIANT()
        ctype = c_int()
        flavor = c_long()
        class_object.Get(
            property_name,
            0,
            byref(value),
            byref(ctype),
            byref(flavor),
        )
    except BaseException:  # noqa: BLE001 — best-effort read; COM call may raise
        return None

    return _unwrap_variant_to_string(value.value)


def _unwrap_variant_to_string(unwrapped: Any) -> str | None:  # noqa: ANN401 — VARIANT-unwrapped Python value is intentionally untyped
    """Pure helper — coerce a VARIANT-unwrapped Python value to a string.

    Extracted from :func:`_read_string_property` so the type-
    coercion rules are testable without ctypes / comtypes
    interop. Total + never raises.

    Coercion rules:

    * ``None`` (VT_NULL / absent property) → ``None``.
    * ``str`` (VT_BSTR / VT_LPWSTR after comtypes unwrap) → as-is.
    * ``bytes`` (rare; some BSTR variants surface as bytes on
      certain comtypes paths) → ``.decode("utf-8", errors="replace")``.
    * Anything else (numbers, dates, arrays) → ``str(value)`` so
      the caller still sees something queryable in logs.
    """
    if unwrapped is None:
        return None
    if isinstance(unwrapped, str):
        return unwrapped
    if isinstance(unwrapped, bytes):
        return unwrapped.decode("utf-8", errors="replace")
    return str(unwrapped)


# ── Factory ─────────────────────────────────────────────────────────


def build_driver_update_listener(
    loop: asyncio.AbstractEventLoop,
    on_driver_changed: Callable[[DriverUpdateEvent], Awaitable[None]],
    *,
    enabled: bool = False,
) -> DriverUpdateListener:
    """Return the right listener for the current platform + flag.

    Cross-OS shim contract:

    * ``enabled=False`` (foundation default through v0.28.0
      adoption window) → :class:`NoopDriverUpdateListener` with
      ``reason="flag_disabled"``.
    * ``sys.platform != "win32"`` →
      :class:`NoopDriverUpdateListener` with
      ``reason="non_windows_platform"``.
    * Windows + ``enabled=True`` →
      :class:`WindowsDriverUpdateListener`.

    The ``enabled`` flag should always come from the resolved
    ``EngineConfig.tuning.voice.audio_driver_update_listener_enabled``
    setting (added in T5.50 wire-up commit), NOT from a parallel
    master switch (anti-pattern #12).

    Args:
        loop: asyncio loop used to marshal WMI sink events.
            Required even on non-Windows so the call site doesn't
            need to branch.
        on_driver_changed: Async callback invoked on the asyncio
            loop after a filtered audio-driver modification.
        enabled: Master gate — when ``False`` returns the no-op
            listener regardless of platform.

    Returns:
        A listener honouring the :class:`DriverUpdateListener`
        protocol. The caller invokes ``register()`` /
        ``unregister()`` without branching on the concrete type.
    """
    if not enabled:
        return NoopDriverUpdateListener(reason="flag_disabled")
    if sys.platform != "win32":
        return NoopDriverUpdateListener(reason="non_windows_platform")
    return WindowsDriverUpdateListener(
        loop=loop,
        on_driver_changed=on_driver_changed,
    )


__all__ = [
    "DriverUpdateEvent",
    "DriverUpdateListener",
    "NoopDriverUpdateListener",
    "WindowsDriverUpdateListener",
    "build_driver_update_listener",
]
