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
        time.sleep(0.01)
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
        from unittest.mock import MagicMock, AsyncMock

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
