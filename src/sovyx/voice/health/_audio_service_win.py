"""Windows ``audiosrv`` lifecycle monitor (ADR §4.4.5).

Polls the service state through ``sc query audiosrv`` on an asyncio
task. Transitions from Running → Stopped emit
:class:`~sovyx.voice.health.contract.AudioServiceEvent` with kind
``DOWN``; the inverse transition emits ``UP``. The monitor tolerates
flaky polls (subprocess failures, unexpected output) by treating them
as "no change" rather than bouncing the state — a single failed query
must not cascade into a spurious re-probe.

Cadence comes from ``tuning.voice.watchdog_audio_service_poll_s``
(default 2 s). ``sc`` is present on every Windows SKU since Vista so
no external dependency is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._audio_service import (
    AudioServiceMonitor,
    NoopAudioServiceMonitor,
)
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


_DEFAULT_POLL_S = _VoiceTuning().watchdog_audio_service_poll_s
_SC_QUERY_TIMEOUT_S = 3.0
_SC_EXE = "sc.exe"
_SERVICE_NAME = "audiosrv"


def _query_audiosrv_state() -> str | None:
    """Return ``audiosrv`` state or ``None`` when the query failed.

    The caller treats ``None`` as "no change" so a transient ``sc``
    failure never flips the watchdog state. The state string mirrors
    ``sc`` output ("RUNNING", "STOPPED", "STOP_PENDING", etc.).
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [_SC_EXE, "query", _SERVICE_NAME],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SC_QUERY_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("STATE"):
            continue
        # Format: "STATE              : 4  RUNNING"
        parts = line.split()
        if len(parts) >= 4:
            return parts[3].upper()
    return None


class WindowsAudioServiceMonitor:
    """Polls ``audiosrv`` and emits DOWN/UP transitions.

    Args:
        poll_interval_s: Seconds between polls. Defaults to tuning.
        query: Override for the state query (tests inject fakes).
    """

    def __init__(
        self,
        *,
        poll_interval_s: float | None = None,
        query: Callable[[], str | None] | None = None,
    ) -> None:
        self._interval = poll_interval_s if poll_interval_s is not None else _DEFAULT_POLL_S
        if self._interval <= 0:
            msg = f"poll_interval_s must be > 0, got {self._interval}"
            raise ValueError(msg)
        self._query = query or _query_audiosrv_state
        self._task: asyncio.Task[None] | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._run(on_event))
        logger.info(
            "voice_audio_service_monitor_started",
            platform="win32",
            poll_interval_s=self._interval,
        )

    async def stop(self) -> None:
        self._started = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        # Seed the baseline on the first successful poll. Until we
        # observe a Running state we don't fire UP events — otherwise
        # the daemon would flood the watchdog with a spurious UP on
        # startup of every session.
        last_running: bool | None = None
        while self._started:
            state = await asyncio.to_thread(self._query)
            running: bool | None = None if state is None else state == "RUNNING"
            if running is not None and last_running is not None and running != last_running:
                kind = AudioServiceEventKind.UP if running else AudioServiceEventKind.DOWN
                logger.info(
                    "voice_audio_service_transition",
                    kind=kind.value,
                    previous_running=last_running,
                    current_running=running,
                )
                try:
                    await on_event(AudioServiceEvent(kind=kind))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "voice_audio_service_dispatch_failed",
                        exc_info=True,
                    )
            if running is not None:
                last_running = running
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return


def build_windows_audio_service_monitor() -> AudioServiceMonitor:
    """Return a real Windows monitor, or Noop when ``sc.exe`` is absent."""
    # Fast-path probe: if sc.exe isn't on PATH the monitor would never
    # report a transition anyway. Skip the thread entirely.
    probe = _query_audiosrv_state()
    if probe is None:
        logger.warning(
            "voice_audio_service_monitor_unavailable",
            platform="win32",
            reason="sc_query_failed",
        )
        return NoopAudioServiceMonitor(reason="sc.exe unavailable or audiosrv missing")
    return WindowsAudioServiceMonitor()


__all__ = [
    "WindowsAudioServiceMonitor",
    "build_windows_audio_service_monitor",
]
