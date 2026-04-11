"""Sovyx Plugin Lifecycle — State machine and event emission.

Formal state machine: DISCOVERED → LOADING → ACTIVE → UNLOADING → UNLOADED
with ERROR as a fallback state from LOADING.

Events are emitted on state transitions via the engine EventBus (if available).

Spec: SPE-008 §10 (Plugin Lifecycle State Machine)
"""

from __future__ import annotations

import enum
import time
import typing

from sovyx.observability.logging import get_logger

if typing.TYPE_CHECKING:  # pragma: no cover
    from sovyx.engine.events import EventBus

logger = get_logger(__name__)


# ── Plugin State ────────────────────────────────────────────────────


class PluginState(enum.Enum):
    """Plugin lifecycle states.

    State machine:
        DISCOVERED → LOADING → ACTIVE → UNLOADING → UNLOADED
                        ↓
                      ERROR → DISCOVERED (retry)
    """

    DISCOVERED = "discovered"
    LOADING = "loading"
    ACTIVE = "active"
    UNLOADING = "unloading"
    UNLOADED = "unloaded"
    ERROR = "error"


# Valid transitions: from_state → set of allowed to_states
_VALID_TRANSITIONS: dict[PluginState, set[PluginState]] = {
    PluginState.DISCOVERED: {PluginState.LOADING},
    PluginState.LOADING: {PluginState.ACTIVE, PluginState.ERROR},
    PluginState.ACTIVE: {PluginState.UNLOADING},
    PluginState.UNLOADING: {PluginState.UNLOADED},
    PluginState.UNLOADED: set(),
    PluginState.ERROR: {PluginState.DISCOVERED},
}


# ── State Tracker ───────────────────────────────────────────────────


class PluginStateTracker:
    """Tracks and enforces plugin state transitions.

    Each plugin gets its own tracker instance. State changes are
    validated against the transition table, logged, and optionally
    emitted as events.

    Usage::

        tracker = PluginStateTracker("weather")
        tracker.transition(PluginState.LOADING)
        tracker.transition(PluginState.ACTIVE)
        assert tracker.state == PluginState.ACTIVE

    Spec: SPE-008 §10
    """

    def __init__(
        self,
        plugin_name: str,
        event_bus: EventBus | None = None,
    ) -> None:
        self._plugin = plugin_name
        self._event_bus = event_bus
        self._state = PluginState.DISCOVERED
        self._history: list[tuple[PluginState, float]] = [
            (PluginState.DISCOVERED, time.monotonic())
        ]
        self._error_message: str | None = None

    @property
    def state(self) -> PluginState:
        """Current plugin state."""
        return self._state

    @property
    def error_message(self) -> str | None:
        """Error message if state is ERROR."""
        return self._error_message

    @property
    def history(self) -> list[tuple[PluginState, float]]:
        """State transition history (state, timestamp)."""
        return list(self._history)

    @property
    def uptime_seconds(self) -> float:
        """Seconds since last transition to ACTIVE, or 0 if not active."""
        if self._state != PluginState.ACTIVE:
            return 0.0
        # Find last ACTIVE entry
        for state, ts in reversed(self._history):
            if state == PluginState.ACTIVE:
                return time.monotonic() - ts
        return 0.0  # pragma: no cover — unreachable when state == ACTIVE

    def transition(
        self,
        to_state: PluginState,
        *,
        error: str | None = None,
    ) -> None:
        """Transition to a new state.

        Args:
            to_state: Target state.
            error: Error message (required when transitioning to ERROR).

        Raises:
            InvalidTransitionError: Transition not allowed.
        """
        allowed = _VALID_TRANSITIONS.get(self._state, set())
        if to_state not in allowed:
            msg = (
                f"Invalid state transition for plugin '{self._plugin}': "
                f"{self._state.value} → {to_state.value}"
            )
            raise InvalidTransitionError(msg)

        old_state = self._state
        self._state = to_state
        self._history.append((to_state, time.monotonic()))

        if to_state == PluginState.ERROR:
            self._error_message = error or "Unknown error"
        else:
            self._error_message = None

        logger.info(
            "plugin_state_transition",
            plugin=self._plugin,
            from_state=old_state.value,
            to_state=to_state.value,
            error=error,
        )

        # Emit event if bus available
        self._emit_event(old_state, to_state, error)

    def reset_to_discovered(self) -> None:
        """Reset from ERROR back to DISCOVERED for retry.

        Raises:
            InvalidTransitionError: Not in ERROR state.
        """
        self.transition(PluginState.DISCOVERED)

    def _emit_event(
        self,
        from_state: PluginState,
        to_state: PluginState,
        error: str | None,
    ) -> None:
        """Emit lifecycle event on the EventBus."""
        if not self._event_bus:
            return

        try:
            from sovyx.plugins.events import PluginStateChanged

            event = PluginStateChanged(
                plugin_name=self._plugin,
                from_state=from_state.value,
                to_state=to_state.value,
                error_message=error or "",
            )

            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_bus.emit(event))
            except RuntimeError:
                pass  # No event loop, skip emit
        except ImportError:  # pragma: no cover
            pass  # Events module not available


# ── Exceptions ──────────────────────────────────────────────────────


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
