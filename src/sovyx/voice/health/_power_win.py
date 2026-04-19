"""Windows-specific ``WM_POWERBROADCAST`` power-event listener.

ADR §4.4.4 — subscribe to ``PBT_APMSUSPEND`` and ``PBT_APMRESUMEAUTOMATIC``
via a dedicated message-only window so the watchdog learns about sleep
and resume transitions without requiring Group Policy rights or the
heavier ``IORegisterForSystemPower`` / D-Bus channels.

Implementation mirrors :mod:`~sovyx.voice.health._hotplug_win`: a
daemon thread owns a ``HWND_MESSAGE`` window whose window procedure
translates ``WM_POWERBROADCAST`` messages into
:class:`~sovyx.voice.health.contract.PowerEvent` instances, which are
marshalled onto the caller's asyncio loop via
:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.

Degrades to :class:`NoopPowerEventListener` when USER32 is absent (Nano
Server) or any Win32 call in :meth:`_install_window` fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes
import threading
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._power import NoopPowerEventListener, PowerEventListener
from sovyx.voice.health.contract import PowerEvent, PowerEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


# ── Win32 constants (winuser.h) ─────────────────────────────────────────
_WM_POWERBROADCAST = 0x0218
_WM_CLOSE = 0x0010

_PBT_APMSUSPEND = 0x0004
_PBT_APMRESUMEAUTOMATIC = 0x0012
_PBT_APMRESUMESUSPEND = 0x0007  # user-present resume — treated same as auto

_HWND_MESSAGE = -3


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


class WindowsPowerEventListener:
    """Dedicated-thread ``WM_POWERBROADCAST`` subscriber.

    See module docstring for lifecycle semantics. The thread is spawned
    lazily on :meth:`start`; :meth:`stop` posts ``WM_CLOSE`` and joins
    with a 2 s ceiling so daemon shutdown cannot be stalled by a
    misbehaving Win32 session.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: Callable[[PowerEvent], Awaitable[None]] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._hwnd: int | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[PowerEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._on_event = on_event
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="sovyx-voice-power-win",
            daemon=True,
        )
        self._thread.start()
        await asyncio.to_thread(self._ready.wait)
        if self._start_error is not None:
            logger.warning(
                "voice_power_listener_start_failed",
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
        logger.info("voice_power_listener_started", platform="win32")

    async def stop(self) -> None:
        thread = self._thread
        hwnd = self._hwnd
        self._started = False
        if hwnd is not None:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
                user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                logger.debug("voice_power_listener_post_close_failed", exc_info=True)
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
            if thread.is_alive():
                logger.error(
                    "voice_power_listener_stop_timeout",
                    platform="win32",
                )
        self._thread = None
        self._hwnd = None

    # ── Worker-thread code ──────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._install_window()
        except BaseException as exc:  # noqa: BLE001
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
        wc.lpszClassName = "SovyxVoicePowerWindow"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if atom == 0:
            raise ctypes.WinError()  # type: ignore[attr-defined, unused-ignore]
        hwnd = user32.CreateWindowExW(
            0,
            wc.lpszClassName,
            "SovyxVoicePower",
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

    def _uninstall_window(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
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
        if msg == _WM_POWERBROADCAST:
            self._handle_power(wparam)
            return 1
        if msg == _WM_CLOSE:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _handle_power(self, wparam: int) -> None:
        if wparam == _PBT_APMSUSPEND:
            kind = PowerEventKind.SUSPEND
        elif wparam in (_PBT_APMRESUMEAUTOMATIC, _PBT_APMRESUMESUSPEND):
            kind = PowerEventKind.RESUME
        else:
            return
        self._dispatch(PowerEvent(kind=kind))

    def _dispatch(self, event: PowerEvent) -> None:
        loop = self._loop
        on_event = self._on_event
        if loop is None or on_event is None:
            return
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(on_event(event), loop=loop),
            )
        except RuntimeError:
            logger.debug("voice_power_dispatch_loop_closed", exc_info=True)


def build_windows_power_listener() -> PowerEventListener:
    """Return a real listener on Windows, or Noop when USER32 is missing."""
    try:
        _ = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
    except (AttributeError, OSError):
        logger.warning(
            "voice_power_listener_unavailable",
            platform="win32",
            reason="user32_unavailable",
        )
        return NoopPowerEventListener(reason="user32 unavailable")
    return WindowsPowerEventListener()


__all__ = [
    "WindowsPowerEventListener",
    "build_windows_power_listener",
]
