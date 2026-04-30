"""Handler for audio driver-update events from the WMI listener.

Phase 1a of ``MISSION-voice-runtime-listener-wireup-2026-04-30.md``
(absorbs T5.50 from the master mission). Receives
:class:`~sovyx.voice.health._driver_update_listener_win.DriverUpdateEvent`
instances from the WMI listener foundation (T5.49, shipped at
``fb815a3``) and decides what to do based on the
``audio_driver_update_recascade_enabled`` tuning flag.

Per ``feedback_staged_adoption`` the handler is the staged-
adoption layer between the listener (detection) and any future
cascade re-run plumbing (action). Detection ships first as
observability; the action is gated independently behind the
``recascade_enabled`` flag with a lenient default that emits
"would re-cascade" without acting.

Lifecycle ownership: the pipeline (Phase 1b of the mission) owns
the handler instance + passes its :meth:`handle_driver_update`
method as the listener's ``on_driver_changed`` callback. The
handler itself is stateless beyond the construction-time flag
value — re-entrancy is safe.

Out of scope (deferred to future mission per Part 4.1 of the
wire-up mission spec):

* Actual cascade re-run plumbing. When ``recascade_enabled=True``
  the handler emits ``voice.driver_update.recascade_would_trigger``
  + records the ``action=triggered`` counter. The orchestrator-
  side wire-up that turns this signal into a real
  :func:`~sovyx.voice.health.cascade.run_cascade` invocation needs
  separate architectural work (mid-utterance interruption
  semantics, idempotence under rapid repeat events, backoff if
  the new driver also fails).

This module is Windows-only by virtue of the WMI listener that
feeds it. On non-Windows hosts the listener factory returns a
no-op listener (see
:func:`~sovyx.voice.health._driver_update_listener_win.build_driver_update_listener`)
so the handler never receives events on those platforms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_driver_update_detected

if TYPE_CHECKING:
    from sovyx.voice.health._driver_update_listener_win import DriverUpdateEvent

logger = get_logger(__name__)


class DriverUpdateHandler:
    """Receive ``DriverUpdateEvent`` + decide based on the recascade flag.

    Construction args:
        recascade_enabled: Captures the
            ``audio_driver_update_recascade_enabled`` tuning flag at
            handler-construction time. The pipeline rebuilds the
            handler when the pipeline restarts, so reading the flag
            once at construction is correct semantics — operators
            who flip the flag mid-session see the change after
            the next pipeline restart.

    The handler is stateless beyond ``recascade_enabled``. Multiple
    concurrent ``handle_driver_update`` calls are safe — each
    invocation only reads ``self._recascade_enabled``, emits logs +
    metrics, and returns.
    """

    def __init__(self, *, recascade_enabled: bool) -> None:
        self._recascade_enabled = recascade_enabled

    @property
    def recascade_enabled(self) -> bool:
        """Snapshot of the recascade flag at construction time.

        Tests + operator-facing diagnostics read this to verify the
        handler was built with the expected flag value. The handler
        does NOT re-read the live flag during ``handle_driver_update``
        — see class docstring for the rationale.
        """
        return self._recascade_enabled

    async def handle_driver_update(self, event: DriverUpdateEvent) -> None:
        """Process one ``DriverUpdateEvent``.

        Sequence:

        1. **Always** emit ``voice.driver_update.detected`` INFO log
           + ``action=detected`` counter. Operator-visible regardless
           of any flag — forensic correlation against deaf-signal
           incidents needs this signal even when the action is
           gated off.
        2. If ``recascade_enabled=False`` (lenient default): emit
           ``voice.driver_update.recascade_skipped{reason=flag_disabled}``
           DEBUG log + ``action=skipped`` counter. Return without
           further action.
        3. If ``recascade_enabled=True``: emit
           ``voice.driver_update.recascade_would_trigger`` WARN log
           + ``action=triggered`` counter. **Actual cascade re-run
           plumbing is OUT OF SCOPE** for this commit — see the
           new mission's Part 4.1 for the deferred work.

        The method is async because the listener's callback contract
        is async (the WMI sink marshals via
        ``asyncio.ensure_future``); the body itself is sync work
        and returns quickly.

        Args:
            event: The driver-update event from the WMI listener.
                ``device_id`` carries the PnP device-instance ID
                (``USB\\VID_xxxx&PID_xxxx\\<serial>`` etc.);
                ``new_driver_version`` is the post-modification
                ``Win32_PnPEntity.DriverVersion``.
        """
        logger.info(
            "voice.driver_update.detected",
            device_id=event.device_id,
            friendly_name=event.friendly_name,
            new_driver_version=event.new_driver_version,
            detected_at=event.detected_at.isoformat(),
        )
        record_driver_update_detected(action="detected")

        if not self._recascade_enabled:
            logger.debug(
                "voice.driver_update.recascade_skipped",
                device_id=event.device_id,
                reason="flag_disabled",
            )
            record_driver_update_detected(action="skipped")
            return

        logger.warning(
            "voice.driver_update.recascade_would_trigger",
            device_id=event.device_id,
            new_driver_version=event.new_driver_version,
            note=(
                "Cascade re-run plumbing not yet wired (see "
                "MISSION-voice-runtime-listener-wireup-2026-04-30 "
                "Part 4.1). The flag is engaged + the listener "
                "detected an event. Future commit will turn this "
                "signal into an actual run_cascade invocation."
            ),
        )
        record_driver_update_detected(action="triggered")


__all__ = ["DriverUpdateHandler"]
