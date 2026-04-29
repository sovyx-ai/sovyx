"""Tests for :mod:`sovyx.voice._mm_notification_client` — Voice
Windows Paranoid Mission §C foundation.

v0.24.0 ships the cross-OS shim contract + factory + non-Windows /
disabled no-op surface. The Windows COM bindings (comtypes-based
``IMMDeviceEnumerator`` registration) land in v0.25.0 wire-up. These
tests pin the v0.24.0 contract:

* ``create_listener`` factory branches correctly on
  ``enabled=False`` / non-Windows / Windows-enabled-but-not-wired.
* :class:`NoopMMNotificationListener` honours the lifecycle
  contract (idempotent register / unregister, single INFO log,
  fires the ``voice.hotplug.listener.registered{registered=false}``
  metric).
* :class:`WindowsMMNotificationListener` v0.24.0 placeholder logs
  the WARN + records the ``not_yet_wired_v024`` metric on
  :meth:`register`.
* The :class:`MMNotificationListener` Protocol is structurally
  satisfied by both implementations.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice._mm_notification_client import (
    MMNotificationListener,
    NoopMMNotificationListener,
    WindowsMMNotificationListener,
    create_listener,
)


@pytest.fixture
def loop() -> asyncio.AbstractEventLoop:
    """A real asyncio loop — captured at construction time by the
    Windows path. We never run anything on it; it's just a handle."""
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        new_loop.close()


# ── Factory: create_listener ────────────────────────────────────────


class TestCreateListenerEnabledFalse:
    """``enabled=False`` always returns the no-op listener regardless
    of platform — operator-flippable opt-in default through v0.25.0."""

    def test_disabled_returns_noop_on_any_platform(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = create_listener(
            loop,
            on_default_capture_changed=AsyncMock(),
            on_device_state_changed=AsyncMock(),
            enabled=False,
        )
        assert isinstance(listener, NoopMMNotificationListener)
        assert listener._reason == "flag_disabled"  # type: ignore[attr-defined]

    def test_default_enabled_is_false(self, loop: asyncio.AbstractEventLoop) -> None:
        """The foundation-phase default is False — calling without
        ``enabled=`` returns the no-op listener."""
        listener = create_listener(
            loop,
            on_default_capture_changed=AsyncMock(),
            on_device_state_changed=AsyncMock(),
        )
        assert isinstance(listener, NoopMMNotificationListener)


class TestCreateListenerNonWindows:
    """Non-Windows platforms always get the no-op listener — Linux +
    macOS hot-plug events flow through their dedicated detectors."""

    def test_linux_returns_noop_with_reason_non_windows(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "linux"):
            listener = create_listener(
                loop,
                on_default_capture_changed=AsyncMock(),
                on_device_state_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, NoopMMNotificationListener)
        assert listener._reason == "non_windows_platform"  # type: ignore[attr-defined]

    def test_darwin_returns_noop_with_reason_non_windows(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "darwin"):
            listener = create_listener(
                loop,
                on_default_capture_changed=AsyncMock(),
                on_device_state_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, NoopMMNotificationListener)
        assert listener._reason == "non_windows_platform"  # type: ignore[attr-defined]


class TestCreateListenerWindowsEnabled:
    """``enabled=True`` on Windows returns the placeholder Windows
    listener (v0.24.0) / real COM subscriber (v0.25.0+)."""

    def test_windows_enabled_returns_windows_listener(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with patch.object(sys, "platform", "win32"):
            listener = create_listener(
                loop,
                on_default_capture_changed=AsyncMock(),
                on_device_state_changed=AsyncMock(),
                on_property_value_changed=AsyncMock(),
                enabled=True,
            )
        assert isinstance(listener, WindowsMMNotificationListener)


# ── NoopMMNotificationListener ──────────────────────────────────────


class TestNoopMMNotificationListener:
    """The non-Win / disabled fallback. Logs once, fires the
    ``registered=false`` metric, honours the idempotent lifecycle."""

    def test_register_logs_single_info(self, caplog: pytest.LogCaptureFixture) -> None:
        listener = NoopMMNotificationListener(reason="flag_disabled")
        caplog.set_level("INFO")
        listener.register()
        # Find the structured event among caplog records (sovyx
        # structlog renders the dict in the message string).
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.noop_register" in r.getMessage()
        ]
        assert len(matching) == 1
        assert "flag_disabled" in matching[0].getMessage()

    def test_register_is_idempotent(self, caplog: pytest.LogCaptureFixture) -> None:
        listener = NoopMMNotificationListener(reason="non_windows_platform")
        caplog.set_level("INFO")
        listener.register()
        listener.register()
        listener.register()
        # Single log emission despite three calls.
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.noop_register" in r.getMessage()
        ]
        assert len(matching) == 1

    def test_unregister_is_idempotent_without_register(self) -> None:
        """``unregister`` must never raise — the capture task lifecycle
        calls it inside try/finally during stop() even when register
        was skipped."""
        listener = NoopMMNotificationListener(reason="flag_disabled")
        listener.unregister()  # no error
        listener.unregister()  # no error

    def test_register_emits_registered_false_metric(self) -> None:
        """The no-op path still bumps the
        ``voice.hotplug.listener.registered`` counter with
        ``registered=false`` — fleet dashboards split active vs no-op
        rates this way."""
        with patch(
            "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
        ) as mock_record:
            listener = NoopMMNotificationListener(reason="flag_disabled")
            listener.register()
            mock_record.assert_called_once_with(
                registered=False,
                error="flag_disabled",
            )

    def test_register_idempotent_does_not_double_record(self) -> None:
        with patch(
            "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
        ) as mock_record:
            listener = NoopMMNotificationListener(reason="flag_disabled")
            listener.register()
            listener.register()
            assert mock_record.call_count == 1

    def test_satisfies_protocol(self) -> None:
        """``isinstance(x, MMNotificationListener)`` succeeds via
        runtime_checkable Protocol — the cross-OS shim contract."""
        listener = NoopMMNotificationListener(reason="flag_disabled")
        assert isinstance(listener, MMNotificationListener)


# ── WindowsMMNotificationListener (v0.24.0 placeholder) ────────────


class TestWindowsMMNotificationListenerV025WireUp:
    """v0.25.0 wire-up (T31) — register() lazy-imports comtypes,
    activates IMMDeviceEnumerator, registers the COMObject. Every
    COM boundary is wrapped in try/except BaseException with
    fallback to a no-op + structured WARN. The tests below pin the
    contract via :func:`_build_com_bindings` mocking so they run on
    any platform regardless of comtypes installation."""

    def _make(self, loop: asyncio.AbstractEventLoop) -> WindowsMMNotificationListener:
        return WindowsMMNotificationListener(
            loop=loop,
            on_default_capture_changed=AsyncMock(),
            on_device_state_changed=AsyncMock(),
            on_property_value_changed=AsyncMock(),
        )

    def test_register_falls_through_when_comtypes_unavailable(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing comtypes → WARN + ``comtypes_import_failed`` metric.
        Non-Windows / slim-CI hosts hit this path. Daemon does NOT
        crash; the watchdog polling fallback keeps device-change
        detection alive at 5 s resolution."""
        with patch(
            "sovyx.voice._mm_notification_client._build_com_bindings",
            return_value=None,
        ):
            listener = self._make(loop)
            caplog.set_level("WARNING")
            listener.register()
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.comtypes_unavailable" in r.getMessage()
        ]
        assert len(matching) == 1
        # listener is "registered" in the sense that idempotency
        # latches — a second register() is a no-op.
        assert listener._registered is True  # type: ignore[attr-defined]

    def test_register_records_comtypes_import_failed_metric(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=None,
            ),
            patch(
                "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
            ) as mock_record,
        ):
            listener = self._make(loop)
            listener.register()
            mock_record.assert_called_once_with(
                registered=False,
                error="comtypes_import_failed",
            )

    def test_register_is_idempotent(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Second / third register() → no-op once latched."""
        with patch(
            "sovyx.voice._mm_notification_client._build_com_bindings",
            return_value=None,
        ):
            listener = self._make(loop)
            caplog.set_level("WARNING")
            listener.register()
            listener.register()
            listener.register()
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.comtypes_unavailable" in r.getMessage()
        ]
        assert len(matching) == 1

    def test_unregister_is_idempotent_without_register(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        listener = self._make(loop)
        listener.unregister()  # no error
        listener.unregister()  # no error

    def test_unregister_after_register_resets_state(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After unregister, calling register again should re-attempt
        the wire-up — the lifecycle is not "fire once forever"."""
        with patch(
            "sovyx.voice._mm_notification_client._build_com_bindings",
            return_value=None,
        ):
            listener = self._make(loop)
            caplog.set_level("WARNING")
            listener.register()
            listener.unregister()
            listener.register()
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.comtypes_unavailable" in r.getMessage()
        ]
        assert len(matching) == 2

    def test_register_records_success_metric_on_happy_path(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Happy-path — COM activation + registration succeed →
        registered=True metric + ``voice.mm_notification_client.registered``
        INFO."""
        # Build mock COM bindings + mock comtypes.client.CreateObject.
        mock_iface = MagicMock(name="IMMNotificationClient")
        mock_enum_cls = MagicMock(name="IMMDeviceEnumerator")
        mock_propkey = MagicMock(name="PROPERTYKEY")
        mock_enumerator = MagicMock(name="enumerator-instance")
        mock_com_object = MagicMock(name="COMObject-base")

        # Synthesize a fake comtypes module so the lazy
        # ``from comtypes import COMObject`` succeeds.
        fake_comtypes = MagicMock(name="comtypes")
        fake_comtypes.COMObject = mock_com_object
        fake_comtypes_client = MagicMock(name="comtypes.client")
        fake_comtypes.client = fake_comtypes_client  # bind so import comtypes.client returns the mock
        fake_comtypes_client.CreateObject = MagicMock(return_value=mock_enumerator)

        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=(mock_iface, mock_enum_cls, mock_propkey),
            ),
            patch.dict(
                sys.modules,
                {
                    "comtypes": fake_comtypes,
                    "comtypes.client": fake_comtypes_client,
                },
            ),
            patch(
                "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
            ) as mock_record,
        ):
            listener = self._make(loop)
            caplog.set_level("INFO")
            listener.register()

        # Success metric.
        mock_record.assert_called_once_with(registered=True)
        # Success log.
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.registered" in r.getMessage()
            and "voice.mm_notification_client.comtypes_unavailable" not in r.getMessage()
        ]
        assert len(matching) >= 1
        # COM activation actually called.
        fake_comtypes_client.CreateObject.assert_called_once()
        # COM register actually called.
        mock_enumerator.RegisterEndpointNotificationCallback.assert_called_once()

    def test_register_falls_through_on_create_object_failure(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``CreateObject`` raises COMError / OSError → defensive
        catch → ``create_object_failed`` metric + ERROR log. Daemon
        does NOT crash."""
        mock_iface = MagicMock()
        mock_enum_cls = MagicMock()
        mock_propkey = MagicMock()

        fake_comtypes = MagicMock()
        fake_comtypes.COMObject = MagicMock()
        fake_comtypes_client = MagicMock()
        fake_comtypes.client = fake_comtypes_client  # bind so import comtypes.client returns the mock
        fake_comtypes_client.CreateObject = MagicMock(side_effect=OSError("COM error"))

        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=(mock_iface, mock_enum_cls, mock_propkey),
            ),
            patch.dict(
                sys.modules,
                {
                    "comtypes": fake_comtypes,
                    "comtypes.client": fake_comtypes_client,
                },
            ),
            patch(
                "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
            ) as mock_record,
        ):
            listener = self._make(loop)
            caplog.set_level("ERROR")
            listener.register()  # MUST NOT raise
        mock_record.assert_called_once_with(
            registered=False,
            error="create_object_failed",
        )
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.create_object_failed" in r.getMessage()
        ]
        assert len(matching) == 1
        assert listener._registered is True  # type: ignore[attr-defined]

    def test_register_falls_through_on_register_callback_failure(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``RegisterEndpointNotificationCallback`` raises → defensive
        catch → ``register_callback_failed`` metric + ERROR log."""
        mock_iface = MagicMock()
        mock_enum_cls = MagicMock()
        mock_propkey = MagicMock()
        mock_enumerator = MagicMock()
        mock_enumerator.RegisterEndpointNotificationCallback = MagicMock(
            side_effect=OSError("driver bug"),
        )

        fake_comtypes = MagicMock()
        fake_comtypes.COMObject = MagicMock()
        fake_comtypes_client = MagicMock()
        fake_comtypes.client = fake_comtypes_client  # bind so import comtypes.client returns the mock
        fake_comtypes_client.CreateObject = MagicMock(return_value=mock_enumerator)

        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=(mock_iface, mock_enum_cls, mock_propkey),
            ),
            patch.dict(
                sys.modules,
                {
                    "comtypes": fake_comtypes,
                    "comtypes.client": fake_comtypes_client,
                },
            ),
            patch(
                "sovyx.voice._mm_notification_client.record_hotplug_listener_registered"
            ) as mock_record,
        ):
            listener = self._make(loop)
            caplog.set_level("ERROR")
            listener.register()  # MUST NOT raise
        mock_record.assert_called_once_with(
            registered=False,
            error="register_callback_failed",
        )
        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.register_callback_failed" in r.getMessage()
        ]
        assert len(matching) == 1

    def test_unregister_releases_com_resources_on_happy_path(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """After successful register, unregister calls
        ``UnregisterEndpointNotificationCallback`` and clears the
        held COM resources."""
        mock_iface = MagicMock()
        mock_enum_cls = MagicMock()
        mock_propkey = MagicMock()
        mock_enumerator = MagicMock()

        fake_comtypes = MagicMock()
        fake_comtypes.COMObject = MagicMock()
        fake_comtypes_client = MagicMock()
        fake_comtypes.client = fake_comtypes_client  # bind so import comtypes.client returns the mock
        fake_comtypes_client.CreateObject = MagicMock(return_value=mock_enumerator)

        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=(mock_iface, mock_enum_cls, mock_propkey),
            ),
            patch.dict(
                sys.modules,
                {
                    "comtypes": fake_comtypes,
                    "comtypes.client": fake_comtypes_client,
                },
            ),
        ):
            listener = self._make(loop)
            listener.register()
            assert listener._enumerator is mock_enumerator  # type: ignore[attr-defined]
            assert listener._com_client is not None  # type: ignore[attr-defined]

            listener.unregister()

        mock_enumerator.UnregisterEndpointNotificationCallback.assert_called_once()
        # COM resources released.
        assert listener._enumerator is None  # type: ignore[attr-defined]
        assert listener._com_client is None  # type: ignore[attr-defined]
        assert listener._registered is False  # type: ignore[attr-defined]

    def test_unregister_swallows_com_failure_on_shutdown(
        self, loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Wedged COM thread on shutdown → ``Unregister`` raises →
        defensive catch + WARN. Daemon shutdown continues
        regardless."""
        mock_iface = MagicMock()
        mock_enum_cls = MagicMock()
        mock_propkey = MagicMock()
        mock_enumerator = MagicMock()
        mock_enumerator.UnregisterEndpointNotificationCallback = MagicMock(
            side_effect=OSError("wedged COM thread"),
        )

        fake_comtypes = MagicMock()
        fake_comtypes.COMObject = MagicMock()
        fake_comtypes_client = MagicMock()
        fake_comtypes.client = fake_comtypes_client  # bind so import comtypes.client returns the mock
        fake_comtypes_client.CreateObject = MagicMock(return_value=mock_enumerator)

        with (
            patch(
                "sovyx.voice._mm_notification_client._build_com_bindings",
                return_value=(mock_iface, mock_enum_cls, mock_propkey),
            ),
            patch.dict(
                sys.modules,
                {
                    "comtypes": fake_comtypes,
                    "comtypes.client": fake_comtypes_client,
                },
            ),
        ):
            listener = self._make(loop)
            listener.register()
            caplog.set_level("WARNING")
            listener.unregister()  # MUST NOT raise

        matching = [
            r
            for r in caplog.records
            if "voice.mm_notification_client.unregister_callback_failed" in r.getMessage()
        ]
        assert len(matching) == 1

    def test_satisfies_protocol(self, loop: asyncio.AbstractEventLoop) -> None:
        listener = self._make(loop)
        assert isinstance(listener, MMNotificationListener)

    def test_callbacks_stored_for_v025_wireup(self, loop: asyncio.AbstractEventLoop) -> None:
        """The constructor captures the asyncio loop + the 3 callbacks
        — v0.25.0 wire-up uses these from the COM-thread callback
        bodies via call_soon_threadsafe."""
        on_default_capture = AsyncMock()
        on_device_state = AsyncMock()
        on_property_value = AsyncMock()
        listener = WindowsMMNotificationListener(
            loop=loop,
            on_default_capture_changed=on_default_capture,
            on_device_state_changed=on_device_state,
            on_property_value_changed=on_property_value,
        )
        assert listener._loop is loop  # type: ignore[attr-defined]
        assert listener._on_default_capture_changed is on_default_capture  # type: ignore[attr-defined]
        assert listener._on_device_state_changed is on_device_state  # type: ignore[attr-defined]
        assert listener._on_property_value_changed is on_property_value  # type: ignore[attr-defined]

    def test_property_value_callback_optional(self, loop: asyncio.AbstractEventLoop) -> None:
        """``on_property_value_changed`` is optional — defaults to
        None for the common case where Voice Clarity toggle detection
        isn't needed."""
        listener = WindowsMMNotificationListener(
            loop=loop,
            on_default_capture_changed=AsyncMock(),
            on_device_state_changed=AsyncMock(),
        )
        assert listener._on_property_value_changed is None  # type: ignore[attr-defined]


# ── Cross-OS import stability ───────────────────────────────────────


class TestModuleImportsCleanlyOnAnyPlatform:
    """The module MUST import without error on Linux + macOS CI
    workers — comtypes must NEVER be imported at module top level."""

    def test_module_already_loaded_safe_to_reimport(self) -> None:
        """If we got this far the module imported. The fact that this
        test file imports
        ``from sovyx.voice._mm_notification_client import ...`` at
        module top level and pytest collected this test on the
        current platform proves the import is safe."""
        import sovyx.voice._mm_notification_client as mm_mod

        assert mm_mod is not None

    def test_no_top_level_comtypes_import_marker(self) -> None:
        """Sanity check that the source file does not contain a top-
        level ``import comtypes`` outside of ``register()`` body
        comments / docstrings."""
        from importlib import util as importlib_util

        spec = importlib_util.find_spec("sovyx.voice._mm_notification_client")
        assert spec is not None
        assert spec.origin is not None
        with open(spec.origin, encoding="utf-8") as f:
            source = f.read()
        # ``import comtypes`` should only appear in docstrings / inline
        # comments showing the v0.25.0 wire-up shape, NEVER as a real
        # top-level statement. We assert it's nowhere outside the
        # WindowsMMNotificationListener.register docstring.
        # Simple check: the literal "    import comtypes" indented at
        # 4 spaces (typical top-level scope) MUST NOT appear.
        for line in source.splitlines():
            stripped = line.lstrip()
            # Top-level imports start at column 0.
            if line.startswith("import comtypes") or line.startswith("from comtypes "):
                pytest.fail(
                    f"Top-level comtypes import detected in source: "
                    f"{line!r} — must be lazy inside register()."
                )
            # Inside docstrings / doctests, comtypes is fine — those
            # are stripped at parse time anyway.
            del stripped


# ── Type contract reminders ─────────────────────────────────────────


class TestProtocolStructure:
    """The MMNotificationListener Protocol must remain stable."""

    def test_protocol_has_register_and_unregister(self) -> None:
        # Get the Protocol's required methods. runtime_checkable
        # protocols carry the abstract method set on
        # ``__protocol_attrs__`` in Python 3.12+.
        # Fall back to the public surface contract.
        attrs = {"register", "unregister"}
        assert all(hasattr(NoopMMNotificationListener, a) for a in attrs)
        assert all(hasattr(WindowsMMNotificationListener, a) for a in attrs)

    def test_a_random_object_does_not_satisfy_protocol(self) -> None:
        """``isinstance(obj, MMNotificationListener)`` must not be a
        no-op — random duck-typed objects without register/unregister
        should fail the runtime_checkable check."""

        class NotAListener:
            pass

        assert not isinstance(NotAListener(), MMNotificationListener)


# ── Module-level Any reference removed; only used by Protocol typing ─


_ = Any  # silence unused import warnings without affecting the contract
_ = MagicMock  # likewise; reserved for v0.25.0 wire-up tests
