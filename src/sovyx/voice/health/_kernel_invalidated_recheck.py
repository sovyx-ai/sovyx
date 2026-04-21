"""§4.4.7 periodic recheck loop for kernel-invalidated endpoints.

ADR §4.4.7. The :class:`~sovyx.voice.health._quarantine.EndpointQuarantine`
holds endpoints whose IAudioClient is in an invalidated state. Two cure
paths exist:

1. **Physical** — the user replugs the USB cable or reboots. The watchdog
   reacts to the OS hot-plug event and clears the quarantine immediately.
2. **Spontaneous** — an audio-driver upgrade or stack reset eventually
   re-arms the IMMDevice. Sovyx polls quarantined endpoints every
   :attr:`VoiceTuningConfig.kernel_invalidated_recheck_interval_s`
   seconds with a fresh :class:`ProbeMode.COLD` probe; on
   :attr:`Diagnosis.HEALTHY` the entry is cleared and the orchestrator
   can fail back.

Both paths emit ``sovyx.voice.health.kernel_invalidated.events`` so
operators can see, on Grafana, the lifecycle ``quarantine →
recheck_still_invalid → … → recheck_recovered`` for any endpoint that
ever entered the §4.4.7 path.

The rechecker owns one :class:`asyncio.Task`; lifecycle is
``start()`` / ``stop()`` to mirror the watchdog. It is sibling to
:class:`~sovyx.voice.health.watchdog.VoiceCaptureWatchdog` — the
watchdog defends *one* active endpoint while the rechecker rotates
across every *quarantined* endpoint, so they cannot share their
lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.health._metrics import record_kernel_invalidated_event
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    QuarantineEntry,
    get_default_quarantine,
)
from sovyx.voice.health.contract import Diagnosis, ProbeResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


class RecheckProbeCallable(Protocol):
    """Structural type for the per-entry recheck probe.

    The callable receives the :class:`QuarantineEntry` (so the implementation
    can extract whatever device-side handle it needs — PortAudio index,
    ALSA card path, etc.) and returns the :class:`ProbeResult` of a fresh
    cold probe. Implementations MUST honour the cold-probe contract: open
    the stream, observe callbacks, close. They MUST NOT raise — any
    open-side failure is reported as a :class:`Diagnosis` value (typically
    :attr:`Diagnosis.KERNEL_INVALIDATED` for the persistent case).
    """

    async def __call__(self, entry: QuarantineEntry) -> ProbeResult: ...


class KernelInvalidatedRechecker:
    """Background task that retries quarantined endpoints on a fixed cadence.

    The loop:

    1. Sleep ``interval_s``.
    2. Snapshot the live quarantine entries (expired rows are purged in
       :meth:`EndpointQuarantine.snapshot`).
    3. For each entry, invoke :meth:`probe_entry` to run a fresh cold
       probe.
    4. On :attr:`Diagnosis.HEALTHY` → clear the quarantine entry +
       emit ``action="recheck_recovered"``.
    5. On :attr:`Diagnosis.KERNEL_INVALIDATED` → re-add the entry to
       extend its TTL + emit ``action="recheck_still_invalid"``.
    6. Anything else → log + leave the entry untouched. Mid-state
       diagnoses (``DEVICE_BUSY``, ``DRIVER_ERROR``) tend to flap; we
       wait for either a clear HEALTHY or another explicit
       KERNEL_INVALIDATED before mutating quarantine.

    Args:
        probe_entry: Callable matching :class:`RecheckProbeCallable`. The
            orchestrator wires this to the same probe stack the cascade
            uses, with the device index resolved from the entry's GUID
            via the device-enum layer.
        quarantine: Quarantine store to drive. Defaults to the process
            singleton via :func:`get_default_quarantine`.
        interval_s: Sleep between rounds in seconds. Defaults to
            :attr:`VoiceTuningConfig.kernel_invalidated_recheck_interval_s`.
        clock: Injected ``asyncio.sleep``-compatible callable for tests
            that want to drive cadence deterministically. Defaults to
            :func:`asyncio.sleep`.
    """

    def __init__(
        self,
        *,
        probe_entry: RecheckProbeCallable,
        quarantine: EndpointQuarantine | None = None,
        interval_s: float | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._probe = probe_entry
        self._quarantine = quarantine if quarantine is not None else get_default_quarantine()
        self._interval_s = (
            interval_s
            if interval_s is not None
            else _VoiceTuning().kernel_invalidated_recheck_interval_s
        )
        if self._interval_s <= 0:
            msg = f"interval_s must be positive, got {self._interval_s}"
            raise ValueError(msg)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def is_running(self) -> bool:
        return self._started and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Spawn the background recheck task. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = spawn(self._loop(), name="voice-kernel-invalidated-recheck")
        logger.info(
            "voice_kernel_invalidated_rechecker_started",
            interval_s=self._interval_s,
        )

    async def stop(self) -> None:
        """Cancel the background task and wait for it to settle."""
        self._started = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while self._started:
            try:
                await self._sleep(self._interval_s)
            except asyncio.CancelledError:
                return
            if not self._started:
                return
            try:
                await self._round()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — recheck failure must not kill the loop
                logger.error("voice_kernel_invalidated_recheck_round_raised", exc_info=True)

    async def _round(self) -> None:
        snapshot = self._quarantine.snapshot()
        if not snapshot:
            return
        logger.debug(
            "voice_kernel_invalidated_recheck_round",
            count=len(snapshot),
        )
        for entry in snapshot:
            if not self._started:
                return
            await self._probe_one(entry)

    async def _probe_one(self, entry: QuarantineEntry) -> None:
        try:
            result = await self._probe(entry)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — single-endpoint failure mustn't kill the round
            logger.error(
                "voice_kernel_invalidated_recheck_probe_raised",
                endpoint=entry.endpoint_guid,
                friendly_name=entry.device_friendly_name,
                exc_info=True,
            )
            return

        if result.diagnosis is Diagnosis.HEALTHY:
            cleared = self._quarantine.clear(
                entry.endpoint_guid,
                reason="recheck_recovered",
            )
            if cleared:
                record_kernel_invalidated_event(
                    platform=result.combo.platform_key or "unknown",
                    host_api=result.combo.host_api,
                    action="recheck_recovered",
                )
                logger.info(
                    "voice_kernel_invalidated_recovered",
                    endpoint=entry.endpoint_guid,
                    friendly_name=entry.device_friendly_name,
                    host_api=result.combo.host_api,
                )
            return

        if result.diagnosis is Diagnosis.KERNEL_INVALIDATED:
            # Re-add to extend the TTL — the underlying state has not
            # cleared, so the user-visible "quarantined since" timer
            # should reflect the most recent failed recheck.
            self._quarantine.add(
                endpoint_guid=entry.endpoint_guid,
                device_friendly_name=entry.device_friendly_name,
                device_interface_name=entry.device_interface_name,
                host_api=entry.host_api,
                reason="watchdog_recheck",
                physical_device_id=entry.physical_device_id,
            )
            record_kernel_invalidated_event(
                platform=result.combo.platform_key or "unknown",
                host_api=entry.host_api or "unknown",
                action="recheck_still_invalid",
            )
            return

        # Mid-state diagnosis (DEVICE_BUSY, DRIVER_ERROR, …). Don't mutate
        # quarantine — wait for a clear HEALTHY/KERNEL_INVALIDATED next round.
        logger.info(
            "voice_kernel_invalidated_recheck_inconclusive",
            endpoint=entry.endpoint_guid,
            friendly_name=entry.device_friendly_name,
            diagnosis=str(result.diagnosis),
        )


__all__ = [
    "KernelInvalidatedRechecker",
    "RecheckProbeCallable",
]
