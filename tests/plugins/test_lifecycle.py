"""Tests for Sovyx Plugin Lifecycle — state machine and events.

Coverage target: ≥95% on plugins/lifecycle.py
"""

from __future__ import annotations

import time

import pytest

from sovyx.plugins.lifecycle import (
    InvalidTransitionError,
    PluginState,
    PluginStateTracker,
)

# ── PluginState ─────────────────────────────────────────────────────


class TestPluginState:
    """Tests for PluginState enum."""

    def test_all_states(self) -> None:
        assert len(PluginState) == 6

    def test_state_values(self) -> None:
        assert PluginState.DISCOVERED.value == "discovered"
        assert PluginState.LOADING.value == "loading"
        assert PluginState.ACTIVE.value == "active"
        assert PluginState.UNLOADING.value == "unloading"
        assert PluginState.UNLOADED.value == "unloaded"
        assert PluginState.ERROR.value == "error"


# ── State Transitions ───────────────────────────────────────────────


class TestStateTransitions:
    """Tests for valid state transitions."""

    def test_initial_state(self) -> None:
        tracker = PluginStateTracker("test")
        assert tracker.state == PluginState.DISCOVERED

    def test_happy_path(self) -> None:
        """DISCOVERED → LOADING → ACTIVE → UNLOADING → UNLOADED."""
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        assert tracker.state == PluginState.LOADING
        tracker.transition(PluginState.ACTIVE)
        assert tracker.state == PluginState.ACTIVE
        tracker.transition(PluginState.UNLOADING)
        assert tracker.state == PluginState.UNLOADING
        tracker.transition(PluginState.UNLOADED)
        assert tracker.state == PluginState.UNLOADED

    def test_error_path(self) -> None:
        """DISCOVERED → LOADING → ERROR."""
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ERROR, error="setup failed")
        assert tracker.state == PluginState.ERROR
        assert tracker.error_message == "setup failed"

    def test_error_default_message(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ERROR)
        assert tracker.error_message == "Unknown error"

    def test_error_to_discovered_retry(self) -> None:
        """ERROR → DISCOVERED (retry)."""
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ERROR, error="oops")
        tracker.reset_to_discovered()
        assert tracker.state == PluginState.DISCOVERED
        assert tracker.error_message is None

    def test_error_cleared_on_non_error_transition(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ERROR, error="fail")
        assert tracker.error_message == "fail"
        tracker.transition(PluginState.DISCOVERED)
        assert tracker.error_message is None


# ── Invalid Transitions ─────────────────────────────────────────────


class TestInvalidTransitions:
    """Tests for rejected state transitions."""

    def test_discovered_to_active(self) -> None:
        tracker = PluginStateTracker("test")
        with pytest.raises(InvalidTransitionError, match="Invalid"):
            tracker.transition(PluginState.ACTIVE)

    def test_active_to_loading(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        with pytest.raises(InvalidTransitionError):
            tracker.transition(PluginState.LOADING)

    def test_unloaded_to_anything(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        tracker.transition(PluginState.UNLOADING)
        tracker.transition(PluginState.UNLOADED)
        with pytest.raises(InvalidTransitionError):
            tracker.transition(PluginState.DISCOVERED)

    def test_reset_from_non_error(self) -> None:
        tracker = PluginStateTracker("test")
        with pytest.raises(InvalidTransitionError):
            tracker.reset_to_discovered()


# ── History ─────────────────────────────────────────────────────────


class TestHistory:
    """Tests for state transition history."""

    def test_initial_history(self) -> None:
        tracker = PluginStateTracker("test")
        history = tracker.history
        assert len(history) == 1
        assert history[0][0] == PluginState.DISCOVERED

    def test_history_grows(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        assert len(tracker.history) == 3

    def test_history_is_copy(self) -> None:
        tracker = PluginStateTracker("test")
        h1 = tracker.history
        tracker.transition(PluginState.LOADING)
        h2 = tracker.history
        assert len(h1) == 1
        assert len(h2) == 2


# ── Uptime ──────────────────────────────────────────────────────────


class TestUptime:
    """Tests for uptime_seconds."""

    def test_not_active(self) -> None:
        tracker = PluginStateTracker("test")
        assert tracker.uptime_seconds == 0.0

    def test_active_uptime(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        # 50 ms > Windows' default monotonic-clock resolution (~15.6 ms
        # without ``timeBeginPeriod``). A 10 ms sleep can round down to
        # a zero-tick delta on Windows, making ``uptime_seconds == 0.0``
        # and failing the ``> 0`` assertion.
        time.sleep(0.05)
        assert tracker.uptime_seconds > 0

    def test_uptime_resets_on_unloading(self) -> None:
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        tracker.transition(PluginState.UNLOADING)
        assert tracker.uptime_seconds == 0.0


# ── Event Emission ──────────────────────────────────────────────────


class TestEventEmission:
    """Tests for event bus integration."""

    @pytest.mark.anyio()
    async def test_emit_with_bus(self) -> None:
        """Events emitted on transitions when bus provided."""
        from unittest.mock import AsyncMock, MagicMock

        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()

        tracker = PluginStateTracker("test", event_bus=mock_bus)
        tracker.transition(PluginState.LOADING)
        # Event emitted via create_task in running loop
        # Just verify no exception

    def test_emit_without_bus(self) -> None:
        """No error when event_bus is None."""
        tracker = PluginStateTracker("test")
        tracker.transition(PluginState.LOADING)  # No error

    def test_emit_no_event_loop(self) -> None:
        """No error when no event loop running."""
        from unittest.mock import MagicMock

        mock_bus = MagicMock()
        tracker = PluginStateTracker("test", event_bus=mock_bus)
        tracker.transition(PluginState.LOADING)  # No error


# ── Event Dataclass Tests (TASK-435) ───────────────────────────────


class TestPluginEventDataclasses:
    """Tests for PluginLoaded and PluginUnloaded events."""

    def test_plugin_loaded_fields(self) -> None:
        from sovyx.engine.events import EventCategory
        from sovyx.plugins.events import PluginLoaded

        evt = PluginLoaded(plugin_name="weather", plugin_version="1.0.0", tools_count=3)
        assert evt.plugin_name == "weather"
        assert evt.plugin_version == "1.0.0"
        assert evt.tools_count == 3
        assert evt.category == EventCategory.PLUGIN
        assert evt.event_id  # auto-generated

    def test_plugin_loaded_defaults(self) -> None:
        from sovyx.plugins.events import PluginLoaded

        evt = PluginLoaded()
        assert evt.plugin_name == ""
        assert evt.plugin_version == ""
        assert evt.tools_count == 0

    def test_plugin_unloaded_fields(self) -> None:
        from sovyx.engine.events import EventCategory
        from sovyx.plugins.events import PluginUnloaded

        evt = PluginUnloaded(plugin_name="timer", reason="shutdown")
        assert evt.plugin_name == "timer"
        assert evt.reason == "shutdown"
        assert evt.category == EventCategory.PLUGIN

    def test_plugin_unloaded_defaults(self) -> None:
        from sovyx.plugins.events import PluginUnloaded

        evt = PluginUnloaded()
        assert evt.plugin_name == ""
        assert evt.reason == ""

    def test_plugin_auto_disabled_fields(self) -> None:
        from sovyx.engine.events import EventCategory
        from sovyx.plugins.events import PluginAutoDisabled

        evt = PluginAutoDisabled(plugin_name="bad", consecutive_failures=5, last_error="boom")
        assert evt.plugin_name == "bad"
        assert evt.consecutive_failures == 5
        assert evt.last_error == "boom"
        assert evt.category == EventCategory.PLUGIN

    def test_all_events_are_frozen(self) -> None:
        """All plugin events are immutable."""
        from sovyx.plugins.events import (
            PluginAutoDisabled,
            PluginLoaded,
            PluginStateChanged,
            PluginToolExecuted,
            PluginUnloaded,
        )

        for cls in [
            PluginLoaded,
            PluginUnloaded,
            PluginToolExecuted,
            PluginAutoDisabled,
            PluginStateChanged,
        ]:
            evt = cls()
            with pytest.raises(AttributeError):
                evt.plugin_name = "mutated"  # type: ignore[misc]
