"""Windows Kernel-PnP Driver Watchdog pre-flight scan.

Reads the ``Microsoft-Windows-Kernel-PnP/Driver Watchdog`` event log for
events 900 (long-running transaction detected) and 901 (long-running
transaction completed). When such events exist in the recent past for a
target device, the audio cascade on this machine is about to open a
capture stream against a driver whose event-queue thread is likely
wedged — pushing an exclusive / WDM-KS open to that driver can escalate
into a ``LiveKernelEvent 0x1CC`` and a Kernel-Power 41 hard-reset (see
the v0.20.3 Razer BlackShark V2 Pro post-mortem in
:mod:`~sovyx.voice.health.cascade`).

The pre-flight lets :mod:`~sovyx.voice.health._factory_integration` skip
exclusive-mode attempts on known-fragile hardware: if a watchdog event
exists in the lookback window, the cascade is forced into shared-mode
for this boot and the incident is logged loudly for operator triage.

Scope:

* Windows-only. On every other platform the scan returns an empty
  ``DriverWatchdogScan`` so the cascade's Windows-exclusive logic can
  call into it unconditionally.
* Best-effort. A PowerShell spawn failure, a locale-dependent output
  format, or a subprocess timeout never blocks the boot path — the
  scan returns "no events observed" and logs a warning so we don't
  silently lose the safety net.
* Privacy-preserving. Only event IDs, timestamps, and a truncated
  message prefix are retained; the full driver context is logged at
  DEBUG level for support-time forensics.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_POWERSHELL_EXE = "powershell.exe"
"""Use ``powershell.exe`` (PS 5.1 — always present on Windows 10/11) rather
than ``pwsh.exe`` (PS 7+, user install only)."""

_DEFAULT_SUBPROCESS_TIMEOUT_S = 3.0
"""Hard cap on the PowerShell subprocess — we must never block voice-enable
boot by more than a few seconds for the pre-flight check."""

_MESSAGE_TRUNCATE_CHARS = 512
"""Per-event message slice retained in :class:`DriverWatchdogEvent`. Enough
for forensic grep ("usbaudio", "VID_1532") without bloating logs."""


@dataclass(frozen=True, slots=True)
class DriverWatchdogEvent:
    """Single Kernel-PnP Driver Watchdog event record.

    Attributes:
        event_id: ``900`` (long-running detected) or ``901`` (completed).
        time_created_iso: ISO-8601 UTC timestamp of the event.
        message_excerpt: First ``_MESSAGE_TRUNCATE_CHARS`` characters of
            the event Message body. Contains the PnP device instance
            path — grep'd by callers looking for a specific hardware ID
            (e.g. ``"USB\\VID_1532&PID_0528"``).
    """

    event_id: int
    time_created_iso: str
    message_excerpt: str


@dataclass(frozen=True, slots=True)
class DriverWatchdogScan:
    """Result of a :func:`scan_recent_driver_watchdog_events` call.

    Attributes:
        events: All events observed in the lookback window. Empty when
            the scan succeeded and found nothing, or when the scan
            failed soft and we have no signal to act on.
        scan_attempted: ``True`` when the scan ran (Windows + subprocess
            returned). ``False`` on non-Windows or when the subprocess
            could not be spawned at all. Lets callers distinguish
            "clean bill of health" from "unknown".
        scan_failed: ``True`` when the subprocess was spawned but did
            not produce parseable output (timeout, JSON error, locale
            mismatch). ``scan_attempted=True`` + ``scan_failed=True``
            means the signal is unavailable, not that the machine is
            healthy.
    """

    events: tuple[DriverWatchdogEvent, ...] = ()
    scan_attempted: bool = False
    scan_failed: bool = False

    @property
    def any_events(self) -> bool:
        """``True`` when at least one event was observed."""
        return bool(self.events)

    def matches_device(self, device_interface_name: str) -> bool:
        """Return ``True`` when any event's message references the device.

        Case-insensitive substring match — the PnP device instance path
        in watchdog messages embeds the same hardware enumeration string
        (``USB\\VID_1532&PID_0528\\...``) that the OS uses everywhere,
        so a match on the ``device_interface_name`` is a strong
        hardware-identity signal.

        Empty ``device_interface_name`` always returns ``False``.
        """
        if not device_interface_name:
            return False
        needle = device_interface_name.strip().lower()
        if not needle:
            return False
        return any(needle in ev.message_excerpt.lower() for ev in self.events)


_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$start = (Get-Date).AddHours(-__LOOKBACK__)
$events = Get-WinEvent -FilterHashtable @{
    LogName = 'Microsoft-Windows-Kernel-PnP/Driver Watchdog'
    StartTime = $start
    Id = 900, 901
} -MaxEvents 50 -ErrorAction SilentlyContinue
$out = @()
foreach ($e in $events) {
    $out += [PSCustomObject]@{
        EventId = $e.Id
        TimeCreated = $e.TimeCreated.ToUniversalTime().ToString('o')
        Message = if ($e.Message) {
            $e.Message.Substring(0, [Math]::Min($e.Message.Length, __TRUNCATE__))
        } else { '' }
    }
}
ConvertTo-Json -InputObject $out -Depth 3 -Compress
"""
"""Embedded scan script. ``__LOOKBACK__`` and ``__TRUNCATE__`` are
replaced at call time. Kept inline (not shelled out to a ``.ps1`` file)
so there's no filesystem surface to poison or tamper with; bandit is
happy because we pass the script as a literal ``-Command`` argument with
``stdin`` disabled."""


async def scan_recent_driver_watchdog_events(
    *,
    lookback_hours: int = 24,
    timeout_s: float = _DEFAULT_SUBPROCESS_TIMEOUT_S,
) -> DriverWatchdogScan:
    """Scan the last ``lookback_hours`` of Driver Watchdog events.

    Best-effort — any failure (non-Windows, PowerShell missing,
    subprocess timeout, unexpected output) returns a scan with
    ``scan_attempted`` / ``scan_failed`` flags set so callers can log
    the miss and continue with the default cascade.

    Args:
        lookback_hours: Window behind now to look at. Default 24 h
            matches the user-visible "device has been unstable today"
            window; shorter windows miss overnight incidents, longer
            ones surface noise.
        timeout_s: Hard subprocess timeout. Never block voice-enable
            boot past this — on timeout, return an empty scan with the
            ``scan_failed`` flag set.
    """
    if sys.platform != "win32":
        return DriverWatchdogScan(scan_attempted=False)

    script = _POWERSHELL_SCRIPT.replace("__LOOKBACK__", str(int(lookback_hours))).replace(
        "__TRUNCATE__", str(_MESSAGE_TRUNCATE_CHARS)
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            _POWERSHELL_EXE,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "voice_driver_watchdog_spawn_failed",
            error=str(exc),
        )
        return DriverWatchdogScan(scan_attempted=False)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        with _suppressed(Exception):
            proc.kill()
        with _suppressed(Exception):
            await proc.wait()
        logger.warning(
            "voice_driver_watchdog_scan_timeout",
            timeout_s=timeout_s,
        )
        return DriverWatchdogScan(scan_attempted=True, scan_failed=True)

    if proc.returncode != 0:
        logger.warning(
            "voice_driver_watchdog_scan_nonzero_exit",
            returncode=proc.returncode,
            stderr=stderr_b.decode("utf-8", errors="replace")[:256],
        )
        return DriverWatchdogScan(scan_attempted=True, scan_failed=True)

    payload = stdout_b.decode("utf-8", errors="replace").strip()
    if not payload:
        return DriverWatchdogScan(scan_attempted=True, events=())

    events = _parse_events(payload)
    if events is None:
        return DriverWatchdogScan(scan_attempted=True, scan_failed=True)
    return DriverWatchdogScan(scan_attempted=True, events=events)


def _parse_events(payload: str) -> tuple[DriverWatchdogEvent, ...] | None:
    """Parse PowerShell ``ConvertTo-Json`` output into events.

    Returns ``None`` when the payload is malformed (the caller treats
    that as a scan-failure so operators see the signal is unavailable).
    An empty list is returned as ``()`` — "scan ran, found nothing".
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning(
            "voice_driver_watchdog_parse_failed",
            error=str(exc),
            payload_prefix=payload[:128],
        )
        return None
    # ConvertTo-Json returns a bare object for a single item and a list
    # for multi-item outputs. Normalise both into a list.
    items = raw if isinstance(raw, list) else [raw]
    out: list[DriverWatchdogEvent] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            event_id = int(entry.get("EventId", 0))
            time_created = str(entry.get("TimeCreated", "") or "")
            message = str(entry.get("Message", "") or "")
        except (TypeError, ValueError):
            continue
        if event_id not in (900, 901):
            continue
        out.append(
            DriverWatchdogEvent(
                event_id=event_id,
                time_created_iso=time_created,
                message_excerpt=message[:_MESSAGE_TRUNCATE_CHARS],
            )
        )
    return tuple(out)


class _suppressed:  # noqa: N801 — internal helper, name mirrors contextlib.suppress
    """Minimal ``contextlib.suppress`` re-implementation — slots-friendly."""

    __slots__ = ("_exc",)

    def __init__(self, exc: type[BaseException]) -> None:
        self._exc = exc

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return exc is not None and isinstance(exc, self._exc)


__all__ = [
    "DriverWatchdogEvent",
    "DriverWatchdogScan",
    "scan_recent_driver_watchdog_events",
]
