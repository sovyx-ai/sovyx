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

* **§4.4.3 Default-device watcher.** An optional
  :class:`~sovyx.voice.health._default_device.DefaultDeviceWatcher`
  (polling ``sounddevice`` on every platform for Sprint 2; native
  ``IMMNotificationClient`` / PipeWire paths land in Sprint 4). When
  the OS-level default-input changes the watchdog treats it as an
  explicit user intent to switch endpoints — invalidate the prior
  endpoint's :class:`ComboStore` row and cascade on the new default.

* **§4.4.4 Power events.** An optional
  :class:`~sovyx.voice.health._power.PowerEventListener` (Windows
  ``WM_POWERBROADCAST`` in Sprint 2). On ``SUSPEND`` the watchdog
  cancels any pending re-probe chain and marks state DEGRADED so no
  probe fires while the machine sleeps. On ``RESUME`` it waits
  ``watchdog_resume_settle_s`` (defaults to 2 s — USB/BT stacks take
  time to re-enumerate) and re-cascades from scratch.

* **§4.4.5 Audio-service crash.** An optional
  :class:`~sovyx.voice.health._audio_service.AudioServiceMonitor`
  (Windows ``sc query audiosrv`` in Sprint 2). On ``DOWN`` the watchdog
  stalls — probes cannot succeed while the service is stopped — until
  ``UP`` lands or ``watchdog_audio_service_restart_timeout_s`` elapses,
  at which point it emits ``voice_audio_service_down`` and goes
  DEGRADED. On ``UP`` after a prior DOWN it re-cascades.

* **§4.1 / Phase 1 APO-quarantine recheck.** A periodic loop
  (``apo_quarantine_recheck_interval_s``, default 300 s) re-probes
  every endpoint the :class:`CaptureIntegrityCoordinator` has tagged
  ``reason="apo_degraded"``. On a HEALTHY verdict the entry is cleared
  (``action="recheck_recovered"``) so the factory can pick it again on
  the next boot or hotplug; otherwise the store's TTL handles eviction.
  Kernel-invalidated entries are intentionally skipped here — their
  cure is a physical replug, not a DSP retirement.

The watchdog shares the cascade's lifecycle lock (ADR §5.5) so
hot-plug-driven re-cascades cannot race with in-flight :func:`run_cascade`
calls. Callers inject the same :class:`~sovyx.engine._lock_dict.LRULockDict`
that :func:`~sovyx.voice.health.cascade.run_cascade` uses to guarantee
serialisation across both code paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING

from sovyx.engine._lock_dict import LRULockDict
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.health._audio_service import (
    AudioServiceMonitor,
    NoopAudioServiceMonitor,
)
from sovyx.voice.health._default_device import (
    DefaultDeviceWatcher,
    NoopDefaultDeviceWatcher,
)
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener
from sovyx.voice.health._metrics import (
    record_apo_degraded_event,
    record_kernel_invalidated_event,
    record_recovery_attempt,
)
from sovyx.voice.health._power import NoopPowerEventListener, PowerEventListener
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    QuarantineEntry,
    get_default_quarantine,
)
from sovyx.voice.health.contract import (
    AudioServiceEvent,
    AudioServiceEventKind,
    CascadeResult,
    Diagnosis,
    HotplugEvent,
    HotplugEventKind,
    PowerEvent,
    PowerEventKind,
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

_DEFAULT_RESUME_SETTLE_S = _VoiceTuning().watchdog_resume_settle_s
"""§4.4.4 settle delay after ``RESUME`` before re-cascade."""

_DEFAULT_AUDIO_SERVICE_RESTART_TIMEOUT_S = _VoiceTuning().watchdog_audio_service_restart_timeout_s
"""§4.4.5 ceiling — when ``audiosrv`` stays DOWN past this, go DEGRADED."""

_DEFAULT_APO_RECHECK_INTERVAL_S = _VoiceTuning().apo_quarantine_recheck_interval_s
"""§4.1 / Phase 1 — APO-quarantine recheck cadence.

Zero or negative disables the recheck loop. The loop iterates the
shared :class:`EndpointQuarantine` snapshot once per interval, re-probes
every entry tagged ``reason="apo_degraded"``, and clears it on a
HEALTHY verdict so the factory can pick the endpoint again on the next
boot or hotplug."""


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
        resume_settle_s: float | None = None,
        audio_service_restart_timeout_s: float | None = None,
        quarantine: EndpointQuarantine | None = None,
        apo_recheck_interval_s: float | None = None,
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
        self._resume_settle_s = (
            resume_settle_s if resume_settle_s is not None else _DEFAULT_RESUME_SETTLE_S
        )
        self._audio_restart_timeout_s = (
            audio_service_restart_timeout_s
            if audio_service_restart_timeout_s is not None
            else _DEFAULT_AUDIO_SERVICE_RESTART_TIMEOUT_S
        )
        self._apo_recheck_interval_s = (
            apo_recheck_interval_s
            if apo_recheck_interval_s is not None
            else _DEFAULT_APO_RECHECK_INTERVAL_S
        )
        self._state: WatchdogState = WatchdogState.IDLE
        self._pending: asyncio.Task[None] | None = None
        self._apo_recheck_task: asyncio.Task[None] | None = None
        self._hotplug: HotplugListener | None = None
        self._power: PowerEventListener | None = None
        self._audio_service: AudioServiceMonitor | None = None
        self._default_device: DefaultDeviceWatcher | None = None
        # §4.4.5 gating: while the audio service is DOWN, new probes/cascades
        # can't succeed. We park them on an ``asyncio.Event`` set to DOWN and
        # release once an UP event lands.
        self._audio_service_up = asyncio.Event()
        self._audio_service_up.set()
        self._audio_service_down_waiter: asyncio.Task[None] | None = None
        # §4.4.7 — the quarantine store is shared across the cascade,
        # the watchdog (hot-plug clear), and the recheck loop. Default
        # to the process singleton so callers don't have to thread it.
        self._quarantine: EndpointQuarantine = (
            quarantine if quarantine is not None else get_default_quarantine()
        )
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    @property
    def state(self) -> WatchdogState:
        return self._state

    @property
    def active_endpoint_guid(self) -> str:
        return self._endpoint

    async def start(
        self,
        hotplug: HotplugListener,
        *,
        power: PowerEventListener | None = None,
        audio_service: AudioServiceMonitor | None = None,
        default_device: DefaultDeviceWatcher | None = None,
    ) -> None:
        """Install OS subscriptions and begin watching.

        The hot-plug listener is mandatory (Sprint 1 contract); the
        other three are optional so the caller can opt into each
        §4.4.x surface independently. Missing listeners default to
        their Noop variants so the public API stays the same whether
        a surface is on or off.
        """
        if self._started:
            return
        self._hotplug = hotplug
        await hotplug.start(self._on_hotplug)
        if power is not None:
            self._power = power
            await power.start(self._on_power_event)
        if audio_service is not None:
            self._audio_service = audio_service
            await audio_service.start(self._on_audio_service_event)
        if default_device is not None:
            self._default_device = default_device
            await default_device.start(self._on_hotplug)
        if self._apo_recheck_interval_s > 0:
            self._apo_recheck_task = spawn(self._apo_recheck_loop(), name="voice-watchdog-apo-recheck")
        self._started = True
        logger.info(
            "voice_watchdog_started",
            endpoint=self._endpoint,
            schedule_s=list(self._schedule),
            max_attempts=self._max_attempts,
            power_enabled=power is not None,
            audio_service_enabled=audio_service is not None,
            default_device_enabled=default_device is not None,
            apo_recheck_interval_s=self._apo_recheck_interval_s,
        )

    async def stop(self) -> None:
        """Cancel every pending task and tear down OS subscriptions."""
        self._started = False
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        recheck = self._apo_recheck_task
        self._apo_recheck_task = None
        if recheck is not None and not recheck.done():
            recheck.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recheck
        waiter = self._audio_service_down_waiter
        self._audio_service_down_waiter = None
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter
        hotplug = self._hotplug
        self._hotplug = None
        if hotplug is not None:
            await hotplug.stop()
        power = self._power
        self._power = None
        if power is not None:
            await power.stop()
        audio_service = self._audio_service
        self._audio_service = None
        if audio_service is not None:
            await audio_service.stop()
        default_device = self._default_device
        self._default_device = None
        if default_device is not None:
            await default_device.stop()

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
            record_recovery_attempt(trigger="deaf_backoff")
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

    # ── §4.1 / Phase 1 — APO-quarantine periodic recheck ─────────────────

    async def _apo_recheck_loop(self) -> None:
        """Periodically re-probe APO-quarantined endpoints.

        The :class:`CaptureIntegrityCoordinator` quarantines an endpoint
        with ``reason="apo_degraded"`` when every
        :class:`PlatformBypassStrategy` candidate fails to restore a
        HEALTHY integrity verdict. This loop gives the endpoint a chance
        to escape quarantine without waiting for the full TTL or a
        hotplug event — typical cure is an OS update that retires the
        APO (``voiceclarityep`` KB roll-back, PipeWire module purge) or
        a manual driver reinstall.

        Behaviour per tick:

        1. Snapshot the quarantine store and filter entries whose
           ``reason`` is ``"apo_degraded"``. Kernel-invalidated entries
           have their own recheck path via the backoff chain / hot-plug
           listener and are intentionally skipped here.
        2. Re-probe each GUID via :attr:`_re_probe` (the factory wires
           this to a cascade-style warm probe that opens its own
           transient capture stream, so it works on non-active
           endpoints).
        3. On :class:`~sovyx.voice.health.contract.Diagnosis.HEALTHY`
           clear the quarantine and emit ``action="recheck_recovered"``.
        4. On any other diagnosis keep the entry and emit
           ``action="recheck_still_invalid"``. The store's own TTL
           ultimately evicts stale entries if the interval never
           produces a recovery.

        The loop is cancellation-safe: :meth:`stop` cancels the task
        and a raised :class:`asyncio.CancelledError` exits cleanly.
        """
        platform_key = self._platform_key_for_metric()
        while self._started:
            try:
                await asyncio.sleep(self._apo_recheck_interval_s)
            except asyncio.CancelledError:
                return
            if not self._started:
                return
            snapshot = tuple(
                entry for entry in self._quarantine.snapshot() if entry.reason == "apo_degraded"
            )
            if not snapshot:
                continue
            logger.debug(
                "voice_watchdog_apo_recheck_tick",
                candidates=len(snapshot),
            )
            for entry in snapshot:
                if not self._started:
                    return
                try:
                    result = await self._re_probe(entry.endpoint_guid)
                except asyncio.CancelledError:
                    return
                except Exception:  # noqa: BLE001 — one failing probe must not stop the loop
                    logger.warning(
                        "voice_watchdog_apo_recheck_probe_raised",
                        endpoint=entry.endpoint_guid,
                        friendly_name=entry.device_friendly_name,
                        exc_info=True,
                    )
                    continue
                if result.diagnosis == Diagnosis.HEALTHY:
                    cleared = self._quarantine.clear(
                        entry.endpoint_guid,
                        reason="apo_recheck_recovered",
                    )
                    if cleared:
                        record_apo_degraded_event(
                            platform=platform_key,
                            action="recheck_recovered",
                        )
                        logger.info(
                            "voice_watchdog_apo_recheck_recovered",
                            endpoint=entry.endpoint_guid,
                            friendly_name=entry.device_friendly_name,
                            host_api=entry.host_api,
                        )
                    continue
                record_apo_degraded_event(
                    platform=platform_key,
                    action="recheck_still_invalid",
                )
                logger.debug(
                    "voice_watchdog_apo_recheck_still_invalid",
                    endpoint=entry.endpoint_guid,
                    friendly_name=entry.device_friendly_name,
                    diagnosis=result.diagnosis.value,
                )

    # ── §4.4.2 Hot-plug reaction ─────────────────────────────────────────

    async def _on_hotplug(self, event: HotplugEvent) -> None:
        if not self._started:
            return
        # §4.4.7 — a USB replug or driver reload is the canonical cure for
        # a kernel-invalidated endpoint. Clear quarantine on either kind
        # of event regardless of whether it targets the active endpoint:
        # the quarantine spans every endpoint we've ever failed-over from,
        # so an event for one of those non-active GUIDs must still clear
        # it so the next factory boot can pick that endpoint again.
        if event.kind in {HotplugEventKind.DEVICE_REMOVED, HotplugEventKind.DEVICE_ADDED}:
            self._maybe_clear_quarantine_on_hotplug(event)
        if event.kind == HotplugEventKind.DEFAULT_DEVICE_CHANGED:
            await self._handle_default_device_change(event)
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

    def _maybe_clear_quarantine_on_hotplug(self, event: HotplugEvent) -> None:
        """Clear quarantine for the event's endpoint when applicable.

        Three resolution rules, in order:

        1. Direct GUID match — every backend (Windows ``IMMNotificationClient``,
           Linux ``udev``, macOS ``IOAudio``) tries to populate
           ``endpoint_guid``. When present and the GUID is quarantined,
           clear it.
        2. Friendly-name match — Linux ``udev`` events typically lack a
           GUID but report a stable interface name. We scan the live
           snapshot for the first entry whose ``device_friendly_name``
           or ``device_interface_name`` matches the event's labels.
        3. Otherwise no-op — a generic add/remove with no identifying
           fields cannot safely clear any specific entry.
        """
        guid = event.endpoint_guid
        if guid and self._quarantine.is_quarantined(guid):
            entry = self._quarantine.get(guid)
            if self._quarantine.clear(guid, reason="hotplug_clear"):
                self._emit_hotplug_clear_metric(entry, event=event)
            return
        friendly = event.device_friendly_name
        interface = event.device_interface_name
        if not friendly and not interface:
            return
        for entry in self._quarantine.snapshot():
            label_match = bool(
                (friendly and entry.device_friendly_name == friendly)
                or (interface and entry.device_interface_name == interface),
            )
            if not label_match:
                continue
            if self._quarantine.clear(entry.endpoint_guid, reason="hotplug_clear"):
                self._emit_hotplug_clear_metric(
                    entry,
                    event=event,
                    matched_by="friendly_name" if friendly else "interface_name",
                )
            # Stop after the first match — duplicate friendly names are
            # vanishingly rare and clearing every match would mask any
            # ambiguity in operator-facing logs.
            return

    def _emit_hotplug_clear_metric(
        self,
        entry: QuarantineEntry | None,
        *,
        event: HotplugEvent,
        matched_by: str = "endpoint_guid",
    ) -> None:
        """Emit the correct telemetry surface for a hotplug-clear event.

        APO-degraded entries emit the :func:`record_apo_degraded_event`
        counter so the Phase 1 APO dashboards attribute the recovery
        path correctly. Everything else (kernel-invalidated,
        factory-integration, probe_*) continues to emit
        :func:`record_kernel_invalidated_event` as before — preserving
        the pre-Phase-1 metric contract.
        """
        platform = self._platform_key_for_metric()
        host_api = entry.host_api if entry is not None and entry.host_api else "unknown"
        reason = entry.reason if entry is not None else ""
        endpoint = entry.endpoint_guid if entry is not None else (event.endpoint_guid or "")
        if reason == "apo_degraded":
            record_apo_degraded_event(
                platform=platform,
                action="hotplug_clear",
            )
            logger.info(
                "voice_apo_degraded_hotplug_clear",
                endpoint=endpoint,
                kind=event.kind.value,
                matched_by=matched_by,
            )
            return
        record_kernel_invalidated_event(
            platform=platform,
            host_api=host_api,
            action="hotplug_clear",
        )
        logger.info(
            "voice_kernel_invalidated_hotplug_clear",
            endpoint=endpoint,
            kind=event.kind.value,
            matched_by=matched_by,
        )

    def _platform_key_for_metric(self) -> str:
        """Resolve the platform tag used in §4.4.7 telemetry labels."""
        plat = sys.platform
        if plat.startswith("win"):
            return "win32"
        if plat == "darwin":
            return "darwin"
        return "linux"

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
        record_recovery_attempt(trigger="hotplug")
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
        record_recovery_attempt(trigger="hotplug")
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

    # ── §4.4.3 Default-device change ─────────────────────────────────────

    async def _handle_default_device_change(self, event: HotplugEvent) -> None:
        """User flipped the default mic — invalidate prior endpoint + re-cascade.

        The watchdog does not (yet) switch its own ``active_endpoint_guid``;
        the pipeline orchestrator reacts to the ``voice_default_device_changed``
        log entry and re-constructs a fresh watchdog for the new endpoint.
        Invalidating the combo store for the previous endpoint here prevents
        a stale winning_combo from hijacking the next cascade.
        """
        logger.info(
            "voice_default_device_changed_reacted",
            previous_endpoint=self._endpoint,
            new_friendly=event.device_friendly_name,
        )
        if self._combo_store is not None:
            lock = self._locks[self._endpoint]
            async with lock:
                try:
                    self._combo_store.invalidate(
                        self._endpoint,
                        reason="default-device-changed",
                    )
                except Exception:  # noqa: BLE001 — store failures must not block re-cascade
                    logger.warning(
                        "voice_watchdog_combo_invalidate_failed",
                        endpoint=self._endpoint,
                        exc_info=True,
                    )
        record_recovery_attempt(trigger="default_change")
        try:
            await self._re_cascade(self._endpoint)
        except Exception:  # noqa: BLE001
            logger.error(
                "voice_watchdog_recascade_raised",
                endpoint=self._endpoint,
                trigger="default_device_changed",
                exc_info=True,
            )

    # ── §4.4.4 Power events ──────────────────────────────────────────────

    async def _on_power_event(self, event: PowerEvent) -> None:
        if not self._started:
            return
        if event.kind == PowerEventKind.SUSPEND:
            await self._handle_suspend()
            return
        if event.kind == PowerEventKind.RESUME:
            await self._handle_resume()

    async def _handle_suspend(self) -> None:
        logger.info("voice_watchdog_suspend", endpoint=self._endpoint)
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        # Any probe/cascade that races through suspend will just fail; we
        # simply want to make sure no new re-probe chain fires until resume.
        self._state = WatchdogState.BACKOFF

    async def _handle_resume(self) -> None:
        logger.info(
            "voice_watchdog_resume",
            endpoint=self._endpoint,
            settle_s=self._resume_settle_s,
        )
        try:
            await asyncio.sleep(self._resume_settle_s)
        except asyncio.CancelledError:
            raise
        record_recovery_attempt(trigger="power")
        try:
            result = await self._re_cascade(self._endpoint)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.error(
                "voice_watchdog_recascade_raised",
                endpoint=self._endpoint,
                trigger="resume",
                exc_info=True,
            )
            return
        self._state = (
            WatchdogState.IDLE if result.winning_combo is not None else WatchdogState.DEGRADED
        )

    # ── §4.4.5 Audio-service crash ───────────────────────────────────────

    async def _on_audio_service_event(self, event: AudioServiceEvent) -> None:
        if not self._started:
            return
        if event.kind == AudioServiceEventKind.DOWN:
            await self._handle_audio_service_down()
            return
        if event.kind == AudioServiceEventKind.UP:
            await self._handle_audio_service_up()

    async def _handle_audio_service_down(self) -> None:
        logger.warning(
            "voice_audio_service_down",
            endpoint=self._endpoint,
            restart_timeout_s=self._audio_restart_timeout_s,
        )
        self._audio_service_up.clear()
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        waiter = self._audio_service_down_waiter
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter
        self._audio_service_down_waiter = asyncio.create_task(
            self._await_audio_service_restart(),
        )

    async def _await_audio_service_restart(self) -> None:
        try:
            await asyncio.wait_for(
                self._audio_service_up.wait(),
                timeout=self._audio_restart_timeout_s,
            )
        except TimeoutError:
            self._state = WatchdogState.DEGRADED
            logger.error(
                "voice_audio_service_restart_timeout",
                endpoint=self._endpoint,
                waited_s=self._audio_restart_timeout_s,
            )
        except asyncio.CancelledError:
            raise

    async def _handle_audio_service_up(self) -> None:
        was_down = not self._audio_service_up.is_set()
        self._audio_service_up.set()
        if not was_down:
            # First observation is UP — baseline seeded, nothing to do.
            return
        logger.info("voice_audio_service_up", endpoint=self._endpoint)
        record_recovery_attempt(trigger="audio_service")
        try:
            result = await self._re_cascade(self._endpoint)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.error(
                "voice_watchdog_recascade_raised",
                endpoint=self._endpoint,
                trigger="audio_service_up",
                exc_info=True,
            )
            return
        self._state = (
            WatchdogState.IDLE if result.winning_combo is not None else WatchdogState.DEGRADED
        )


def build_platform_power_listener(
    *,
    platform_key: str | None = None,
    runtime_resilience_enabled: bool | None = None,
) -> PowerEventListener:
    """Return the power-event listener the current platform + config support.

    Mirrors :func:`build_platform_hotplug_listener`: rollback kill-switch,
    Windows backend in Sprint 2, Linux / macOS Noop until Sprint 4.
    """
    if runtime_resilience_enabled is None:
        runtime_resilience_enabled = _VoiceTuning().runtime_resilience_enabled
    if not runtime_resilience_enabled:
        return NoopPowerEventListener(reason="runtime_resilience_enabled=False")
    plat = platform_key or sys.platform
    if plat == "win32":
        from sovyx.voice.health._power_win import build_windows_power_listener

        return build_windows_power_listener()
    logger.info(
        "voice_power_listener_unavailable",
        platform=plat,
        reason="sprint4_backend_pending",
    )
    return NoopPowerEventListener(reason=f"no backend for platform {plat!r}")


def build_platform_audio_service_monitor(
    *,
    platform_key: str | None = None,
    runtime_resilience_enabled: bool | None = None,
) -> AudioServiceMonitor:
    """Return the audio-service monitor the current platform + config support.

    macOS always returns :class:`NoopAudioServiceMonitor` because
    ``coreaudiod`` is managed by launchd and effectively always respawns.
    Linux Noop until Sprint 4 (``systemctl is-active pipewire.service``).
    """
    if runtime_resilience_enabled is None:
        runtime_resilience_enabled = _VoiceTuning().runtime_resilience_enabled
    if not runtime_resilience_enabled:
        return NoopAudioServiceMonitor(reason="runtime_resilience_enabled=False")
    plat = platform_key or sys.platform
    if plat == "win32":
        from sovyx.voice.health._audio_service_win import (
            build_windows_audio_service_monitor,
        )

        return build_windows_audio_service_monitor()
    if plat == "darwin":
        return NoopAudioServiceMonitor(reason="coreaudiod respawns via launchd")
    logger.info(
        "voice_audio_service_monitor_unavailable",
        platform=plat,
        reason="sprint4_backend_pending",
    )
    return NoopAudioServiceMonitor(reason=f"no backend for platform {plat!r}")


def build_platform_default_device_watcher(
    *,
    query_default: Callable[[], object] | None = None,
    platform_key: str | None = None,
    runtime_resilience_enabled: bool | None = None,
) -> DefaultDeviceWatcher:
    """Return a polling default-device watcher, or Noop when disabled.

    Sprint 2 uses the same :class:`PollingDefaultDeviceWatcher` on every
    platform; native notification paths land in Sprint 4. Callers must
    supply a ``query_default`` that returns a stable identifier of the
    current default input (e.g. ``sounddevice.query_devices`` index + name).
    When ``query_default`` is ``None`` the factory returns Noop so the
    watchdog can still boot in environments without PortAudio.
    """
    if runtime_resilience_enabled is None:
        runtime_resilience_enabled = _VoiceTuning().runtime_resilience_enabled
    if not runtime_resilience_enabled:
        return NoopDefaultDeviceWatcher(reason="runtime_resilience_enabled=False")
    del platform_key  # reserved for Sprint 4 native overrides
    if query_default is None:
        return NoopDefaultDeviceWatcher(reason="no query_default supplied")
    from sovyx.voice.health._default_device import PollingDefaultDeviceWatcher

    return PollingDefaultDeviceWatcher(query_default=query_default)


__all__ = [
    "VoiceCaptureWatchdog",
    "build_platform_audio_service_monitor",
    "build_platform_default_device_watcher",
    "build_platform_hotplug_listener",
    "build_platform_power_listener",
]
