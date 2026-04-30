"""Integration tests for the runtime listener wire-up in
:class:`~sovyx.voice.pipeline._orchestrator.VoicePipeline`.

Phase 1b of ``MISSION-voice-runtime-listener-wireup-2026-04-30.md``.
Pins the contract that the pipeline's ``start()`` registers the MM
notification + driver-update listeners (per their respective
``*_enabled`` flags), ``stop()`` unregisters them, and one
listener failing to register does NOT block the other.

Test strategy:

* Patch ``create_mm_notification_listener`` and
  ``build_driver_update_listener`` at the orchestrator module
  level (where the wire-up imports them) so we control what
  registration/unregistration looks like under each scenario.
* The patches return ``MagicMock``-backed listeners so we can
  assert on ``.register()`` / ``.unregister()`` call counts.
* Driver-update listener tests also verify the
  ``audio_driver_update_recascade_enabled`` flag is propagated
  to the handler via the listener's ``on_driver_changed``
  callback (the handler captures it at construction).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.pipeline import _orchestrator as module_under_test
from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._orchestrator import VoicePipeline


def _make_pipeline(
    *,
    mm_notification_listener_enabled: bool = False,
    audio_driver_update_listener_enabled: bool = False,
    audio_driver_update_recascade_enabled: bool = False,
) -> VoicePipeline:
    """Build a minimal pipeline with mock deps + optional flag overrides.

    All construction args except the listener flags are
    ``MagicMock`` / ``AsyncMock`` — tests focus on the listener
    wire-up, not on real audio plumbing. Explicit kwargs (instead
    of ``**flags``) so mypy strict can narrow each flag's type
    individually.
    """
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
        mm_notification_listener_enabled=mm_notification_listener_enabled,
        audio_driver_update_listener_enabled=audio_driver_update_listener_enabled,
        audio_driver_update_recascade_enabled=audio_driver_update_recascade_enabled,
    )


class TestListenerRegistrationFlagPlumbing:
    """The pipeline forwards the listener-enabled flags into the
    listener factory calls. Verifies the construction-time flag
    capture flows correctly through to factory invocation.
    """

    def test_mm_listener_factory_called_with_enabled_flag(self) -> None:
        """Pipeline.start() calls create_mm_notification_listener with
        the construction-time mm_notification_listener_enabled value.
        """
        pipeline = _make_pipeline(mm_notification_listener_enabled=True)

        mock_mm_listener = MagicMock(name="mm_listener")
        mock_driver_listener = MagicMock(name="driver_listener")
        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=mock_mm_listener,
            ) as mock_mm_factory,
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=mock_driver_listener,
            ),
        ):
            asyncio.run(pipeline.start())

        mock_mm_factory.assert_called_once()
        assert mock_mm_factory.call_args.kwargs["enabled"] is True

    def test_mm_listener_factory_called_with_disabled_flag(self) -> None:
        """Default (mm_notification_listener_enabled=False) plumbs to
        the factory's enabled=False — Noop listener returned.
        """
        pipeline = _make_pipeline(mm_notification_listener_enabled=False)

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=MagicMock(),
            ) as mock_mm_factory,
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=MagicMock(),
            ),
        ):
            asyncio.run(pipeline.start())

        mock_mm_factory.assert_called_once()
        assert mock_mm_factory.call_args.kwargs["enabled"] is False

    def test_driver_update_listener_factory_called_with_enabled_flag(self) -> None:
        pipeline = _make_pipeline(audio_driver_update_listener_enabled=True)

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=MagicMock(),
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=MagicMock(),
            ) as mock_driver_factory,
        ):
            asyncio.run(pipeline.start())

        mock_driver_factory.assert_called_once()
        assert mock_driver_factory.call_args.kwargs["enabled"] is True

    def test_driver_update_recascade_flag_flows_to_handler(self) -> None:
        """The ``audio_driver_update_recascade_enabled`` flag flows
        into the ``DriverUpdateHandler`` constructor — pinned by
        capturing the callback registered with the listener and
        introspecting the bound handler.
        """
        pipeline = _make_pipeline(
            audio_driver_update_listener_enabled=True,
            audio_driver_update_recascade_enabled=True,
        )

        captured_callback = None

        def _capture_callback(**kwargs: Any) -> Any:
            nonlocal captured_callback
            captured_callback = kwargs.get("on_driver_changed")
            return MagicMock()

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=MagicMock(),
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                side_effect=_capture_callback,
            ),
        ):
            asyncio.run(pipeline.start())

        # The captured callback is the bound method
        # ``handler.handle_driver_update``. The handler instance
        # lives on the bound method's ``__self__``. Verify the
        # recascade flag was propagated.
        assert captured_callback is not None
        handler = captured_callback.__self__
        assert handler.recascade_enabled is True


class TestRegisterAndUnregisterContract:
    """``start()`` calls register on each listener; ``stop()`` calls
    unregister on each. The list ``self._listeners`` mediates the
    teardown — only successful registrations get torn down.
    """

    def test_register_called_on_both_listeners(self) -> None:
        pipeline = _make_pipeline()
        mock_mm_listener = MagicMock(name="mm_listener")
        mock_driver_listener = MagicMock(name="driver_listener")
        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=mock_mm_listener,
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=mock_driver_listener,
            ),
        ):
            asyncio.run(pipeline.start())

        mock_mm_listener.register.assert_called_once()
        mock_driver_listener.register.assert_called_once()
        assert pipeline._listeners == [mock_mm_listener, mock_driver_listener]

    def test_unregister_called_on_stop(self) -> None:
        pipeline = _make_pipeline()
        mock_mm_listener = MagicMock(name="mm_listener")
        mock_driver_listener = MagicMock(name="driver_listener")

        async def _full_lifecycle() -> None:
            with (
                patch.object(
                    module_under_test,
                    "create_mm_notification_listener",
                    return_value=mock_mm_listener,
                ),
                patch.object(
                    module_under_test,
                    "build_driver_update_listener",
                    return_value=mock_driver_listener,
                ),
            ):
                await pipeline.start()
            await pipeline.stop()

        asyncio.run(_full_lifecycle())

        mock_mm_listener.unregister.assert_called_once()
        mock_driver_listener.unregister.assert_called_once()
        # The list is cleared after teardown so a subsequent start()
        # doesn't re-register against stale listener instances.
        assert pipeline._listeners == []

    def test_unregister_idempotent_when_not_started(self) -> None:
        """``stop()`` without a prior ``start()`` MUST be safe — no
        listeners registered means nothing to unregister.
        """
        pipeline = _make_pipeline()
        # Pipeline._running is False from construction — directly
        # call _unregister_listeners to verify it doesn't raise.
        pipeline._unregister_listeners()
        assert pipeline._listeners == []


class TestFailureIsolation:
    """Per the mission's failure-isolation contract, each listener
    registers in its own try/except — one failing doesn't block the
    other. Failed registrations are NOT appended to
    ``self._listeners`` so stop's teardown only sees successes.
    """

    def test_mm_listener_register_raise_does_not_block_driver_update(self) -> None:
        pipeline = _make_pipeline()

        # MM listener raises on register. Driver-update listener
        # registers normally.
        mock_mm_listener = MagicMock(name="mm_listener")
        mock_mm_listener.register.side_effect = OSError("E_ACCESSDENIED")
        mock_driver_listener = MagicMock(name="driver_listener")

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=mock_mm_listener,
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=mock_driver_listener,
            ),
        ):
            asyncio.run(pipeline.start())

        # Driver-update listener still registered.
        mock_driver_listener.register.assert_called_once()
        # ``self._listeners`` contains ONLY the driver-update
        # listener — the failed MM listener is NOT appended.
        assert pipeline._listeners == [mock_driver_listener]

    def test_driver_update_register_raise_does_not_block_mm_listener(self) -> None:
        pipeline = _make_pipeline()

        mock_mm_listener = MagicMock(name="mm_listener")
        mock_driver_listener = MagicMock(name="driver_listener")
        mock_driver_listener.register.side_effect = OSError("WMI service down")

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=mock_mm_listener,
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=mock_driver_listener,
            ),
        ):
            asyncio.run(pipeline.start())

        mock_mm_listener.register.assert_called_once()
        assert pipeline._listeners == [mock_mm_listener]

    def test_both_listeners_failing_does_not_block_pipeline_start(self) -> None:
        """Pipeline still transitions to running even if BOTH
        listeners fail to register. Voice pipeline works without
        device-change awareness — degraded but functional.
        """
        pipeline = _make_pipeline()

        mock_mm_listener = MagicMock(name="mm_listener")
        mock_mm_listener.register.side_effect = OSError("MM failed")
        mock_driver_listener = MagicMock(name="driver_listener")
        mock_driver_listener.register.side_effect = OSError("driver failed")

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=mock_mm_listener,
            ),
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=mock_driver_listener,
            ),
        ):
            asyncio.run(pipeline.start())

        # Pipeline IS running (start() didn't raise), but no
        # listeners are tracked.
        assert pipeline._running is True
        assert pipeline._listeners == []

    def test_unregister_failure_does_not_block_other_unregister(self) -> None:
        pipeline = _make_pipeline()

        mock_mm_listener = MagicMock(name="mm_listener")
        mock_mm_listener.unregister.side_effect = OSError("wedged WMI")
        mock_driver_listener = MagicMock(name="driver_listener")

        async def _full_lifecycle() -> None:
            with (
                patch.object(
                    module_under_test,
                    "create_mm_notification_listener",
                    return_value=mock_mm_listener,
                ),
                patch.object(
                    module_under_test,
                    "build_driver_update_listener",
                    return_value=mock_driver_listener,
                ),
            ):
                await pipeline.start()
            await pipeline.stop()

        asyncio.run(_full_lifecycle())

        # Both unregisters were attempted even though the first
        # raised.
        mock_mm_listener.unregister.assert_called_once()
        mock_driver_listener.unregister.assert_called_once()


class TestStartIdempotency:
    """``start()`` is idempotent — a second call is a no-op + does
    NOT spawn a second pair of listeners.
    """

    def test_double_start_does_not_re_register(self) -> None:
        pipeline = _make_pipeline()

        with (
            patch.object(
                module_under_test,
                "create_mm_notification_listener",
                return_value=MagicMock(),
            ) as mock_mm_factory,
            patch.object(
                module_under_test,
                "build_driver_update_listener",
                return_value=MagicMock(),
            ) as mock_driver_factory,
        ):

            async def _double_start() -> None:
                await pipeline.start()
                await pipeline.start()  # no-op per the early-return guard

            asyncio.run(_double_start())

        # Each factory called exactly once across both start()
        # invocations.
        assert mock_mm_factory.call_count == 1
        assert mock_driver_factory.call_count == 1


class TestRestartAfterStop:
    """A start → stop → start cycle re-registers fresh listeners on
    the second start, NOT the originals from before stop.
    """

    def test_listeners_re_registered_after_restart(self) -> None:
        pipeline = _make_pipeline()

        # First-cycle listeners.
        mm_first = MagicMock(name="mm_first")
        driver_first = MagicMock(name="driver_first")
        # Second-cycle listeners.
        mm_second = MagicMock(name="mm_second")
        driver_second = MagicMock(name="driver_second")

        async def _restart_cycle() -> None:
            with (
                patch.object(
                    module_under_test,
                    "create_mm_notification_listener",
                    return_value=mm_first,
                ),
                patch.object(
                    module_under_test,
                    "build_driver_update_listener",
                    return_value=driver_first,
                ),
            ):
                await pipeline.start()
            await pipeline.stop()

            with (
                patch.object(
                    module_under_test,
                    "create_mm_notification_listener",
                    return_value=mm_second,
                ),
                patch.object(
                    module_under_test,
                    "build_driver_update_listener",
                    return_value=driver_second,
                ),
            ):
                await pipeline.start()

        asyncio.run(_restart_cycle())

        # Second-cycle listeners are the ones held by the pipeline.
        # First-cycle listeners were unregistered + dropped from the
        # list during stop().
        assert pipeline._listeners == [mm_second, driver_second]
        mm_first.unregister.assert_called_once()
        driver_first.unregister.assert_called_once()
        mm_second.register.assert_called_once()
        driver_second.register.assert_called_once()


class TestListenerCallbackEvents:
    """The MM listener's 2 in-scope callbacks
    (``on_default_capture_changed`` + ``on_device_state_changed``)
    emit structured events. Per mission Part 4.2 they do NOT yet
    wire into the capture-task restart triggers — that's a separate
    follow-up commit. Test scope here is just the structured-event
    emission contract.
    """

    @pytest.mark.asyncio
    async def test_default_capture_changed_emits_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pipeline = _make_pipeline()
        with caplog.at_level("INFO"):
            await pipeline._on_default_capture_changed("test-device-guid")
        assert any("voice.default_capture_changed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_device_state_changed_emits_log(self, caplog: pytest.LogCaptureFixture) -> None:
        pipeline = _make_pipeline()
        with caplog.at_level("INFO"):
            await pipeline._on_device_state_changed("test-device-guid", 0x1)
        records = [r for r in caplog.records if "voice.device_state_changed" in r.message]
        assert records
        # State was hex-encoded for log readability.
        assert "0x1" in records[0].message
