"""L4 — runtime resilience orchestrator.

ADR §4.4 sub-components that land this sprint:

* **§4.4.1 Exponential-backoff re-probe.** After
  :meth:`VoiceCaptureWatchdog.report_deafness` the watchdog schedules a
  chain of warm re-probes at ``+10 s, +30 s, +90 s`` (max 3). The
  endpoint returns to :attr:`~sovyx.voice.health.contract.WatchdogState.IDLE`
  on the first HEALTHY re-probe. On exhaustion it transitions to
  :attr:`~sovyx.voice.health.contract.WatchdogState.DEGRADED` and emits
  ``voice_capture_permanently_degraded`` so the pipeline can fall back
  to push-to-talk.

* **§4.4.2 Hot-plug listener.** The watchdog owns a
  :class:`~sovyx.voice.health._hotplug.HotplugListener` (platform
  selected via :func:`build_platform_hotplug_listener`). On remove of
  the *active* endpoint: invalidate its :class:`ComboStore` entry,
  emit ``voice_active_endpoint_removed`` and schedule a full re-cascade.
  On add: no-op unless the endpoint is currently ``DEGRADED``, in
  which case a re-cascade tries the freshly attached device.

The watchdog shares the cascade's lifecycle lock (ADR §5.5) so
hot-plug-driven re-cascades cannot race with in-flight :func:`run_cascade`
calls. Callers inject the same :class:`~sovyx.engine._lock_dict.LRULockDict`
that :func:`~sovyx.voice.health.cascade.run_cascade` uses to guarantee
serialisation across both code paths.

Subsequent Sprint 2 tasks (#18 default-device-change, #19 power, #19
audio-service crash, #20 self-feedback isolation) extend this module
without changing the public constructor surface — they hang new
internal coroutines off :meth:`VoiceCaptureWatchdog.start` and new
handlers off :meth:`_on_hotplug`.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING

from sovyx.engine._lock_dict import LRULockDict
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener
from sovyx.voice.health.contract import (
    CascadeResult,
    Diagnosis,
    HotplugEvent,
    HotplugEventKind,
    ProbeResult,
    WatchdogState,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice.health.combo_store import ComboStore

logger = get_logger(__name__)


# Defaults sourced from :class:`VoiceTuningConfig` per CLAUDE.md anti-pattern #17
# so operators can override via ``SOVYX_TUNING__VOICE__*`` env vars.
_DEFAULT_SCHEDULE = tuple(_VoiceTuning().watchdog_backoff_schedule_s)
"""Backoff delays (seconds) between successive warm re-probes."""

_DEFAULT_MAX_ATTEMPTS = _VoiceTuning().watchdog_max_attempts
"""Cap on re-probe attempts per deafness report."""

_DEFAULT_LIFECYCLE_LOCK_MAX = _VoiceTuning().cascade_lifecycle_lock_max
"""Max concurrent endpoints tracked by the shared lifecycle lock."""


def build_platform_hotplug_listener(
    *,
    platform_key: str | None = None,
    runtime_resilience_enabled: bool | None = None,
) -> HotplugListener:
    """Return the listener the current platform + config support.

    Resolution order:

    1. If ``runtime_resilience_enabled`` is ``False`` — hard opt-out via
       the ADR §7 rollback path — return :class:`NoopHotplugListener`.
    2. On Windows — import-and-construct
       :func:`~sovyx.voice.health._hotplug_win.build_windows_hotplug_listener`.
    3. On Linux — :func:`~sovyx.voice.health._hotplug_linux.build_linux_hotplug_listener`.
    4. On macOS — :func:`~sovyx.voice.health._hotplug_mac.build_macos_hotplug_listener`.
    5. Unknown platform — :class:`NoopHotplugListener`.

    The ``platform_key`` override lets tests drive the resolution on a
    non-matching host without monkey-patching :mod:`sys`.
    """
    if runtime_resilience_enabled is None:
        runtime_resilience_enabled = _VoiceTuning().runtime_resilience_enabled
    if not runtime_resilience_enabled:
        return NoopHotplugListener(reason="runtime_resilience_enabled=False")
    plat = platform_key or sys.platform
    if plat == "win32":
        from sovyx.voice.health._hotplug_win import build_windows_hotplug_listener

        return build_windows_hotplug_listener()
    if plat.startswith("linux"):
        from sovyx.voice.health._hotplug_linux import build_linux_hotplug_listener

        return build_linux_hotplug_listener()
    if plat == "darwin":
        from sovyx.voice.health._hotplug_mac import build_macos_hotplug_listener

        return build_macos_hotplug_listener()
    logger.info("voice_hotplug_listener_unavailable", platform=plat, reason="unknown_platform")
    return NoopHotplugListener(reason=f"unknown platform {plat!r}")


class VoiceCaptureWatchdog:
    """Orchestrates §4.4.1 backoff + §4.4.2 hot-plug reactions.

    The watchdog is **per-pipeline-session**: one instance tracks one
    active endpoint. When the pipeline restarts for a new endpoint the
    caller constructs a fresh watchdog. This keeps the state machine
    small and side-effect-free with respect to earlier sessions.

    Args:
        active_endpoint_guid: GUID of the endpoint this watchdog is
            defending. Used to filter hot-plug events — the watchdog
            only reacts to removes that match this GUID (or its
            friendly name when the OS only reports the label).
        active_endpoint_friendly_name: Best-known friendly name for
            the active endpoint. Matched as a fallback when the OS
            reports a friendly name but no GUID (typical on Linux).
        re_probe: Callable invoked once per backoff tick with the
            active endpoint's GUID; returns the warm
            :class:`ProbeResult`. Callers wire this to
            :func:`~sovyx.voice.health.probe.probe` with their
            capture-stream-aware wrapper.
        re_cascade: Callable invoked when a remove-of-active event
            lands or when the watchdog is :attr:`WatchdogState.DEGRADED`
            and a new device arrives. Callers wire this to
            :func:`~sovyx.voice.health.cascade.run_cascade`.
        combo_store: Optional :class:`~sovyx.voice.health.combo_store.ComboStore`.
            When provided, a remove-of-active event invalidates the
            endpoint's entry (ADR §4.4.2).
        lifecycle_locks: Shared :class:`LRULockDict` so this watchdog
            serialises against :func:`run_cascade` on the same endpoint.
            A fresh one is created when ``None``.
        schedule_s: Override the backoff schedule. Defaults to
            :attr:`VoiceTuningConfig.watchdog_backoff_schedule_s`.
        max_attempts: Cap on backoff ticks before DEGRADED. Defaults
            to :attr:`VoiceTuningConfig.watchdog_max_attempts`.
    """

    def __init__(
        self,
        *,
        active_endpoint_guid: str,
        re_probe: Callable[[str], Awaitable[ProbeResult]],
        re_cascade: Callable[[str], Awaitable[CascadeResult]],
        active_endpoint_friendly_name: str = "",
        combo_store: ComboStore | None = None,
        lifecycle_locks: LRULockDict[str] | None = None,
        schedule_s: tuple[float, ...] | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if not active_endpoint_guid:
            msg = "active_endpoint_guid must be a non-empty string"
            raise ValueError(msg)
        self._endpoint = active_endpoint_guid
        self._friendly = active_endpoint_friendly_name
        self._re_probe = re_probe
        self._re_cascade = re_cascade
        self._combo_store = combo_store
        # `or` would treat an empty `LRULockDict` as falsy (it has `__len__`),
        # silently dropping the caller's shared lock. Use an explicit identity
        # check so a freshly-constructed cascade lock dict is honoured.
        self._locks: LRULockDict[str] = (
            lifecycle_locks
            if lifecycle_locks is not None
            else LRULockDict(maxsize=_DEFAULT_LIFECYCLE_LOCK_MAX)
        )
        schedule = tuple(schedule_s) if schedule_s is not None else _DEFAULT_SCHEDULE
        self._max_attempts = max_attempts if max_attempts is not None else _DEFAULT_MAX_ATTEMPTS
        # Trim schedule to max_attempts so a tuning change like
        # ``max_attempts=1`` doesn't still produce three ticks.
        self._schedule: tuple[float, ...] = schedule[: self._max_attempts]
        self._state: WatchdogState = WatchdogState.IDLE
        self._pending: asyncio.Task[None] | None = None
        self._hotplug: HotplugListener | None = None
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    @property
    def state(self) -> WatchdogState:
        return self._state

    @property
    def active_endpoint_guid(self) -> str:
        return self._endpoint

    async def start(self, hotplug: HotplugListener) -> None:
        """Install the hot-plug subscription and begin watching."""
        if self._started:
            return
        self._hotplug = hotplug
        await hotplug.start(self._on_hotplug)
        self._started = True
        logger.info(
            "voice_watchdog_started",
            endpoint=self._endpoint,
            schedule_s=list(self._schedule),
            max_attempts=self._max_attempts,
        )

    async def stop(self) -> None:
        """Cancel any pending re-probe chain and stop the hot-plug listener."""
        self._started = False
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        hotplug = self._hotplug
        self._hotplug = None
        if hotplug is not None:
            await hotplug.stop()

    # ── §4.4.1 Exponential-backoff re-probe ──────────────────────────────

    async def report_deafness(self) -> None:
        """Schedule a backoff re-probe chain for the active endpoint.

        Called by L3 warm probe (heartbeat-driven) or by the pipeline
        orchestrator when VAD / callback degradation is sustained. A
        second call while a chain is already in flight is a no-op so
        multiple deaf heartbeats don't stack schedules.
        """
        if not self._started:
            return
        if self._state == WatchdogState.DEGRADED:
            return
        lock = self._locks[self._endpoint]
        async with lock:
            if self._pending is not None and not self._pending.done():
                return
            self._state = WatchdogState.BACKOFF
            self._pending = asyncio.create_task(self._backoff_chain())
            logger.info(
                "voice_watchdog_backoff_scheduled",
                endpoint=self._endpoint,
                schedule_s=list(self._schedule),
            )

    async def _backoff_chain(self) -> None:
        for attempt_idx, delay in enumerate(self._schedule):
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            attempt = attempt_idx + 1
            try:
                result = await self._re_probe(self._endpoint)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — re-probe failure must not kill chain
                logger.error(
                    "voice_watchdog_reprobe_raised",
                    endpoint=self._endpoint,
                    attempt=attempt,
                    exc_info=True,
                )
                continue
            logger.info(
                "voice_watchdog_reprobe_result",
                endpoint=self._endpoint,
                attempt=attempt,
                diagnosis=result.diagnosis.value,
                vad_max_prob=result.vad_max_prob,
                rms_db=result.rms_db,
            )
            if result.diagnosis == Diagnosis.HEALTHY:
                self._state = WatchdogState.IDLE
                self._pending = None
                logger.info(
                    "voice_watchdog_recovered",
                    endpoint=self._endpoint,
                    attempt=attempt,
                )
                return
        self._state = WatchdogState.DEGRADED
        self._pending = None
        logger.error(
            "voice_capture_permanently_degraded",
            endpoint=self._endpoint,
            attempts=self._max_attempts,
        )

    # ── §4.4.2 Hot-plug reaction ─────────────────────────────────────────

    async def _on_hotplug(self, event: HotplugEvent) -> None:
        if not self._started:
            return
        is_active = self._event_matches_active(event)
        if event.kind == HotplugEventKind.DEVICE_REMOVED and is_active:
            await self._handle_active_removal()
            return
        if event.kind == HotplugEventKind.DEVICE_ADDED and self._state == WatchdogState.DEGRADED:
            await self._handle_degraded_arrival(event)
            return
        # All other combinations are intentionally ignored.
        logger.debug(
            "voice_watchdog_hotplug_ignored",
            endpoint=self._endpoint,
            kind=event.kind.value,
            matches_active=is_active,
            state=self._state.value,
        )

    def _event_matches_active(self, event: HotplugEvent) -> bool:
        return bool(
            (event.endpoint_guid and event.endpoint_guid == self._endpoint)
            or (
                event.device_friendly_name
                and self._friendly
                and event.device_friendly_name == self._friendly
            ),
        )

    async def _handle_active_removal(self) -> None:
        lock = self._locks[self._endpoint]
        async with lock:
            logger.warning(
                "voice_active_endpoint_removed",
                endpoint=self._endpoint,
                friendly_name=self._friendly,
            )
            if self._combo_store is not None:
                try:
                    self._combo_store.invalidate(
                        self._endpoint,
                        reason="hotplug-remove-active-endpoint",
                    )
                except Exception:  # noqa: BLE001 — store failures must not block re-cascade
                    logger.warning(
                        "voice_watchdog_combo_invalidate_failed",
                        endpoint=self._endpoint,
                        exc_info=True,
                    )
        try:
            await self._re_cascade(self._endpoint)
        except Exception:  # noqa: BLE001
            logger.error(
                "voice_watchdog_recascade_raised",
                endpoint=self._endpoint,
                trigger="active_removal",
                exc_info=True,
            )

    async def _handle_degraded_arrival(self, event: HotplugEvent) -> None:
        logger.info(
            "voice_watchdog_degraded_device_added",
            endpoint=self._endpoint,
            added_interface=event.device_interface_name,
            added_friendly=event.device_friendly_name,
        )
        try:
            result = await self._re_cascade(self._endpoint)
        except Exception:  # noqa: BLE001
            logger.error(
                "voice_watchdog_recascade_raised",
                endpoint=self._endpoint,
                trigger="degraded_device_added",
                exc_info=True,
            )
            return
        if result.winning_combo is not None:
            self._state = WatchdogState.IDLE
            logger.info(
                "voice_watchdog_recovered_via_hotplug",
                endpoint=self._endpoint,
                source=result.source,
            )


__all__ = [
    "VoiceCaptureWatchdog",
    "build_platform_hotplug_listener",
]
