"""Resolve a Windows IMMDevice endpoint ID to a stable USB fingerprint.

Phase 5 / T5.51 — Windows-side consumer of the cross-platform
:mod:`sovyx.voice.health._usb_fingerprint` foundation shipped in
T5.43. The combo store and device-enumeration plumbing carry the
endpoint ID returned by ``IMMDevice::GetId`` (a string of the form
``{0.0.1.00000000}.{guid}``); to derive a stable identity that
survives replug + firmware update, we resolve that endpoint to its
underlying PnP device-instance ID
(``USB\\VID_xxxx&PID_xxxx\\<serial>``) via the IPropertyStore +
``PKEY_Device_InstanceId`` chain, then delegate to
:func:`sovyx.voice.health._usb_fingerprint.fingerprint_usb_device`
for the actual ``"usb-VVVV:PPPP[-SERIAL]"`` formatting.

Public surface — :func:`resolve_endpoint_to_usb_fingerprint`.

Best-effort by design: every failure path returns ``None`` rather
than raising. The combo store's existing fallback (the PortAudio
surrogate hash) takes over whenever the USB fingerprint isn't
available — non-USB endpoints (PCI codecs, virtual devices),
stale endpoint IDs racing with hot-unplug, comtypes unavailable on
slim-CI hosts, and driver-side COM glitches all degrade gracefully.

The first comtypes ``ImportError`` per process emits a structured
WARN with the operator-actionable install hint, then a module-level
latch suppresses subsequent calls — mirrors the T5.44 pyudev
once-per-process pattern so a busy daemon (cascade restart loops)
doesn't flood ``sovyx.log`` with the same diagnostic.

Threading: the function is synchronous. ``IPropertyStore::GetValue``
calls block on driver IPC — async callers MUST wrap invocations in
:func:`asyncio.to_thread` per CLAUDE.md anti-pattern #14. The
intended call-site is the combo-store key-derivation path, which
is itself wrapped in ``asyncio.to_thread`` already.

Architecture: defines its own comtypes interface classes locally
(``IMMDevice``, ``IMMDeviceEnumerator``, ``IPropertyStore``,
``PROPVARIANT``, ``PROPERTYKEY``) rather than reusing
:mod:`sovyx.voice._mm_notification_client`'s ``_build_com_bindings``.
The IMM-listener subset of ``IMMDeviceEnumerator`` doesn't expose
``GetDevice`` on the layout this module needs, and duplicating the
GUID/IID/vtable definitions is the canonical-safe pattern for
COM (each module owns the contract it consumes; cross-module
sharing would couple two unrelated consumers via internal
implementation details).
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._usb_fingerprint import fingerprint_usb_device

logger = get_logger(__name__)


# ``VT_LPWSTR`` PROPVARIANT variant tag — the only variant
# ``PKEY_Device_InstanceId`` ever carries. Anything else means the
# property store returned an unexpected type, which fails safe to
# ``None``.
_VT_LPWSTR = 31


# ── Windows COM constants (mmdeviceapi.h + functiondiscoverykeys_devpkey.h) ──


# CLSID for ``CLSID_MMDeviceEnumerator``. Documented in
# ``mmdeviceapi.h``; identical to the one
# :mod:`sovyx.voice._mm_notification_client` defines — duplicated
# here so this module's binding contract stays self-contained.
_CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

# IID for ``IMMDeviceEnumerator``.
_IID_IMM_DEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"

# IID for ``IMMDevice``.
_IID_IMM_DEVICE = "{D666063F-1587-4E43-81F1-B948E807363F}"

# IID for ``IPropertyStore``.
_IID_PROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"

# ``PKEY_Device_InstanceId`` — the property whose value is the PnP
# device-instance ID string. Documented in ``functiondiscoverykeys_devpkey.h``
# (Windows SDK). The fmtid/pid pair is canonical and stable across
# every Windows version since Vista.
_PKEY_DEVICE_INSTANCE_ID_FMTID = "{78C34FC8-104A-4ACA-9EA4-524D52996E57}"
_PKEY_DEVICE_INSTANCE_ID_PID = 256

# ``STGM_READ`` flag for ``IMMDevice::OpenPropertyStore``. Open the
# property store read-only — we never write to it, and read-only
# avoids the elevation requirement some endpoints carry.
_STGM_READ = 0x00000000

# ``DEVICE_STATE_ACTIVE`` mask — only resolve fingerprints for
# active endpoints. Inactive / unplugged endpoints have stale
# property stores that may return cached IDs from prior firmware.
# (Currently unused by the resolver — the caller passes a known-
# active endpoint ID — but exposed here so a future enumeration
# path has the canonical mask.)
_DEVICE_STATE_ACTIVE = 0x00000001


# ── Once-per-process WARN latch (mirrors T5.44 pyudev pattern) ──


_comtypes_warning_emitted = False
"""Module-level latch — flipped True on the first comtypes ImportError
emission. Subsequent resolution attempts on the same process return
None silently. A daemon under cascade-restart load can call this
function dozens of times per minute; without the latch every call
would log the same "install comtypes" hint and drown actual
incident-relevant log lines."""


def _emit_comtypes_unavailable_warning_once() -> None:
    """Emit the comtypes-missing WARN exactly once per process."""
    global _comtypes_warning_emitted
    if _comtypes_warning_emitted:
        return
    _comtypes_warning_emitted = True
    logger.warning(
        "voice.endpoint_fingerprint.comtypes_unavailable",
        reason=(
            "comtypes import failed; cannot resolve IMMDevice endpoint "
            "IDs to PnP device-instance IDs for stable USB fingerprinting. "
            "The combo store falls back to the PortAudio surrogate hash, "
            "which is NOT stable across firmware update + replug. "
            "Install via `pip install comtypes` or `pip install sovyx[voice]`."
        ),
    )


# ── comtypes interface bindings (lazy-loaded per call) ──


def _build_property_store_bindings() -> tuple[Any, Any, Any, Any] | None:
    """Lazy-resolve the comtypes interface + struct definitions.

    Returns a ``(IMMDeviceEnumerator, IMMDevice, IPropertyStore,
    PROPVARIANT)`` 4-tuple when comtypes is importable, ``None``
    otherwise. Defining the comtypes classes inside this function
    (rather than module top-level) keeps this module importable on
    non-Windows / slim-CI hosts where comtypes isn't installed —
    the same lazy-import contract :mod:`sovyx.voice._mm_notification_client`
    uses for its own COM bindings.

    The vtable order MUST match ``mmdeviceapi.h`` +
    ``propsys.h`` exactly. comtypes resolves vtable slots
    positionally, so reordering methods would silently invoke the
    wrong native function.
    """
    try:
        import ctypes
        from ctypes import POINTER, c_uint, c_ushort, c_void_p, c_wchar_p

        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    except ImportError:
        return None

    class _PropertyKey(ctypes.Structure):
        """``PROPERTYKEY`` struct — passed by value to ``GetValue``.

        Layout from ``propkeydef.h``:

        ``typedef struct {{ GUID fmtid; DWORD pid; }} PROPERTYKEY;``
        """

        _fields_ = (("fmtid", GUID), ("pid", c_uint))

    class _PropVariant(ctypes.Structure):
        """``PROPVARIANT`` struct — out-param of
        ``IPropertyStore::GetValue``.

        Layout from ``propidlbase.h``. We only need the
        ``VT_LPWSTR`` (31) variant for ``PKEY_Device_InstanceId``,
        which carries a ``LPWSTR pwszVal`` in the union. The rest
        of the union is opaque ``c_void_p`` here — comtypes
        unmarshals the variant tag at ``vt`` and we read
        ``pwszVal`` only when ``vt == 31``.

        The full PROPVARIANT union is large (~24 bytes). We use
        ``c_void_p`` for the union slot because Python's ctypes
        Union class is awkward across the comtypes-managed
        marshalling boundary; reading the LPWSTR pointer directly
        from the well-defined offset is simpler + safer.
        """

        # vt + wReserved1/2/3 = 8 bytes; then the union starts.
        # The union's first 8 bytes (on x64) hold the LPWSTR
        # pointer for VT_LPWSTR. Aligning the union as a single
        # c_void_p reads exactly that pointer.
        _fields_ = (
            ("vt", c_ushort),
            ("wReserved1", c_ushort),
            ("wReserved2", c_ushort),
            ("wReserved3", c_ushort),
            ("pwszVal", c_void_p),
            # Trailing union bytes — present in the C struct but
            # never read by us. Pad to the canonical 24-byte size.
            ("_padding", c_void_p),
        )

    class _IPropertyStore(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed; mypy strict disallow_subclassing_any
        """Subset of ``propsys.h`` we call.

        Vtable order MUST match ``propsys.h``:

        1. GetCount
        2. GetAt
        3. GetValue
        4. SetValue
        5. Commit
        """

        _iid_ = GUID(_IID_PROPERTY_STORE)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "GetCount",
                (["out"], POINTER(c_uint), "count"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetAt",
                (["in"], c_uint, "index"),
                (["out"], POINTER(_PropertyKey), "key"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetValue",
                (["in"], POINTER(_PropertyKey), "key"),
                (["out"], POINTER(_PropVariant), "value"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "SetValue",
                (["in"], POINTER(_PropertyKey), "key"),
                (["in"], POINTER(_PropVariant), "value"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "Commit",
            ),
        ]

    class _IMMDevice(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed; mypy strict disallow_subclassing_any
        """Subset of ``mmdeviceapi.h`` we call.

        Vtable order MUST match ``mmdeviceapi.h``:

        1. Activate
        2. OpenPropertyStore
        3. GetId
        4. GetState
        """

        _iid_ = GUID(_IID_IMM_DEVICE)
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], c_uint, "cls_ctx"),
                (["in"], c_void_p, "activation_params"),
                (["out"], POINTER(POINTER(IUnknown)), "interface"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OpenPropertyStore",
                (["in"], c_uint, "stgm_access"),
                (["out"], POINTER(POINTER(_IPropertyStore)), "properties"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetId",
                (["out"], POINTER(c_wchar_p), "id"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetState",
                (["out"], POINTER(c_uint), "state"),
            ),
        ]

    class _IMMDeviceEnumerator(IUnknown):  # type: ignore[misc]  # comtypes IUnknown is Any-typed; mypy strict disallow_subclassing_any
        """Subset of ``mmdeviceapi.h`` we call.

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
                (["out"], POINTER(POINTER(_IMMDevice)), "endpoint"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetDevice",
                (["in"], c_wchar_p, "id"),
                (["out"], POINTER(POINTER(_IMMDevice)), "device"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "RegisterEndpointNotificationCallback",
                (["in"], POINTER(IUnknown), "client"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "UnregisterEndpointNotificationCallback",
                (["in"], POINTER(IUnknown), "client"),
            ),
        ]

    return (_IMMDeviceEnumerator, _IMMDevice, _IPropertyStore, _PropVariant)


# ── Public API ──


def resolve_endpoint_to_usb_fingerprint(endpoint_id: str) -> str | None:
    """Resolve a Windows IMMDevice endpoint ID to a stable USB fingerprint.

    Args:
        endpoint_id: The string returned by ``IMMDevice::GetId`` —
            a GUID-form ID like
            ``"{0.0.1.00000000}.{c7c1f3d8-1234-...}"`` carried by
            the combo store and the device-enumeration plumbing.

    Returns:
        ``"usb-VVVV:PPPP[-SERIAL]"`` (lowercase hex; see
        :mod:`sovyx.voice.health._usb_fingerprint`) when the
        endpoint resolves to a USB device, ``None`` otherwise.

    Failure modes returning ``None`` (each best-effort, no raise):

    * ``endpoint_id`` is empty or ``None``.
    * ``sys.platform != "win32"`` (no Windows COM available).
    * ``comtypes`` not installed (slim-CI / Linux container).
    * ``CoCreateInstance`` fails (audio service down / disabled).
    * ``GetDevice`` rejects the endpoint ID (stale / hot-unplug race).
    * ``OpenPropertyStore`` fails (driver buggy).
    * ``GetValue(PKEY_Device_InstanceId)`` returns no string
      (permission denial / property not implemented).
    * The PnP ID isn't a USB device (PCI codec, virtual loopback,
      Bluetooth A2DP — those use ``BTHENUM\\``,
      ``HDAUDIO\\``, ``SWD\\``, etc. prefixes that
      :func:`fingerprint_usb_device` correctly rejects).

    The first comtypes ImportError per process emits a single
    structured WARN with the install hint; subsequent calls are
    silent.
    """
    if not endpoint_id:
        return None
    if sys.platform != "win32":
        return None

    pnp_id = _resolve_endpoint_to_pnp_id(endpoint_id)
    if pnp_id is None:
        return None

    return fingerprint_usb_device(pnp_device_id=pnp_id)


def _resolve_endpoint_to_pnp_id(endpoint_id: str) -> str | None:
    """Resolve an IMMDevice endpoint ID to its PnP device-instance ID.

    Pure COM plumbing — opens IMMDeviceEnumerator, calls
    ``GetDevice(endpoint_id)``, opens the property store read-only,
    and reads ``PKEY_Device_InstanceId`` as a wide-string. Returns
    ``None`` on any failure (see
    :func:`resolve_endpoint_to_usb_fingerprint` failure modes).

    Separated from :func:`resolve_endpoint_to_usb_fingerprint` so
    tests can pin the COM-chain behaviour independent of the T5.43
    fingerprint formatting.
    """
    bindings = _build_property_store_bindings()
    if bindings is None:
        _emit_comtypes_unavailable_warning_once()
        return None

    enumerator_cls, _device_cls, _propstore_cls, _propvariant_cls = bindings

    try:
        from comtypes.client import CreateObject
    except ImportError:
        _emit_comtypes_unavailable_warning_once()
        return None

    # Each COM boundary is wrapped in BaseException — driver bugs
    # can surface as OSError, COMError, RuntimeError, even
    # AttributeError when the property store layout shifts. The
    # function contract is "best-effort, return None on any
    # failure"; callers fall back to the PortAudio surrogate hash.
    try:
        enumerator = CreateObject(
            _CLSID_MM_DEVICE_ENUMERATOR,
            interface=enumerator_cls,
        )
    except BaseException as exc:  # noqa: BLE001 — best-effort COM contract
        logger.debug(
            "voice.endpoint_fingerprint.create_enumerator_failed",
            reason=str(exc),
            exc_type=type(exc).__name__,
        )
        return None

    try:
        device = enumerator.GetDevice(endpoint_id)
    except BaseException as exc:  # noqa: BLE001 — best-effort COM contract
        logger.debug(
            "voice.endpoint_fingerprint.get_device_failed",
            reason=str(exc),
            exc_type=type(exc).__name__,
        )
        return None

    if device is None:
        return None

    try:
        propstore = device.OpenPropertyStore(_STGM_READ)
    except BaseException as exc:  # noqa: BLE001 — best-effort COM contract
        logger.debug(
            "voice.endpoint_fingerprint.open_property_store_failed",
            reason=str(exc),
            exc_type=type(exc).__name__,
        )
        return None

    if propstore is None:
        return None

    return _read_pnp_id_from_property_store(propstore)


def _read_pnp_id_from_property_store(propstore: Any) -> str | None:  # noqa: ANN401 — comtypes-generated COM POINTER types are dynamically typed; pinning to a Protocol would over-specify the structural shape and reject mock objects in tests
    """Extract ``PKEY_Device_InstanceId`` from an IPropertyStore.

    The COM chain is:

    1. Build a ``PROPERTYKEY`` (fmtid + pid) for ``PKEY_Device_InstanceId``.
    2. Call ``IPropertyStore::GetValue(key)`` — out-param is a
       ``PROPVARIANT``.
    3. Verify ``propvariant.vt == VT_LPWSTR`` (31). Anything else
       means the property exists but isn't the expected wide-string
       (drivers occasionally return ``VT_EMPTY`` for endpoints
       whose backing PnP device has been removed mid-session).
    4. Read ``propvariant.pwszVal`` as a ``LPWSTR`` (wide-string
       pointer).

    Returns the wide-string contents or ``None`` on any failure.
    """
    try:
        from comtypes import GUID
    except ImportError:
        # Defensive — _build_property_store_bindings already
        # succeeded, so comtypes IS importable. Documents the
        # contract for readers.
        return None

    # Build PROPERTYKEY for PKEY_Device_InstanceId.
    # The PropertyKey class lives inside _build_property_store_bindings's
    # closure; we re-create it here. comtypes auto-marshals the
    # struct by name, not by class identity, so this is safe.
    class _PropertyKeyLocal(ctypes.Structure):
        _fields_ = (("fmtid", GUID), ("pid", ctypes.c_uint))

    key = _PropertyKeyLocal()
    key.fmtid = GUID(_PKEY_DEVICE_INSTANCE_ID_FMTID)
    key.pid = _PKEY_DEVICE_INSTANCE_ID_PID

    try:
        propvariant = propstore.GetValue(ctypes.byref(key))
    except BaseException as exc:  # noqa: BLE001 — best-effort COM contract
        logger.debug(
            "voice.endpoint_fingerprint.get_value_failed",
            reason=str(exc),
            exc_type=type(exc).__name__,
        )
        return None

    if propvariant is None:
        return None

    try:
        vt = int(propvariant.vt)
    except (AttributeError, TypeError, ValueError):
        return None
    if vt != _VT_LPWSTR:
        return None

    try:
        pwsz_val = propvariant.pwszVal
    except AttributeError:
        return None

    if not pwsz_val:
        return None

    # ``pwszVal`` is a LPWSTR (wide-string pointer). Cast to
    # ``c_wchar_p`` to read it as a Python ``str``.
    try:
        pnp_id = ctypes.cast(pwsz_val, ctypes.c_wchar_p).value
    except (TypeError, ValueError, OSError):
        return None

    if not pnp_id:
        return None

    return pnp_id
