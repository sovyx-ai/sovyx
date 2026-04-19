"""Audio-service crash monitor contract for ADR §4.4.5.

Windows ``audiosrv`` can die from driver bugs, Windows Update restarts
of the service, or a user-initiated ``net stop``. When the service
restarts every open PortAudio stream is permanently broken — the
pipeline must close, wait for the service to come back, then
re-cascade. On Linux the equivalent is PipeWire / PulseAudio daemons
dying under systemd-user supervision; on macOS ``coreaudiod`` is
managed by launchd and essentially always respawns, so the monitor is
a Noop there.

The :class:`AudioServiceMonitor` protocol is intentionally small:
``start`` installs the poller, ``stop`` cancels it. Platform backends
own the cadence (usually the tuning knob
:attr:`VoiceTuningConfig.watchdog_audio_service_poll_s`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice.health.contract import AudioServiceEvent

logger = get_logger(__name__)


class AudioServiceMonitor(Protocol):
    """Platform-agnostic audio-service lifecycle monitor (ADR §4.4.5)."""

    async def start(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        """Begin polling and forward transitions to ``on_event``."""
        ...

    async def stop(self) -> None:
        """Cancel the poller. Must be idempotent."""
        ...


class NoopAudioServiceMonitor:
    """Audio-service monitor that never reports a transition.

    Used on macOS (``coreaudiod`` is managed by launchd), on Linux until
    the Sprint 4 ``dbus``/``systemctl`` backend lands, on Windows when
    ``sc.exe`` is not available, and everywhere when
    ``tuning.voice.runtime_resilience_enabled`` is ``False``.

    Emits a single INFO log entry on the first :meth:`start` so operators
    can diagnose why the audio-service surface is quiet.
    """

    def __init__(self, *, reason: str) -> None:
        self._reason = reason
        self._started = False

    async def start(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        del on_event  # intentionally discarded
        if not self._started:
            logger.info("voice_audio_service_monitor_noop", reason=self._reason)
            self._started = True

    async def stop(self) -> None:
        self._started = False


__all__ = [
    "AudioServiceMonitor",
    "NoopAudioServiceMonitor",
]
