"""Linux audio-service lifecycle monitor (ADR §4.4.5 Linux backend).

Mirrors :mod:`sovyx.voice.health._audio_service_win` for Linux:
polls the user-session systemd state of the audio-stack services
and emits :class:`~sovyx.voice.health.contract.AudioServiceEvent`
transitions when the aggregate goes Running ↔ Stopped.

Services watched (all four — any that isn't installed on the host
is excluded at factory time so it doesn't pollute the aggregate):

* ``pipewire.service`` — the PipeWire daemon (audio graph + nodes).
* ``wireplumber.service`` — session manager (routing policy).
* ``pipewire-pulse.service`` — PulseAudio protocol bridge.
* ``pulseaudio.service`` — pure-PA systems (mutually exclusive with
  PipeWire on modern distros; both may exist on edge installs).

The aggregate watch is DOWN iff ANY monitored service is inactive
after we previously saw it active, UP iff every monitored service
is active again after a DOWN. Hysteretic one-shot transitions like
the Windows path — correlated flaps (pipewire → wireplumber) emit
a single DOWN event, not two.

Design decisions (see Sprint 1A plan for rationale):

* **Polling via ``systemctl --user``, not D-Bus.** Zero new deps
  (systemctl is standard on every systemd host), degrades
  gracefully on non-systemd systems (Alpine, Docker without
  session), matches the Windows ``sc`` polling pattern. 2s poll
  latency is acceptable for operator-scale events.

* **User session scope.** Audio services run under the user
  session on Linux desktops. ``systemctl --user`` is the right
  command; headless servers / container hosts without a user
  session fall back to Noop via the factory probe.

* **Injected query + probe callables.** Tests substitute pure
  Python stubs with no subprocess overhead.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.health._audio_service import (
    AudioServiceMonitor,
    NoopAudioServiceMonitor,
)
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


_DEFAULT_POLL_S = _VoiceTuning().watchdog_audio_service_poll_s
"""Cadence re-uses the Windows tuning knob — operators configure a
single value and all platforms honour it. Default 2 s."""


_SYSTEMCTL_TIMEOUT_S = 3.0
"""Hard cap on each ``systemctl`` subprocess. Healthy invocations
complete in single-digit ms; a 3 s cap catches the pathological
"systemd-user is hung" case without stalling the event loop's
to_thread pool."""


_SYSTEMCTL_EXE = "systemctl"
"""Resolved via ``PATH`` like the rest of the toolbelt. No hardcoded
``/usr/bin/systemctl`` because distros vary (NixOS, Guix, Ubuntu
on Flatpak sandboxes). Pattern deliberately matches Windows
``"sc.exe"``."""


_AUDIO_SERVICE_CANDIDATES: tuple[str, ...] = (
    "pipewire.service",
    "wireplumber.service",
    "pipewire-pulse.service",
    "pulseaudio.service",
)
"""Every systemd user unit name that could plausibly host the audio
stack. Candidates that do not exist on the host are excluded at
factory time (:func:`_probe_existing_services`) so the aggregate
state machine only tracks real installations."""


def _query_service_state(service: str) -> str | None:
    """Return the unit's active-state string, or ``None`` on failure.

    ``systemctl --user is-active <svc>`` emits one of:

    * ``"active"`` (running)
    * ``"inactive"`` / ``"failed"`` / ``"activating"`` / ``"deactivating"``
    * ``"unknown"`` (rare — systemctl couldn't decide)

    A ``None`` return means the subprocess itself failed (systemctl
    missing, user bus inaccessible, timeout). Callers treat that as
    "no change" — one transient subprocess failure must not flip the
    watchdog state and trigger a spurious re-cascade. Matches the
    Windows ``_query_audiosrv_state`` semantics.
    """
    try:
        proc = subprocess.run(  # noqa: S603, S607 — fixed argv via PATH, no shell
            [_SYSTEMCTL_EXE, "--user", "is-active", service],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    # ``is-active`` exits non-zero for inactive/failed services but
    # still prints the state on stdout — so we consult stdout
    # regardless of returncode, treating only subprocess failures
    # (captured above) as "unknown".
    state = proc.stdout.strip().splitlines()
    if not state:
        return None
    return state[0].strip() or None


def _probe_existing_services(
    candidates: tuple[str, ...] = _AUDIO_SERVICE_CANDIDATES,
    *,
    query: Callable[[str], str | None] | None = None,
) -> set[str]:
    """Return the subset of ``candidates`` that systemd knows about.

    A service is "known" if :func:`_query_service_state` returns a
    non-``None`` value for it — that covers ``"active"`` (running),
    ``"inactive"`` / ``"failed"`` (unit installed but not running),
    etc. A ``None`` means systemctl itself didn't respond (service
    truly missing, user bus gone, timeout); those are excluded.

    The factory calls this once at startup to decide between the
    real monitor and Noop. An empty return → Noop (non-systemd
    system or no audio stack installed).
    """
    q = query if query is not None else _query_service_state
    found: set[str] = set()
    for service in candidates:
        state = q(service)
        if state is None:
            continue
        # ``"unknown"`` is systemctl's fallback when it cannot
        # determine state. Treat as "service not really installed"
        # to keep the watch set lean.
        if state.lower() == "unknown":
            continue
        found.add(service)
    return found


class LinuxAudioServiceMonitor:
    """Polls the user-session audio services and emits DOWN/UP events.

    Args:
        services_to_monitor: The subset of
            :data:`_AUDIO_SERVICE_CANDIDATES` that exist on this
            host (per :func:`_probe_existing_services`). Empty sets
            are rejected — the factory routes to Noop in that case.
        poll_interval_s: Seconds between poll rounds. Defaults to
            the shared tuning knob so operators configure a single
            value across platforms.
        query: Injectable service-state query — tests substitute a
            pure-Python stub, production defaults to
            :func:`_query_service_state`.
    """

    def __init__(
        self,
        *,
        services_to_monitor: frozenset[str],
        poll_interval_s: float | None = None,
        query: Callable[[str], str | None] | None = None,
    ) -> None:
        if not services_to_monitor:
            msg = "services_to_monitor must be non-empty"
            raise ValueError(msg)
        self._services = services_to_monitor
        self._interval = poll_interval_s if poll_interval_s is not None else _DEFAULT_POLL_S
        if self._interval <= 0:
            msg = f"poll_interval_s must be > 0, got {self._interval}"
            raise ValueError(msg)
        self._query = query if query is not None else _query_service_state
        self._task: asyncio.Task[None] | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        if self._started:
            return
        self._started = True
        self._task = spawn(self._run(on_event), name="voice-audio-service-monitor-linux")
        logger.info(
            "voice_audio_service_monitor_started",
            platform="linux",
            poll_interval_s=self._interval,
            services=sorted(self._services),
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
        """Poll every service each tick; emit transitions on the
        AGGREGATE ``all running`` boolean.

        Like Windows, we seed the baseline on the first successful
        poll — no UP event fires on startup. Only state transitions
        relative to a prior confirmed reading generate events, so
        correlated failures (pipewire goes down → wireplumber goes
        down ~200 ms later) yield a single DOWN, not a per-service
        flood.
        """
        last_all_running: bool | None = None
        while self._started:
            current_all_running = await self._poll_aggregate()
            if (
                current_all_running is not None
                and last_all_running is not None
                and current_all_running != last_all_running
            ):
                kind = (
                    AudioServiceEventKind.UP if current_all_running else AudioServiceEventKind.DOWN
                )
                logger.info(
                    "voice_audio_service_transition",
                    kind=kind.value,
                    previous_running=last_all_running,
                    current_running=current_all_running,
                    platform="linux",
                )
                # Canonical log mirrors the Windows path so SLO
                # dashboards can union-query the
                # ``audio.service.restarted`` topic across platforms.
                logger.warning(
                    "audio.service.restarted",
                    **{
                        "voice.service": ",".join(sorted(self._services)),
                        "voice.up": current_all_running,
                        "voice.previous_running": last_all_running,
                        "voice.platform": "linux",
                    },
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
            if current_all_running is not None:
                last_all_running = current_all_running
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    async def _poll_aggregate(self) -> bool | None:
        """Return the aggregate ``all-services-active`` state.

        ``None`` → at least one query failed this round. The caller
        treats that as "no change" so a transient systemctl blip
        doesn't bounce the state. Matches the Windows semantics:
        err on the side of silence.
        """
        states = await asyncio.gather(
            *(asyncio.to_thread(self._query, svc) for svc in self._services),
        )
        # If any query returned None, we can't reason about the
        # aggregate this round — preserve the prior state.
        if any(state is None for state in states):
            return None
        return all(state == "active" for state in states)


def build_linux_audio_service_monitor(
    *,
    query: Callable[[str], str | None] | None = None,
    poll_interval_s: float | None = None,
) -> AudioServiceMonitor:
    """Return a real monitor, or Noop when systemd-user is unavailable.

    Fast-path probes which of the candidate audio services exist on
    this host. An empty result means one of:

    * Non-systemd distro (Alpine, void, gentoo-openrc).
    * Container without a user bus (``systemd-logind`` absent).
    * No audio stack installed.

    In any of those cases the real monitor would never observe a
    transition, so we skip the poll loop entirely.

    ``query`` is injectable so the daemon's boot sequence can
    verify Noop behaviour in CI without needing a real systemctl.
    """
    existing = _probe_existing_services(query=query)
    if not existing:
        logger.warning(
            "voice_audio_service_monitor_unavailable",
            platform="linux",
            reason="no_systemd_audio_services",
            probed=list(_AUDIO_SERVICE_CANDIDATES),
        )
        return NoopAudioServiceMonitor(
            reason="systemd-user has no audio service installed or bus unavailable",
        )
    return LinuxAudioServiceMonitor(
        services_to_monitor=frozenset(existing),
        poll_interval_s=poll_interval_s,
        query=query,
    )


__all__ = [
    "LinuxAudioServiceMonitor",
    "build_linux_audio_service_monitor",
]
