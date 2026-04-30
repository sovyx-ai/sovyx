"""Tests for the audio driver-update handler.

Phase 1a of ``MISSION-voice-runtime-listener-wireup-2026-04-30.md``.
Pins the contract for
:class:`~sovyx.voice.health._driver_update_handler.DriverUpdateHandler`:

* The detection log + ``action=detected`` counter ALWAYS fire,
  regardless of the recascade flag.
* When ``recascade_enabled=False`` (lenient): a DEBUG skipped log
  fires + ``action=skipped`` counter; no would-trigger emission.
* When ``recascade_enabled=True``: a WARN would-trigger log fires
  + ``action=triggered`` counter.
* The handler is stateless beyond construction-time flag capture
  (concurrent ``handle_driver_update`` calls don't interfere).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from unittest.mock import patch

import pytest

from sovyx.voice.health import _driver_update_handler as module_under_test
from sovyx.voice.health._driver_update_handler import DriverUpdateHandler
from sovyx.voice.health._driver_update_listener_win import DriverUpdateEvent


def _make_event(
    *,
    device_id: str = r"USB\VID_046D&PID_0A45\AB12CD34",
    friendly_name: str = "Logitech BRIO",
    new_driver_version: str = "1.2.3.4",
) -> DriverUpdateEvent:
    return DriverUpdateEvent(
        device_id=device_id,
        friendly_name=friendly_name,
        new_driver_version=new_driver_version,
        detected_at=_dt.datetime.now(_dt.UTC),
    )


# ── Construction-time flag capture ──────────────────────────────────


class TestConstructionTimeFlagCapture:
    """The handler captures the flag at construction. Mid-session
    flag flips don't take effect until the pipeline restarts (which
    rebuilds the handler with the new flag value). Tests pin this
    contract so a future "live re-read" refactor that breaks the
    semantic is caught.
    """

    def test_disabled_handler_property_reflects_flag(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=False)
        assert handler.recascade_enabled is False

    def test_enabled_handler_property_reflects_flag(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=True)
        assert handler.recascade_enabled is True


# ── Always-on detection log ─────────────────────────────────────────


class TestAlwaysOnDetectionLog:
    """Detection log + counter fire regardless of recascade flag."""

    @pytest.mark.parametrize("recascade_enabled", [False, True])
    def test_detected_log_fires(
        self,
        recascade_enabled: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``voice.driver_update.detected`` fires under both flag
        states — operator-visible signal regardless of action.
        """
        handler = DriverUpdateHandler(recascade_enabled=recascade_enabled)
        event = _make_event()
        with caplog.at_level("INFO"):
            asyncio.run(handler.handle_driver_update(event))
        assert any("voice.driver_update.detected" in r.message for r in caplog.records)

    @pytest.mark.parametrize("recascade_enabled", [False, True])
    def test_detected_log_carries_event_fields(
        self,
        recascade_enabled: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handler = DriverUpdateHandler(recascade_enabled=recascade_enabled)
        event = _make_event(
            device_id=r"USB\VID_1532&PID_0528",
            friendly_name="Razer Mic",
            new_driver_version="9.9.9.9",
        )
        with caplog.at_level("INFO"):
            asyncio.run(handler.handle_driver_update(event))
        detected_records = [
            r for r in caplog.records if "voice.driver_update.detected" in r.message
        ]
        assert detected_records, "detected log did not fire"
        # The caplog renders structlog kwargs through Python repr()
        # which double-escapes backslashes (``\\`` → ``\\\\``).
        # Assert against the repr form for the device_id and the
        # raw form for the other fields (which have no special
        # characters).
        rendered = detected_records[0].message
        assert "USB\\\\VID_1532&PID_0528" in rendered
        assert "Razer Mic" in rendered
        assert "9.9.9.9" in rendered

    @pytest.mark.parametrize("recascade_enabled", [False, True])
    def test_detected_counter_fires(
        self,
        recascade_enabled: bool,
    ) -> None:
        """``record_driver_update_detected(action="detected")`` is
        called under both flag states.
        """
        handler = DriverUpdateHandler(recascade_enabled=recascade_enabled)
        event = _make_event()
        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(handler.handle_driver_update(event))
        # First call MUST always be action=detected. Subsequent
        # calls (skipped or triggered) are tested separately below.
        assert mock_record.call_args_list, "no record_driver_update_detected call fired"
        assert mock_record.call_args_list[0].kwargs == {"action": "detected"}


# ── Lenient mode — recascade_enabled=False ──────────────────────────


class TestLenientMode:
    """When the flag is False (default), the handler logs the skip
    + records ``action=skipped``; no would-trigger emission.
    """

    def test_skipped_log_fires(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()
        with caplog.at_level("DEBUG"):
            asyncio.run(handler.handle_driver_update(event))
        assert any("voice.driver_update.recascade_skipped" in r.message for r in caplog.records)

    def test_skipped_log_records_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()
        with caplog.at_level("DEBUG"):
            asyncio.run(handler.handle_driver_update(event))
        skipped = [r for r in caplog.records if "recascade_skipped" in r.message]
        assert skipped
        assert "flag_disabled" in skipped[0].message

    def test_would_trigger_log_does_not_fire(self, caplog: pytest.LogCaptureFixture) -> None:
        """Critical: the lenient path MUST NOT emit
        ``recascade_would_trigger`` — that's reserved for the
        ``recascade_enabled=True`` path. Flipping this contract
        would surface false-positive "I would have re-cascaded"
        events in operator dashboards.
        """
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()
        with caplog.at_level("DEBUG"):
            asyncio.run(handler.handle_driver_update(event))
        assert not any("recascade_would_trigger" in r.message for r in caplog.records)

    def test_skipped_counter_fires(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()
        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(handler.handle_driver_update(event))
        actions = [call.kwargs.get("action") for call in mock_record.call_args_list]
        assert actions == ["detected", "skipped"]

    def test_triggered_counter_does_not_fire(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()
        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(handler.handle_driver_update(event))
        actions = [call.kwargs.get("action") for call in mock_record.call_args_list]
        assert "triggered" not in actions


# ── Active mode — recascade_enabled=True ────────────────────────────


class TestActiveMode:
    """When the flag is True, the handler emits would-trigger +
    records ``action=triggered``. Actual cascade re-run plumbing
    is OUT OF SCOPE here — that's the Part 4.1 deferral.
    """

    def test_would_trigger_log_fires(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = DriverUpdateHandler(recascade_enabled=True)
        event = _make_event()
        with caplog.at_level("WARNING"):
            asyncio.run(handler.handle_driver_update(event))
        assert any(
            "voice.driver_update.recascade_would_trigger" in r.message for r in caplog.records
        )

    def test_would_trigger_log_carries_device_and_version(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = DriverUpdateHandler(recascade_enabled=True)
        event = _make_event(
            device_id="device-xyz",
            new_driver_version="2.0.0.0",
        )
        with caplog.at_level("WARNING"):
            asyncio.run(handler.handle_driver_update(event))
        triggered = [r for r in caplog.records if "recascade_would_trigger" in r.message]
        assert triggered
        assert "device-xyz" in triggered[0].message
        assert "2.0.0.0" in triggered[0].message

    def test_skipped_log_does_not_fire(self, caplog: pytest.LogCaptureFixture) -> None:
        """The active path MUST NOT emit ``recascade_skipped`` —
        that's reserved for the lenient path."""
        handler = DriverUpdateHandler(recascade_enabled=True)
        event = _make_event()
        with caplog.at_level("DEBUG"):
            asyncio.run(handler.handle_driver_update(event))
        assert not any("recascade_skipped" in r.message for r in caplog.records)

    def test_triggered_counter_fires(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=True)
        event = _make_event()
        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(handler.handle_driver_update(event))
        actions = [call.kwargs.get("action") for call in mock_record.call_args_list]
        assert actions == ["detected", "triggered"]

    def test_skipped_counter_does_not_fire(self) -> None:
        handler = DriverUpdateHandler(recascade_enabled=True)
        event = _make_event()
        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(handler.handle_driver_update(event))
        actions = [call.kwargs.get("action") for call in mock_record.call_args_list]
        assert "skipped" not in actions


# ── Concurrency safety (stateless invariant) ────────────────────────


class TestConcurrencySafety:
    """The handler holds no mutable state beyond construction-time
    ``_recascade_enabled``. Concurrent invocations don't interfere.
    """

    def test_multiple_concurrent_invocations_consistent(self) -> None:
        """3 concurrent ``handle_driver_update`` calls each emit
        their own pair of counter records (detected + skipped)
        without losing or duplicating any. Pins the stateless-
        handler invariant — a future refactor that introduced
        shared mutable state would race here.
        """
        handler = DriverUpdateHandler(recascade_enabled=False)
        event = _make_event()

        async def _invoke_three() -> None:
            await asyncio.gather(
                handler.handle_driver_update(event),
                handler.handle_driver_update(event),
                handler.handle_driver_update(event),
            )

        with patch.object(
            module_under_test,
            "record_driver_update_detected",
        ) as mock_record:
            asyncio.run(_invoke_three())

        # Each invocation emits 2 counter records (detected +
        # skipped) → 3 invocations × 2 = 6 total records. The
        # action sequence must alternate detected/skipped 3×.
        actions = [call.kwargs.get("action") for call in mock_record.call_args_list]
        assert actions.count("detected") == 3  # noqa: PLR2004
        assert actions.count("skipped") == 3  # noqa: PLR2004
        assert "triggered" not in actions


# ── Counter coercion ────────────────────────────────────────────────


class TestRecordDriverUpdateDetectedCoercion:
    """The metric's ``action`` label is coerced to ``unknown`` for
    out-of-range values to keep cardinality bounded. Pin the
    contract here so a future caller passing a typo'd action gets
    a fixed-cardinality fallback instead of bloating the metric.
    """

    def test_unknown_action_coerced_to_unknown(self) -> None:
        from sovyx.voice.health._metrics import (
            _DRIVER_UPDATE_ACTIONS,
            record_driver_update_detected,
        )

        # Sanity: the canonical actions are exactly the 3 we use.
        expected_actions = frozenset({"detected", "skipped", "triggered"})
        assert expected_actions == _DRIVER_UPDATE_ACTIONS

        # Calling with an out-of-range action MUST NOT raise — the
        # metric layer is best-effort and the coercion preserves
        # forward compatibility.
        with patch("sovyx.voice.health._metrics.get_metrics") as mock_get_metrics:
            mock_counter = mock_get_metrics.return_value
            mock_counter.voice_driver_update_detected = _MockCounter()
            record_driver_update_detected(action="rogue-value")
            # The mock counter saw a coerced ``action="unknown"``.
            assert mock_counter.voice_driver_update_detected.last_attributes == {
                "action": "unknown"
            }


class _MockCounter:
    """Minimal stand-in for an OTel counter that records the last
    ``add`` call's attributes. Lets the coercion test pin the
    label that actually flowed through to the metric layer.
    """

    def __init__(self) -> None:
        self.last_attributes: dict[str, str] | None = None

    def add(self, value: int, attributes: dict[str, str] | None = None) -> None:  # noqa: ARG002 — counter contract uses value but the test only inspects attributes
        self.last_attributes = attributes
