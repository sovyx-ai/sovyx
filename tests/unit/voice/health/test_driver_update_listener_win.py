"""Tests for the Windows audio driver-update WMI listener.

Phase 5 / T5.49 — foundation. Pins the contract for the
:mod:`sovyx.voice.health._driver_update_listener_win` module:
factory branching, noop semantics, register / unregister
idempotency, defensive degradation when comtypes unavailable,
worker-thread orchestration, and the Indicate-callback event
marshalling contract.

The Windows worker thread + COM bindings are exercised via the
same comtypes-mocking pattern as
:mod:`tests.unit.voice.test_mm_notification_client`: patch
``_build_wmi_bindings`` + the lazy ``comtypes.client`` /
``comtypes.automation`` imports through ``sys.modules`` so the
tests run on every CI runner regardless of platform / extras.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import threading
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health import _driver_update_listener_win as module_under_test
from sovyx.voice.health._driver_update_listener_win import (
    DriverUpdateEvent,
    DriverUpdateListener,
    NoopDriverUpdateListener,
    WindowsDriverUpdateListener,
    _read_string_property,
    _read_target_instance,
    _unwrap_variant_to_class_object,
    _unwrap_variant_to_string,
    build_driver_update_listener,
)


@pytest.fixture
def loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Real asyncio loop captured at construction time. Tests never
    run anything on it; it's just a handle for `call_soon_threadsafe`.
    """
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        new_loop.close()


# ── Factory: build_driver_update_listener ───────────────────────────


class TestBuildDriverUpdateListenerFactory:
    """The cross-OS shim contract."""

    def test_disabled_returns_noop_on_any_platform(self, loop: asyncio.AbstractEventLoop) -> None:
        """``enabled=False`` always returns Noop, regardless of platform."""
        listener = build_driver_update_listener(
            loop,
            on_driver_changed=AsyncMock(),
            enabled=False,
        )
        assert isinstance(listener, NoopDriverUpdateListener)
        assert listener._reason == "flag_disabled"

    def test_default_enabled_is_false(self, loop: asyncio.AbstractEventLoop) -> None:
        """Foundation-phase default is False — calling without
        ``enabled=`` returns Noop.
        """
        listener = build_driver_update_listener(
            loop,
            on_driver_changed=AsyncMock(),
        )
        assert isinstance(listener, NoopDriverUpdateListener)

    def test_linux_returns_noop_with_reason_non_windows(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "linux"):
            listener = build_driver_update_listener(
                loop,
                on_driver_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, NoopDriverUpdateListener)
        assert listener._reason == "non_windows_platform"

    def test_darwin_returns_noop_with_reason_non_windows(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "darwin"):
            listener = build_driver_update_listener(
                loop,
                on_driver_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, NoopDriverUpdateListener)
        assert listener._reason == "non_windows_platform"

    def test_windows_enabled_returns_windows_listener(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "win32"):
            listener = build_driver_update_listener(
                loop,
                on_driver_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, WindowsDriverUpdateListener)


# ── NoopDriverUpdateListener ─────────────────────────────────────────


class TestNoopDriverUpdateListener:
    """Lifecycle contract — idempotent register/unregister + single log."""

    def test_register_logs_once(self, caplog: pytest.LogCaptureFixture) -> None:
        listener = NoopDriverUpdateListener(reason="flag_disabled")
        listener.register()
        first_logs = [r for r in caplog.records if "noop_register" in r.message]
        assert len(first_logs) == 1

    def test_register_idempotent(self, caplog: pytest.LogCaptureFixture) -> None:
        listener = NoopDriverUpdateListener(reason="flag_disabled")
        listener.register()
        caplog.clear()
        listener.register()
        listener.register()
        assert [r for r in caplog.records if "noop_register" in r.message] == []

    def test_unregister_idempotent(self) -> None:
        listener = NoopDriverUpdateListener(reason="flag_disabled")
        # Multiple unregister calls without register MUST not raise.
        listener.unregister()
        listener.unregister()
        listener.register()
        listener.unregister()
        listener.unregister()


# ── DriverUpdateListener Protocol structural conformance ─────────────


class TestProtocolConformance:
    """Both implementations satisfy the runtime-checkable Protocol."""

    def test_noop_satisfies_protocol(self) -> None:
        listener = NoopDriverUpdateListener(reason="flag_disabled")
        assert isinstance(listener, DriverUpdateListener)

    def test_windows_listener_satisfies_protocol(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=AsyncMock(),
        )
        assert isinstance(listener, DriverUpdateListener)


# ── WindowsDriverUpdateListener — comtypes unavailable path ──────────


class TestWindowsListenerComtypesUnavailable:
    """When comtypes isn't installed, register() emits WARN + no thread."""

    def test_register_emits_warn_when_comtypes_unavailable(
        self,
        loop: asyncio.AbstractEventLoop,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        listener = WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=AsyncMock(),
        )
        with patch.object(
            module_under_test,
            "_build_wmi_bindings",
            return_value=None,
        ):
            listener.register()
        assert any("comtypes_unavailable" in r.message for r in caplog.records)
        assert listener._thread is None

    def test_register_idempotent_when_comtypes_unavailable(
        self,
        loop: asyncio.AbstractEventLoop,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Second register call is a no-op even after a failed first call."""
        listener = WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=AsyncMock(),
        )
        with patch.object(
            module_under_test,
            "_build_wmi_bindings",
            return_value=None,
        ):
            listener.register()
            caplog.clear()
            listener.register()
        assert [r for r in caplog.records if "comtypes_unavailable" in r.message] == []


# ── WindowsDriverUpdateListener — worker thread orchestration ────────


class TestWindowsListenerWorkerThread:
    """Patch the COM-chain helpers + verify the worker thread sequence."""

    def _make_listener(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> WindowsDriverUpdateListener:
        return WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=AsyncMock(),
        )

    def _make_fake_bindings(self) -> tuple[Any, Any, Any, Any]:
        return (
            MagicMock(name="IWbemLocator"),
            MagicMock(name="IWbemServices"),
            MagicMock(name="IWbemObjectSink"),
            MagicMock(name="IWbemClassObject"),
        )

    def _patch_lazy_comtypes(self) -> Any:
        """Patch the lazy ``import comtypes`` + ``import comtypes.client``
        the worker thread does. Returns the contextmanager.
        """
        fake_comtypes = MagicMock(name="comtypes")
        fake_comtypes.CoInitializeEx = MagicMock()
        fake_comtypes.CoUninitialize = MagicMock()
        fake_comtypes.COMObject = MagicMock(name="COMObject")
        fake_comtypes_client = MagicMock(name="comtypes.client")
        fake_comtypes_client.CreateObject = MagicMock(return_value=MagicMock())
        return patch.dict(
            sys.modules,
            {
                "comtypes": fake_comtypes,
                "comtypes.client": fake_comtypes_client,
            },
        )

    def test_register_spawns_worker_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = self._make_listener(loop)
        bindings = self._make_fake_bindings()

        thread_started_event = threading.Event()

        def _block_until_signaled(*args: Any, **kwargs: Any) -> None:
            thread_started_event.set()
            # Block forever — unregister will set self._stop_event.
            listener._stop_event.wait()

        with (
            patch.object(
                module_under_test,
                "_build_wmi_bindings",
                return_value=bindings,
            ),
            patch.object(
                WindowsDriverUpdateListener,
                "_run_worker",
                _block_until_signaled,
            ),
        ):
            listener.register()
            try:
                thread_started_event.wait(timeout=2.0)
                assert thread_started_event.is_set()
                assert listener._thread is not None
                assert listener._thread.is_alive()
            finally:
                listener.unregister()
                # Worker should exit after stop_event is set.
                if listener._thread is not None and listener._thread.is_alive():
                    listener._thread.join(timeout=2.0)

    def test_register_idempotent(self, loop: asyncio.AbstractEventLoop) -> None:
        """Second register call doesn't spawn a second thread."""
        listener = self._make_listener(loop)
        bindings = self._make_fake_bindings()

        def _block_until_signaled(*args: Any, **kwargs: Any) -> None:
            listener._stop_event.wait()

        with (
            patch.object(
                module_under_test,
                "_build_wmi_bindings",
                return_value=bindings,
            ),
            patch.object(
                WindowsDriverUpdateListener,
                "_run_worker",
                _block_until_signaled,
            ),
        ):
            listener.register()
            first_thread = listener._thread
            listener.register()  # Second call — must be a no-op.
            assert listener._thread is first_thread
            listener.unregister()

    def test_unregister_signals_stop_and_joins(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = self._make_listener(loop)
        bindings = self._make_fake_bindings()

        def _block_until_signaled(*args: Any, **kwargs: Any) -> None:
            listener._stop_event.wait()

        with (
            patch.object(
                module_under_test,
                "_build_wmi_bindings",
                return_value=bindings,
            ),
            patch.object(
                WindowsDriverUpdateListener,
                "_run_worker",
                _block_until_signaled,
            ),
        ):
            listener.register()
            assert listener._thread is not None
            assert listener._thread.is_alive()
            listener.unregister()
            assert listener._stop_event.is_set()
            # After unregister, _thread is reset to None.
            assert listener._thread is None

    def test_unregister_idempotent_without_register(self, loop: asyncio.AbstractEventLoop) -> None:
        """Calling unregister without register MUST not raise."""
        listener = self._make_listener(loop)
        listener.unregister()
        listener.unregister()


# ── _run_worker chain — patch helpers + verify call sequence ─────────


class TestRunWorkerChain:
    """Patch each COM-chain helper + assert the orchestration order."""

    def _make_listener(self, loop: asyncio.AbstractEventLoop) -> WindowsDriverUpdateListener:
        return WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=AsyncMock(),
        )

    def _patch_comtypes_module(self) -> Any:
        fake_comtypes = MagicMock(name="comtypes")
        fake_comtypes.CoInitializeEx = MagicMock()
        fake_comtypes.CoUninitialize = MagicMock()
        return patch.dict(sys.modules, {"comtypes": fake_comtypes})

    def test_coinitialize_failure_returns_early(
        self,
        loop: asyncio.AbstractEventLoop,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        listener = self._make_listener(loop)
        bindings = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        fake_comtypes = MagicMock(name="comtypes")
        fake_comtypes.CoInitializeEx = MagicMock(side_effect=OSError("CoInitializeEx E_FAIL"))
        fake_comtypes.CoUninitialize = MagicMock()

        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            listener._run_worker(bindings)

        assert any("coinitialize_failed" in r.message for r in caplog.records)

    def test_create_locator_failure_short_circuits(
        self,
        loop: asyncio.AbstractEventLoop,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        listener = self._make_listener(loop)
        bindings = (MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with (
            self._patch_comtypes_module(),
            patch.object(listener, "_create_locator", return_value=None),
        ):
            listener._run_worker(bindings)

        # Connect / sink-build / exec / cancel should NOT have been
        # called when the locator creation failed.
        # (We can't directly assert "not called" on the bound methods
        # since we only patched _create_locator; the other helpers
        # would have raised AttributeError on the None locator if
        # the short-circuit wasn't honoured.)

    def test_connect_server_failure_short_circuits(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = self._make_listener(loop)
        bindings = (MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with (
            self._patch_comtypes_module(),
            patch.object(listener, "_create_locator", return_value=MagicMock()),
            patch.object(listener, "_connect_server", return_value=None),
            patch.object(listener, "_build_sink") as mock_build_sink,
            patch.object(listener, "_exec_subscription") as mock_exec,
        ):
            listener._run_worker(bindings)
            mock_build_sink.assert_not_called()
            mock_exec.assert_not_called()

    def test_full_happy_path_runs_to_subscription_then_blocks(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """The worker should call create→connect→build_sink→exec→wait→cancel."""
        listener = self._make_listener(loop)
        bindings = (MagicMock(), MagicMock(), MagicMock(), MagicMock())

        mock_locator = MagicMock(name="locator")
        mock_services = MagicMock(name="services")
        mock_sink = MagicMock(name="sink")

        # Pre-set the stop_event so the worker doesn't actually block.
        listener._stop_event.set()

        with (
            self._patch_comtypes_module(),
            patch.object(listener, "_create_locator", return_value=mock_locator),
            patch.object(listener, "_connect_server", return_value=mock_services),
            patch.object(listener, "_build_sink", return_value=mock_sink),
            patch.object(listener, "_exec_subscription", return_value=True) as mock_exec,
            patch.object(listener, "_cancel_subscription") as mock_cancel,
        ):
            listener._run_worker(bindings)

        mock_exec.assert_called_once_with(mock_services, mock_sink)
        mock_cancel.assert_called_once_with(mock_services, mock_sink)

    def test_exec_subscription_failure_skips_cancel(self, loop: asyncio.AbstractEventLoop) -> None:
        """When ExecNotificationQueryAsync fails, we don't try to
        cancel a subscription that never started.
        """
        listener = self._make_listener(loop)
        bindings = (MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with (
            self._patch_comtypes_module(),
            patch.object(listener, "_create_locator", return_value=MagicMock()),
            patch.object(listener, "_connect_server", return_value=MagicMock()),
            patch.object(listener, "_build_sink", return_value=MagicMock()),
            patch.object(listener, "_exec_subscription", return_value=False),
            patch.object(listener, "_cancel_subscription") as mock_cancel,
        ):
            listener._run_worker(bindings)
            mock_cancel.assert_not_called()


# ── Dispatcher: _dispatch_event ──────────────────────────────────────


class TestDispatchEvent:
    """The dispatcher invokes the user callback on the asyncio loop."""

    def test_dispatch_invokes_callback(self, loop: asyncio.AbstractEventLoop) -> None:
        callback = AsyncMock()
        listener = WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=callback,
        )
        event = DriverUpdateEvent(
            device_id=r"USB\VID_046D&PID_0A45\AB12CD34",
            friendly_name="Test mic",
            new_driver_version="1.2.3.4",
            detected_at=_dt.datetime.now(_dt.UTC),
        )
        listener._dispatch_event(event)

        # ``ensure_future`` schedules the coroutine but doesn't run
        # it until the loop runs. Run one iteration so the callback
        # is awaited.
        async def _drain() -> None:
            # Yield control so the scheduled future executes.
            await asyncio.sleep(0)

        loop.run_until_complete(_drain())
        callback.assert_called_once_with(event)

    def test_dispatch_isolates_callback_exceptions(
        self,
        loop: asyncio.AbstractEventLoop,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An exception raised by ``on_driver_changed`` must NOT
        propagate back to the WMI thread (anti-pattern #29 contract).
        """

        def _raising_callback(event: DriverUpdateEvent) -> Any:  # noqa: ARG001 — sync signature stand-in for the async callback to force ensure_future to fail synchronously
            raise OSError("user callback exploded")

        listener = WindowsDriverUpdateListener(
            loop=loop,
            on_driver_changed=_raising_callback,
        )
        event = DriverUpdateEvent(
            device_id="device",
            friendly_name="name",
            new_driver_version="",
            detected_at=_dt.datetime.now(_dt.UTC),
        )
        # Dispatch must NOT raise.
        listener._dispatch_event(event)
        assert any("dispatch_failed" in r.message for r in caplog.records)


# ── VARIANT property reading helpers ─────────────────────────────────


class TestReadStringProperty:
    """Branch coverage for the COM-bound IWbemClassObject.Get wrapper.

    The COM-bound side (Get + ctypes byref) is hard to unit-test
    cleanly without real comtypes / ctypes. We exercise:

    * The defensive ``except BaseException`` path — Get raises →
      function returns None.
    * The post-unwrap coercion logic via the pure helper
      :func:`_unwrap_variant_to_string` (full coverage in
      :class:`TestUnwrapVariantToString`).
    """

    def test_get_raises_returns_none(self) -> None:
        """When the COM Get call raises, the function returns None
        without surfacing the exception (anti-pattern #29 contract).
        """
        mock_class_object = MagicMock(name="class_object")
        mock_class_object.Get = MagicMock(side_effect=OSError("WBEM_E_NOT_FOUND"))
        result = _read_string_property(mock_class_object, "anything")
        assert result is None


class TestUnwrapVariantToString:
    """Pure-helper coverage for the VARIANT-value coercion rules.

    Tests pass plain Python values directly — no ctypes / comtypes
    interop needed because the helper operates on the post-VARIANT-
    unwrap value.
    """

    def test_none_input_returns_none(self) -> None:
        assert _unwrap_variant_to_string(None) is None

    def test_string_input_passes_through(self) -> None:
        assert _unwrap_variant_to_string("test-string") == "test-string"

    def test_empty_string_passes_through(self) -> None:
        # Empty string is distinct from None (property exists but
        # is empty).
        assert _unwrap_variant_to_string("") == ""

    def test_bytes_input_decoded_utf8(self) -> None:
        assert _unwrap_variant_to_string(b"byte-string") == "byte-string"

    def test_bytes_invalid_utf8_uses_replace(self) -> None:
        """Invalid UTF-8 bytes don't raise — they fall back to
        the Unicode replacement character per the
        ``errors="replace"`` policy.
        """
        result = _unwrap_variant_to_string(b"\xff\xfe-not-utf8")
        assert result is not None
        # The first two bytes are invalid UTF-8 → replaced; the
        # tail "-not-utf8" survives.
        assert "-not-utf8" in result

    def test_integer_input_str_coerced(self) -> None:
        assert _unwrap_variant_to_string(12345) == "12345"

    def test_arbitrary_object_str_coerced(self) -> None:
        class _Custom:
            def __str__(self) -> str:
                return "custom-repr"

        assert _unwrap_variant_to_string(_Custom()) == "custom-repr"


class TestReadTargetInstance:
    """Branch coverage for the COM-bound TargetInstance extraction.

    Same split as :class:`TestReadStringProperty` — the COM-bound
    side is tested via the defensive raise path, the post-unwrap
    logic via the pure helper :func:`_unwrap_variant_to_class_object`.
    """

    def test_get_raises_returns_none(self) -> None:
        mock_event = MagicMock(name="event")
        mock_event.Get = MagicMock(side_effect=OSError("WBEM_E_NOT_FOUND"))
        result = _read_target_instance(mock_event, MagicMock())
        assert result is None


class TestUnwrapVariantToClassObject:
    """Pure-helper coverage for the VARIANT → IWbemClassObject
    QueryInterface step.
    """

    def test_none_input_returns_none(self) -> None:
        assert _unwrap_variant_to_class_object(None, MagicMock()) is None

    def test_query_interface_success(self) -> None:
        mock_class_object = MagicMock(name="class_object")
        mock_unknown = MagicMock(name="unknown")
        mock_unknown.QueryInterface = MagicMock(return_value=mock_class_object)
        class_object_cls = MagicMock(name="IWbemClassObject")
        result = _unwrap_variant_to_class_object(mock_unknown, class_object_cls)
        assert result is mock_class_object
        mock_unknown.QueryInterface.assert_called_once_with(class_object_cls)

    def test_query_interface_raises_returns_none(self) -> None:
        """Alien COM object that doesn't implement the requested
        interface — QueryInterface raises, function returns None
        without propagating.
        """
        mock_unknown = MagicMock(name="unknown")
        mock_unknown.QueryInterface = MagicMock(side_effect=OSError("E_NOINTERFACE"))
        result = _unwrap_variant_to_class_object(mock_unknown, MagicMock())
        assert result is None
