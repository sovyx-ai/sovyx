"""Sovyx Plugin Lifecycle — State machine and event emission.

Formal state machine: DISCOVERED → LOADING → ACTIVE → UNLOADING → UNLOADED
with ERROR as a fallback state from LOADING.

Events are emitted on state transitions via the engine EventBus (if available).

Resource probes: :class:`MemoryProbe` is a tiny dataclass capturing
process RSS + a perf-counter timestamp at construction. Pair it with
:func:`emit_plugin_loaded` / :func:`emit_plugin_unloaded` to compute
``mem_delta_bytes`` + ``import_duration_ms`` deltas without scattering
``psutil`` calls across the manager. ``psutil`` is optional — if it
isn't installed, the probe records ``rss_bytes=None`` and the emit
helpers leave ``mem_delta_bytes`` as ``None`` while still publishing
the timing + tool-count fields. A one-time WARNING
(``plugin.lifecycle.psutil_missing``) flags the dependency gap. Phase
6 Task 6.1 will pin ``psutil`` and the fallback becomes dead code.

Spec: SPE-008 §10 (Plugin Lifecycle State Machine)
"""

from __future__ import annotations

import dataclasses
import enum
import time
import typing

from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn

if typing.TYPE_CHECKING:  # pragma: no cover
    from sovyx.engine.events import EventBus

logger = get_logger(__name__)


# ── Memory + timing probe ───────────────────────────────────────────

# Module-level guard so the psutil-missing warning fires exactly once
# per process, not once per plugin load.
_PSUTIL_WARNED: bool = False


def _capture_rss_bytes() -> int | None:
    """Return the current process RSS in bytes, or ``None`` if psutil missing.

    Lives at module scope so the import cost is paid once at first
    call. ``psutil`` is an optional dependency until Phase 6 wires it
    in formally; until then a missing import logs
    ``plugin.lifecycle.psutil_missing`` once and degrades to ``None``.
    """
    global _PSUTIL_WARNED  # noqa: PLW0603 — single-process latch.
    try:
        import psutil  # noqa: PLC0415 — optional dep, deferred to first call.
    except ImportError:
        if not _PSUTIL_WARNED:
            _PSUTIL_WARNED = True
            logger.warning(
                "plugin.lifecycle.psutil_missing",
                detail="psutil unavailable; mem_delta_bytes will be null",
            )
        return None
    try:
        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001
        return None


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryProbe:
    """Snapshot of process RSS + perf-counter timestamp at construction.

    Returned by :func:`probe_now`. Pass to :func:`emit_plugin_loaded`
    or :func:`emit_plugin_unloaded` to publish the lifecycle event
    with ``mem_delta_bytes`` (vs the caller's later snapshot) and
    ``import_duration_ms`` (vs ``time.perf_counter()``).
    """

    rss_bytes: int | None
    started_at: float


def probe_now() -> MemoryProbe:
    """Return a fresh :class:`MemoryProbe` for the current process.

    Use as a paired call around a load/unload operation::

        before = probe_now()
        await plugin.setup(ctx)
        emit_plugin_loaded(plugin_name, before, tool_count=len(tools), ...)
    """
    return MemoryProbe(rss_bytes=_capture_rss_bytes(), started_at=time.perf_counter())


def _delta_bytes(before: MemoryProbe, after_rss: int | None) -> int | None:
    """Return ``after_rss - before.rss_bytes`` or ``None`` when either is missing."""
    if before.rss_bytes is None or after_rss is None:
        return None
    return after_rss - before.rss_bytes


def emit_plugin_loaded(
    plugin_name: str,
    before: MemoryProbe,
    *,
    plugin_version: str,
    tool_count: int,
) -> None:
    """Emit ``plugin.lifecycle.loaded`` enriched with memory + timing.

    Computes ``mem_delta_bytes`` (post-load RSS minus the probe's
    captured value, or ``None`` if psutil is missing) and
    ``import_duration_ms`` (perf-counter delta in milliseconds). Both
    fields land in a structured log so the dashboard can graph plugin
    load cost without instrumenting every loader site individually.
    """
    after_rss = _capture_rss_bytes()
    duration_ms = int((time.perf_counter() - before.started_at) * 1000.0)
    logger.info(
        "plugin.lifecycle.loaded",
        **{
            "plugin_id": plugin_name,
            "plugin.version": plugin_version,
            "plugin.tool_count": tool_count,
            "plugin.import_duration_ms": duration_ms,
            "plugin.mem_delta_bytes": _delta_bytes(before, after_rss),
        },
    )


def emit_plugin_unloaded(
    plugin_name: str,
    before: MemoryProbe,
    *,
    reason: str,
) -> None:
    """Emit ``plugin.lifecycle.unloaded`` enriched with memory + timing.

    ``mem_delta_bytes`` is typically negative for clean teardowns —
    the field doubles as a memory-leak signal: a plugin that fails to
    release references will show a delta near zero (or positive) at
    unload time, visible in the lifecycle log without any extra
    tooling.
    """
    after_rss = _capture_rss_bytes()
    duration_ms = int((time.perf_counter() - before.started_at) * 1000.0)
    logger.info(
        "plugin.lifecycle.unloaded",
        **{
            "plugin_id": plugin_name,
            "plugin.unload_reason": reason,
            "plugin.unload_duration_ms": duration_ms,
            "plugin.mem_delta_bytes": _delta_bytes(before, after_rss),
        },
    )


# ── Plugin State ────────────────────────────────────────────────────


class PluginState(enum.StrEnum):
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
                asyncio.get_running_loop()
                spawn(self._event_bus.emit(event), name="plugin-state-changed-emit")
            except RuntimeError:
                pass  # No event loop, skip emit
        except ImportError:  # pragma: no cover
            pass  # Events module not available


# ── Exceptions ──────────────────────────────────────────────────────


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
