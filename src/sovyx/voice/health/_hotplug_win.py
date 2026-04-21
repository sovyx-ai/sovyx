"""Windows-specific ``WM_DEVICECHANGE`` hot-plug listener.

ADR §4.4.2:

* Create a message-only window (``HWND_MESSAGE`` parent) on a dedicated
  daemon thread.
* Call :func:`RegisterDeviceNotification` with
  ``DBT_DEVTYP_DEVICEINTERFACE`` filtered to ``KSCATEGORY_AUDIO`` so we
  only get notified about audio endpoints — not every USB key insertion.
* Pump :func:`PeekMessage` / :func:`DispatchMessage` until the window
  procedure receives ``WM_QUIT`` from :meth:`stop`.
* On ``DBT_DEVICEARRIVAL`` / ``DBT_DEVICEREMOVECOMPLETE`` translate the
  :class:`DEV_BROADCAST_DEVICEINTERFACE` payload into a
  :class:`~sovyx.voice.health.contract.HotplugEvent` and marshal it onto
  the asyncio event loop via :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.

The implementation is intentionally robust against the two common
degraded states:

1. ``pywin32`` is not installed (slim CI runners or a user who purged
   it). :func:`build_windows_hotplug_listener` returns
   :class:`NoopHotplugListener` and logs a WARNING.
2. ``CreateWindowEx`` / ``RegisterDeviceNotification`` fails (for
   example on Windows Nano Server where the USER32 window-manager
   subsystem is absent). The worker thread emits a WARNING + the
   listener silently degrades to no-op; daemon shutdown is unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes
import threading
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener
from sovyx.voice.health.contract import HotplugEvent, HotplugEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


# ── Win32 constants (from dbt.h / winuser.h / Ks.h) ─────────────────────
#
# Named verbatim for grep-ability; Microsoft docs reference them by
# these exact names.
_WM_DEVICECHANGE = 0x0219
_WM_QUIT = 0x0012
_WM_CLOSE = 0x0010

_DBT_DEVICEARRIVAL = 0x8000
_DBT_DEVICEREMOVECOMPLETE = 0x8004
_DBT_DEVTYP_DEVICEINTERFACE = 0x00000005

_DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

_HWND_MESSAGE = -3

# KSCATEGORY_AUDIO from ks.h — filters WM_DEVICECHANGE to audio endpoints.
_KSCATEGORY_AUDIO_GUID = "{6994AD04-93EF-11D0-A3CC-00A0C9223196}"


class _DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):  # noqa: N801 — Win32 API name
    _fields_ = (  # noqa: RUF012 — ctypes Structure contract
        ("dbcc_size", ctypes.wintypes.DWORD),
        ("dbcc_devicetype", ctypes.wintypes.DWORD),
        ("dbcc_reserved", ctypes.wintypes.DWORD),
        ("dbcc_classguid", ctypes.c_byte * 16),
        ("dbcc_name", ctypes.wintypes.WCHAR * 1),
    )


class _GUID(ctypes.Structure):
    _fields_ = (  # noqa: RUF012
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_byte * 8),
    )


class _DEV_BROADCAST_DEVICEINTERFACE_REGISTER(ctypes.Structure):  # noqa: N801 — Win32 API name
    _fields_ = (  # noqa: RUF012
        ("dbcc_size", ctypes.wintypes.DWORD),
        ("dbcc_devicetype", ctypes.wintypes.DWORD),
        ("dbcc_reserved", ctypes.wintypes.DWORD),
        ("dbcc_classguid", _GUID),
        ("dbcc_name", ctypes.wintypes.WCHAR * 1),
    )


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


def _guid_from_string(s: str) -> _GUID:
    """Parse ``{01234567-89ab-cdef-0123-456789abcdef}`` into a ``_GUID``.

    Lives here rather than in a shared helper because no other module
    needs the 16-byte COM layout and keeping it local avoids an import
    cycle on the Sprint 2 health package.
    """
    s = s.strip().lstrip("{").rstrip("}")
    parts = s.split("-")
    data1 = int(parts[0], 16)
    data2 = int(parts[1], 16)
    data3 = int(parts[2], 16)
    tail = bytes.fromhex(parts[3] + parts[4])
    guid = _GUID()
    guid.Data1 = data1
    guid.Data2 = data2
    guid.Data3 = data3
    for i, byte in enumerate(tail):
        guid.Data4[i] = byte
    return guid


def _extract_device_name(lparam: int) -> str:
    """Read the wide-string device interface path from ``DEV_BROADCAST``.

    Windows packs the path as a zero-terminated ``WCHAR`` array at the
    end of the struct. The struct declares size ``WCHAR*1`` for the
    field but the actual bytes on the wire are longer; we cast the
    LPARAM back to :class:`_DEV_BROADCAST_DEVICEINTERFACE_W` and read
    through :func:`ctypes.wstring_at` with the struct-reported length.
    """
    try:
        hdr = ctypes.cast(
            lparam,
            ctypes.POINTER(_DEV_BROADCAST_DEVICEINTERFACE_W),
        ).contents
    except Exception:  # noqa: BLE001 — OS payload trust boundary
        return ""
    if hdr.dbcc_devicetype != _DBT_DEVTYP_DEVICEINTERFACE:
        return ""
    name_offset = ctypes.sizeof(_DEV_BROADCAST_DEVICEINTERFACE_REGISTER) - ctypes.sizeof(
        ctypes.wintypes.WCHAR,
    )
    try:
        return ctypes.wstring_at(lparam + name_offset)
    except Exception:  # noqa: BLE001
        return ""


class WindowsHotplugListener:
    """Dedicated-thread ``WM_DEVICECHANGE`` subscriber.

    :meth:`start` spawns one daemon thread; the thread owns the
    message-only window and the device-notification handle for its
    entire lifetime. The asyncio event loop is captured at start time
    so the worker can post events back via ``call_soon_threadsafe``.

    :meth:`stop` is cooperative: it posts ``WM_CLOSE`` to the worker
    window so :func:`PeekMessage` unblocks, then joins the thread with
    a 2 s ceiling. If the join times out we log ERROR and detach — the
    thread is a daemon, so the process can still exit.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: Callable[[HotplugEvent], Awaitable[None]] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._hwnd: int | None = None
        self._notify_handle: int | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._on_event = on_event
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="sovyx-voice-hotplug-win",
            daemon=True,
        )
        self._thread.start()
        await asyncio.to_thread(self._ready.wait)
        if self._start_error is not None:
            logger.warning(
                "voice_hotplug_listener_start_failed",
                platform="win32",
                error=str(self._start_error),
                exc_info=(
                    type(self._start_error),
                    self._start_error,
                    self._start_error.__traceback__,
                ),
            )
            self._thread = None
            return
        self._started = True
        logger.info("voice_hotplug_listener_started", platform="win32")

    async def stop(self) -> None:
        thread = self._thread
        hwnd = self._hwnd
        self._started = False
        if hwnd is not None:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
                user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001 — best-effort shutdown
                logger.debug("voice_hotplug_listener_post_close_failed", exc_info=True)
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
            if thread.is_alive():
                logger.error(
                    "voice_hotplug_listener_stop_timeout",
                    platform="win32",
                )
        self._thread = None
        self._hwnd = None
        self._notify_handle = None

    # ── Worker-thread code ──────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._install_window()
        except BaseException as exc:  # noqa: BLE001 — surfaced to start()
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            self._pump_messages()
        finally:
            self._uninstall_window()

    def _install_window(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined, unused-ignore]

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = (  # noqa: RUF012
                ("style", ctypes.wintypes.UINT),
                ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HINSTANCE),
                ("hIcon", ctypes.wintypes.HICON),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.wintypes.HBRUSH),
                ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                ("lpszClassName", ctypes.wintypes.LPCWSTR),
            )

        self._wndproc = _WNDPROC(self._wndproc_impl)  # keep ref alive
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "SovyxVoiceHotplugWindow"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if atom == 0:
            raise ctypes.WinError()  # type: ignore[attr-defined, unused-ignore]
        hwnd = user32.CreateWindowExW(
            0,
            wc.lpszClassName,
            "SovyxVoiceHotplug",
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            wc.hInstance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()  # type: ignore[attr-defined, unused-ignore]
        self._hwnd = hwnd

        filt = _DEV_BROADCAST_DEVICEINTERFACE_REGISTER()
        filt.dbcc_size = ctypes.sizeof(filt)
        filt.dbcc_devicetype = _DBT_DEVTYP_DEVICEINTERFACE
        filt.dbcc_classguid = _guid_from_string(_KSCATEGORY_AUDIO_GUID)
        handle = user32.RegisterDeviceNotificationW(
            hwnd,
            ctypes.byref(filt),
            _DEVICE_NOTIFY_WINDOW_HANDLE,
        )
        if not handle:
            raise ctypes.WinError()  # type: ignore[attr-defined, unused-ignore]
        self._notify_handle = handle

    def _uninstall_window(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        if self._notify_handle:
            with contextlib.suppress(Exception):
                user32.UnregisterDeviceNotification(self._notify_handle)
            self._notify_handle = None
        if self._hwnd:
            with contextlib.suppress(Exception):
                user32.DestroyWindow(self._hwnd)
            self._hwnd = None

    def _pump_messages(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        msg = ctypes.wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc_impl(
        self,
        hwnd: int,
        msg: int,
        wparam: int,
        lparam: int,
    ) -> int:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        if msg == _WM_DEVICECHANGE:
            self._handle_device_change(wparam, lparam)
            return 1
        if msg == _WM_CLOSE:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _handle_device_change(self, wparam: int, lparam: int) -> None:
        if wparam == _DBT_DEVICEARRIVAL:
            kind = HotplugEventKind.DEVICE_ADDED
            event_name = "audio.device.arrived"
        elif wparam == _DBT_DEVICEREMOVECOMPLETE:
            kind = HotplugEventKind.DEVICE_REMOVED
            event_name = "audio.device.removed"
        else:
            return
        interface = _extract_device_name(lparam) if lparam else ""
        logger.info(
            event_name,
            **{
                "voice.platform": "win32",
                "voice.interface_name": interface or None,
            },
        )
        event = HotplugEvent(
            kind=kind,
            endpoint_guid=None,
            device_friendly_name=None,
            device_interface_name=interface or None,
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
            # Event loop already closed — daemon is shutting down.
            logger.debug("voice_hotplug_dispatch_loop_closed", exc_info=True)


def build_windows_hotplug_listener() -> HotplugListener:
    """Return a real listener on Windows hosts, or a no-op if USER32 is missing.

    Kept here (not in ``__init__.py``) so the :mod:`ctypes.windll` lookup
    cost only happens when someone actually needs the Windows listener.
    """
    try:
        _ = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
    except (AttributeError, OSError):
        logger.warning(
            "voice_hotplug_listener_unavailable",
            platform="win32",
            reason="user32_unavailable",
        )
        return NoopHotplugListener(reason="user32 unavailable")
    return WindowsHotplugListener()


__all__ = [
    "WindowsHotplugListener",
    "build_windows_hotplug_listener",
]
