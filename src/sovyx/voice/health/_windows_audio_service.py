"""Windows audio service watchdog (WI2).

The Windows ``Audiosrv`` (Windows Audio) and ``AudioEndpointBuilder``
services are the load-bearing kernel-mode-to-user-mode bridge for
capture. When either is in a non-RUNNING state the entire audio stack
is structurally unavailable — no PortAudio open, no MMDevices
enumeration, no sd.InputStream callback. Pre-WI2 the only signal Sovyx
had was "every probe fails for unknown reason"; this module surfaces
the actual cause (which service, which state) so the operator can
``Start-Service Audiosrv`` instead of staring at opaque PortAudio
errors.

Capabilities:

* :func:`query_audio_service_status` — sync snapshot of both
  services + structured verdict.
* :func:`AudioServiceWatchdog` — async rolling-window monitor that
  emits a ``voice.windows.audio_service_degraded`` WARN when either
  service trips a status change away from RUNNING.

Design contract mirrors the F3/F4/WI3 modules:

* Detection NEVER raises — subprocess / parse failures collapse
  into UNKNOWN with structured ``notes``.
* Watchdog is OPT-IN — explicit ``start()`` / ``stop()`` lifecycle.
* Bounded subprocess timeouts so a wedged Service Control Manager
  doesn't stall Sovyx.

Reference: F1 inventory mission task WI2; Microsoft documentation
of the ``sc.exe query`` exit semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted Win SCM binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


# ── Bounds + tunables ─────────────────────────────────────────────


_SC_QUERY_TIMEOUT_S = 3.0
"""Wall-clock budget per ``sc.exe query`` call. Healthy SCM
responds in <50 ms; 3 s is generous enough to absorb a momentarily-
busy SCM while short enough that a wedged service doesn't stall
preflight."""


_DEFAULT_WATCH_INTERVAL_S = 30.0
"""Default rolling poll cadence for :class:`AudioServiceWatchdog`.
30 s is below typical user patience for "voice broken" reports
while above the SCM's own restart back-off cadence — fast enough
to attribute a service trip within one user attempt, slow enough
not to spam the SCM with queries."""


# ── Public types ──────────────────────────────────────────────────


class WindowsServiceState(StrEnum):
    """Closed-set service state vocabulary (matches sc.exe output)."""

    RUNNING = "running"
    STOPPED = "stopped"
    START_PENDING = "start_pending"
    STOP_PENDING = "stop_pending"
    PAUSED = "paused"
    UNKNOWN = "unknown"
    """Probe failed (sc.exe missing, timeout, parse failure)."""

    NOT_FOUND = "not_found"
    """SCM reported the service does not exist (unusual on Windows
    desktop SKUs — would indicate a corrupted install)."""


@dataclass(frozen=True, slots=True)
class WindowsServiceReport:
    """Structured status of a single service."""

    name: str
    """Service name (e.g. ``"Audiosrv"``, ``"AudioEndpointBuilder"``)."""

    state: WindowsServiceState
    """Current service state."""

    raw_state: str = ""
    """Verbatim ``STATE`` line value from sc.exe output. Useful
    for debugging when ``state`` is UNKNOWN."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (subprocess errors, parse fallbacks)."""

    @property
    def is_healthy(self) -> bool:
        """``True`` only when state is RUNNING. Every other state
        (including PENDING) is degraded for capture purposes."""
        return self.state is WindowsServiceState.RUNNING


@dataclass(frozen=True, slots=True)
class AudioServiceStatus:
    """Aggregate status of both audio services."""

    audiosrv: WindowsServiceReport
    audio_endpoint_builder: WindowsServiceReport

    @property
    def all_healthy(self) -> bool:
        return self.audiosrv.is_healthy and self.audio_endpoint_builder.is_healthy

    @property
    def degraded_services(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.audiosrv.is_healthy:
            out.append(self.audiosrv.name)
        if not self.audio_endpoint_builder.is_healthy:
            out.append(self.audio_endpoint_builder.name)
        return tuple(out)


# ── Probe ─────────────────────────────────────────────────────────


_TARGET_SERVICES: tuple[str, ...] = ("Audiosrv", "AudioEndpointBuilder")
"""The two services Sovyx capture depends on. Audiosrv is the
Windows Audio top-level service; AudioEndpointBuilder enumerates
endpoints — both must be RUNNING for any capture path to work."""


def query_audio_service_status() -> AudioServiceStatus:
    """Synchronous snapshot of both audio service states.

    Returns:
        :class:`AudioServiceStatus` carrying per-service reports.
        Never raises — subprocess / parse failures collapse into
        UNKNOWN per service with structured notes.
    """
    if sys.platform != "win32":
        return AudioServiceStatus(
            audiosrv=WindowsServiceReport(
                name=_TARGET_SERVICES[0],
                state=WindowsServiceState.UNKNOWN,
                notes=(f"non-windows platform: {sys.platform}",),
            ),
            audio_endpoint_builder=WindowsServiceReport(
                name=_TARGET_SERVICES[1],
                state=WindowsServiceState.UNKNOWN,
                notes=(f"non-windows platform: {sys.platform}",),
            ),
        )
    sc_path = shutil.which("sc.exe") or shutil.which("sc")
    if sc_path is None:
        msg_note = ("sc.exe binary not found on PATH",)
        return AudioServiceStatus(
            audiosrv=WindowsServiceReport(
                name=_TARGET_SERVICES[0],
                state=WindowsServiceState.UNKNOWN,
                notes=msg_note,
            ),
            audio_endpoint_builder=WindowsServiceReport(
                name=_TARGET_SERVICES[1],
                state=WindowsServiceState.UNKNOWN,
                notes=msg_note,
            ),
        )
    audiosrv = _query_one_service(sc_path, _TARGET_SERVICES[0])
    aeb = _query_one_service(sc_path, _TARGET_SERVICES[1])
    return AudioServiceStatus(audiosrv=audiosrv, audio_endpoint_builder=aeb)


def _query_one_service(sc_path: str, service: str) -> WindowsServiceReport:
    notes: list[str] = []
    try:
        result = subprocess.run(
            (sc_path, "query", service),
            capture_output=True,
            text=True,
            timeout=_SC_QUERY_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"sc query {service} timed out")
        return WindowsServiceReport(
            name=service,
            state=WindowsServiceState.UNKNOWN,
            notes=tuple(notes),
        )
    except OSError as exc:
        notes.append(f"sc spawn failed: {exc!r}")
        return WindowsServiceReport(
            name=service,
            state=WindowsServiceState.UNKNOWN,
            notes=tuple(notes),
        )
    # sc.exe exit codes: 0 = success, 1060 = service not found.
    if result.returncode == 1060:  # noqa: PLR2004
        return WindowsServiceReport(
            name=service,
            state=WindowsServiceState.NOT_FOUND,
            notes=("sc.exe returned 1060 — service does not exist",),
        )
    if result.returncode != 0:
        notes.append(
            f"sc query exited {result.returncode}: {result.stderr.strip()[:120]}",
        )
        return WindowsServiceReport(
            name=service,
            state=WindowsServiceState.UNKNOWN,
            notes=tuple(notes),
        )
    state, raw = _parse_state_from_sc_output(result.stdout)
    if state is WindowsServiceState.UNKNOWN and raw == "":
        notes.append("STATE line not found in sc output")
    return WindowsServiceReport(
        name=service,
        state=state,
        raw_state=raw,
        notes=tuple(notes),
    )


_STATE_TOKENS: dict[str, WindowsServiceState] = {
    "RUNNING": WindowsServiceState.RUNNING,
    "STOPPED": WindowsServiceState.STOPPED,
    "START_PENDING": WindowsServiceState.START_PENDING,
    "STOP_PENDING": WindowsServiceState.STOP_PENDING,
    "PAUSED": WindowsServiceState.PAUSED,
}


def _parse_state_from_sc_output(stdout: str) -> tuple[WindowsServiceState, str]:
    """Extract the STATE token from ``sc query`` output.

    Output format::

        SERVICE_NAME: Audiosrv
                TYPE               : 30  WIN32
                STATE              : 4  RUNNING
                ...

    The STATE line carries an integer code AND a textual token; we
    key on the token for stability."""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("STATE"):
            continue
        # "STATE              : 4  RUNNING" — split on colon then whitespace.
        if ":" not in line:
            continue
        right = line.split(":", 1)[1].strip()
        # Tokens are space-separated; the textual one is the LAST.
        tokens = right.split()
        for token in reversed(tokens):
            upper = token.upper()
            if upper in _STATE_TOKENS:
                return _STATE_TOKENS[upper], right
    return WindowsServiceState.UNKNOWN, ""


# ── Watchdog ──────────────────────────────────────────────────────


class AudioServiceWatchdog:
    """Async rolling-window monitor for the Windows audio services.

    On every ``interval_s`` tick, queries both services. When the
    aggregate status changes from healthy → degraded (or vice
    versa), invokes the configured callback with the new status.
    The callback is invoked AT MOST once per state transition —
    a sustained-degraded condition produces a single notification,
    not one per tick.

    Thread-safety: the watchdog runs as a single asyncio task; the
    callback is awaited in-loop so no concurrency races against
    the orchestrator's other handlers."""

    def __init__(
        self,
        on_state_change: Callable[[AudioServiceStatus], None] | None = None,
        *,
        interval_s: float = _DEFAULT_WATCH_INTERVAL_S,
    ) -> None:
        if interval_s <= 0:
            msg = f"interval_s must be > 0, got {interval_s}"
            raise ValueError(msg)
        self._on_state_change = on_state_change
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._last_healthy: bool | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        """Begin the polling loop. Idempotent."""
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="audio_service_watchdog")

    async def stop(self) -> None:
        """Halt the polling loop. Idempotent."""
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                status = await asyncio.to_thread(query_audio_service_status)
            except Exception as exc:  # noqa: BLE001 — watchdog must keep running
                logger.warning(
                    "voice.windows.audio_service_query_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                status = None
            if status is not None:
                self._maybe_emit_change(status)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    def _maybe_emit_change(self, status: AudioServiceStatus) -> None:
        is_healthy = status.all_healthy
        if self._last_healthy is None:
            # First tick — record but don't emit (no transition yet).
            self._last_healthy = is_healthy
            return
        if is_healthy == self._last_healthy:
            return
        # State changed — emit + record.
        self._last_healthy = is_healthy
        if not is_healthy:
            logger.warning(
                "voice.windows.audio_service_degraded",
                **{
                    "voice.degraded_services": list(status.degraded_services),
                    "voice.audiosrv_state": status.audiosrv.state.value,
                    "voice.audio_endpoint_builder_state": (
                        status.audio_endpoint_builder.state.value
                    ),
                    "voice.action_required": (
                        "Windows audio service is not RUNNING. Run "
                        "`Start-Service Audiosrv AudioEndpointBuilder` from "
                        "an elevated PowerShell, or restart the system. "
                        "Capture is structurally unavailable until both "
                        "services return to RUNNING."
                    ),
                },
            )
        else:
            logger.info(
                "voice.windows.audio_service_recovered",
                **{
                    "voice.audiosrv_state": status.audiosrv.state.value,
                    "voice.audio_endpoint_builder_state": (
                        status.audio_endpoint_builder.state.value
                    ),
                },
            )
        if self._on_state_change is not None:
            try:
                self._on_state_change(status)
            except Exception as exc:  # noqa: BLE001 — callback boundary
                logger.warning(
                    "voice.windows.audio_service_callback_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )


__all__ = [
    "AudioServiceStatus",
    "AudioServiceWatchdog",
    "WindowsServiceReport",
    "WindowsServiceState",
    "query_audio_service_status",
]
