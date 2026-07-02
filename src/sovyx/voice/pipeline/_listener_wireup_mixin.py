"""Runtime listener wire-up mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's runtime device-monitoring listener lifecycle:
the Windows :class:`MMNotificationListener` (IMMNotificationClient COM
device-change subscription) and the Windows
:class:`DriverUpdateListener` (WMI driver-update subscription). Both
listeners are registered once at :meth:`VoicePipeline.start` and torn
down symmetrically at :meth:`VoicePipeline.stop`.

Pre-extraction this surface lived as 4 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — fifth strike of the Phase 5.F.19+ orchestrator
split.

v0.32.3 Phase 3.B.1 contract: both ``_on_default_capture_changed`` +
``_on_device_state_changed`` are **observability-only by design**.
The :class:`sovyx.voice.health.watchdog.VoiceHealthWatchdog` owns the
authoritative active-device restart path via the
``WM_DEVICECHANGE`` listener (see
:func:`build_windows_hotplug_listener` →
``_handle_active_removal`` → ``_re_cascade``). Wiring restart from
BOTH sources would race the watchdog's lock + invalidate idempotency
— so these IMM callbacks STAY log-only. The IMM subscription provides
complementary fine-grained audio-endpoint observability that
``WM_DEVICECHANGE`` cannot (``flow=eCapture, role=eCommunications``
filters + detailed state codes for dashboards).

Failure-isolation contract:

* The two listener registrations are independent — failure of one
  does NOT block the other. Each is wrapped in its own try/except
  with structured WARN.
* If the asyncio loop cannot be obtained (rare, requires the
  pipeline to be started outside an event loop), the entire
  registration step is skipped with a structured WARN. Pipeline
  keeps working with degraded device-change awareness.
* Unregister is wrapped in try/except so a wedged WMI service or COM
  marshalling glitch on one listener never blocks the pipeline
  shutdown path.
* The MM listener's ``register()`` runs INLINE on the event loop
  thread by design — offloading it to ``asyncio.to_thread`` would hit
  ``CO_E_NOTINITIALIZED`` on COM-uninitialised executor workers and be
  silently swallowed by the listener's defensive register contract.
  See the threading note above the register call in
  :meth:`ListenerWireupMixin._register_listeners`.

Anti-pattern #32 contract: zero cross-mixin method calls. The 2
async callbacks are passed AS REFERENCES (``self._on_default_capture_changed``
/ ``self._on_device_state_changed``) to the listener factories — when
the listeners invoke them, MRO resolves them on the host as usual.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_listeners: list[MMNotificationListener]`` — append/clear list of
  registered listeners; also populated with ``DriverUpdateListener``
  (the typed list is the broader ``MMNotificationListener``-compatible
  protocol per the original orchestrator's typing).
* ``_mm_notification_listener_enabled: bool`` — config flag for the
  IMM subscription registration.
* ``_audio_driver_update_listener_enabled: bool`` — config flag for
  the WMI subscription registration.
* ``_audio_driver_update_recascade_enabled: bool`` — config flag for
  the recascade-on-driver-update behaviour.
* ``_config.mind_id`` — read by the 2 callbacks for log emission
  attribution.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice._mm_notification_client import (
    create_listener as create_mm_notification_listener,
)
from sovyx.voice.health._driver_update_handler import DriverUpdateHandler
from sovyx.voice.health._driver_update_listener_win import (
    build_driver_update_listener,
)

if TYPE_CHECKING:
    from sovyx.voice._mm_notification_client import MMNotificationListener
    from sovyx.voice.pipeline._config import VoicePipelineConfig

logger = get_logger(__name__)


class ListenerWireupMixin:
    """Runtime device-monitoring listener registration + teardown.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the register / unregister lifecycle
    + the 2 observability-only async callbacks.

    See module docstring for the full responsibility carve-out + the
    observability-only contract for the IMM callbacks.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes. Declared
        # TYPE_CHECKING so mypy strict resolves the references without
        # creating runtime attributes that would interfere with the
        # host's own initialisation order.
        _listeners: list[MMNotificationListener]
        _mm_notification_listener_enabled: bool
        _audio_driver_update_listener_enabled: bool
        _audio_driver_update_recascade_enabled: bool
        _config: VoicePipelineConfig

    def _register_listeners(self) -> None:
        """Build + register the runtime device-monitoring listeners.

        Called once from :meth:`VoicePipeline.start`. Each listener
        registers independently — failure of one does NOT block the
        others. Successful registrations are appended to
        ``self._listeners`` for symmetric teardown in
        :meth:`_unregister_listeners`.

        On any failure to obtain the asyncio loop (the listeners need
        it for ``call_soon_threadsafe`` marshalling), the entire
        registration step is skipped with a structured WARN. Pipeline
        keeps working with degraded device-change awareness.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            logger.warning(
                "voice.pipeline.listener_registration_skipped",
                reason="no_running_event_loop",
                error=str(exc),
            )
            return

        # MM notification listener — Windows COM device-change events.
        #
        # THREADING NOTE (audit 2026-07-02, anti-pattern #14 sweep +
        # #48 disposition): ``mm_listener.register()`` performs COM
        # calls (``CoCreateInstance`` +
        # ``RegisterEndpointNotificationCallback``) that can block if
        # the Windows audio service is wedged. It is nonetheless
        # invoked INLINE here on purpose — do NOT wrap it in
        # ``asyncio.to_thread``: comtypes (verified at 1.4.16,
        # ``comtypes/__init__.py`` module-level ``CoInitializeEx()``)
        # initialises COM only on the thread that first imports it and
        # ``comtypes.client.CreateObject`` does no per-thread init, so
        # a default-executor worker without COM initialised fails with
        # ``CO_E_NOTINITIALIZED`` — which the listener's defensive
        # ``except BaseException`` register contract SWALLOWS into
        # "registered=False + WARN". The offload would trade a rare
        # blocking hazard (wedged audiosrv, opt-in flag that is
        # default-OFF at HEAD — WINDOWS-7) for a deterministic silent
        # registration failure. Same constraint is already documented
        # for playback in :mod:`sovyx.voice.audio`
        # (``_play_chunk``: "a requirement that asyncio.to_thread
        # workers do not satisfy"). The real cure is a dedicated
        # CoInitializeEx(MTA)-owning worker thread INSIDE
        # ``WindowsMMNotificationListener`` (mirroring
        # ``sovyx.voice.health._driver_update_listener_win
        # .WindowsDriverUpdateListener._run_worker``) so register/
        # unregister share one COM apartment — tracked as follow-up in
        # MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02
        # (#14-residual item 5).
        try:
            mm_listener = create_mm_notification_listener(
                loop=loop,
                on_default_capture_changed=self._on_default_capture_changed,
                on_device_state_changed=self._on_device_state_changed,
                enabled=self._mm_notification_listener_enabled,
            )
            mm_listener.register()
            self._listeners.append(mm_listener)
        except BaseException as exc:  # noqa: BLE001 — listener registration must NEVER block pipeline start
            logger.warning(
                "voice.pipeline.mm_listener_register_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Driver-update listener — Windows WMI subscription.
        # Independent of MM listener result per failure-isolation
        # contract.
        try:
            handler = DriverUpdateHandler(
                recascade_enabled=self._audio_driver_update_recascade_enabled,
            )
            driver_update_listener = build_driver_update_listener(
                loop=loop,
                on_driver_changed=handler.handle_driver_update,
                enabled=self._audio_driver_update_listener_enabled,
            )
            driver_update_listener.register()
            self._listeners.append(driver_update_listener)
        except BaseException as exc:  # noqa: BLE001 — see MM listener rationale above
            logger.warning(
                "voice.pipeline.driver_update_listener_register_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _unregister_listeners(self) -> None:
        """Tear down all registered runtime listeners.

        Called once from :meth:`VoicePipeline.stop`. Each unregister
        is wrapped in try/except so a wedged WMI service / COM
        marshalling glitch on one listener doesn't block the pipeline
        shutdown path. Idempotent — calling on an already-unregistered
        listener is a no-op.

        After this returns, ``self._listeners`` is empty. A subsequent
        ``start()`` call (after a stop) will re-register fresh
        listeners via :meth:`_register_listeners`.
        """
        for listener in self._listeners:
            try:
                listener.unregister()
            except BaseException as exc:  # noqa: BLE001 — shutdown must never propagate
                logger.warning(
                    "voice.pipeline.listener_unregister_failed",
                    listener_type=type(listener).__name__,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        self._listeners.clear()

    async def _on_default_capture_changed(self, device_id: str) -> None:
        """Async callback for ``IMMNotificationClient.OnDefaultDeviceChanged``
        events filtered to ``flow=eCapture, role=eCommunications``.

        v0.32.3 Phase 3.B.1 — observability-only **by design**. Pre-fix
        the docstring claimed the restart wire-up was "deferred", but
        the architecture has since converged on the
        :class:`sovyx.voice.health.watchdog.VoiceHealthWatchdog` owning
        active-device restart via the ``WM_DEVICECHANGE`` listener
        (:func:`build_windows_hotplug_listener`) → ``_handle_active_removal``
        → ``_re_cascade``. The watchdog path is the single source of
        truth for restart on Windows; the IMM subscription provides
        complementary fine-grained audio-endpoint observability that
        ``WM_DEVICECHANGE`` cannot (it carries
        ``flow=eCapture, role=eCommunications`` filters + detailed
        state codes). Wiring restart from BOTH sources would race the
        watchdog's lock + invalidate idempotency. Keep this callback
        log-only.
        """
        logger.info(
            "voice.default_capture_changed",
            device_id=device_id,
            mind_id=self._config.mind_id,
        )

    async def _on_device_state_changed(self, device_id: str, new_state: int) -> None:
        """Async callback for ``IMMNotificationClient.OnDeviceStateChanged``.

        Same scope contract as :meth:`_on_default_capture_changed` —
        observability-only by design (v0.32.3 Phase 3.B.1 docstring
        clarification). The watchdog's ``WM_DEVICECHANGE`` listener
        owns the actual restart via ``_handle_active_removal``; the
        IMM subscription complements it with audio-endpoint-specific
        state codes (UNPLUGGED / NOT_PRESENT / DISABLED) for
        dashboards.

        Args:
            device_id: The endpoint GUID whose state changed.
            new_state: ``DEVICE_STATE_*`` bitfield value (0x1=ACTIVE,
                0x2=DISABLED, 0x4=NOT_PRESENT, 0x8=UNPLUGGED).
        """
        logger.info(
            "voice.device_state_changed",
            device_id=device_id,
            new_state=hex(new_state & 0xFFFFFFFF),
            mind_id=self._config.mind_id,
        )
