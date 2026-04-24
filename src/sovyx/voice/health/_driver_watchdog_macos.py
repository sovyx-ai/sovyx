"""macOS coreaudiod distress scan — log show-based detection tier.

Peer of :mod:`sovyx.voice.health._driver_watchdog_linux` and
:mod:`sovyx.voice.health._driver_watchdog_win`. Where Windows reads
the Kernel-PnP Event Log and Linux reads ``journalctl -k``, macOS
reads the unified system log via ``log show --predicate`` scoped to
the ``coreaudiod`` / ``AudioComponentRegistrar`` processes.

Scope
=====

F1 is **detection-tier only** — mirrors the Sprint 1B Linux policy.
macOS has no user-space audio "safer mode" to fall back to (there is
no shared/exclusive dichotomy like Windows WASAPI), so any behaviour
change keyed off the scan would be speculative. The module emits
structured ``voice_driver_watchdog_macos_*`` log records so
post-incident triage can correlate cascade outcomes with concrete
coreaudiod events.

What we look for
================

Curated patterns drawn from observed real-world coreaudiod distress:

* **HAL I/O engine errors** — ``HALS_IOA1Engine`` with ``Err`` status
  (codec lost, IOKit failure).
* **Aggregate device failures** — ``AudioAggregateDevice`` with
  ``Disconnected`` / ``Not Ready`` (virtual device rebuild mid-use).
* **kAudioUnit underruns / overruns** — ``kAudioUnitErr_TooMany
  FramesToProcess`` (buffer glitch under load).
* **IOAudioFamily resource exhaustion** — ``Failed to allocate`` /
  ``resource unavailable`` (kernel audio buffer starvation).
* **USB audio re-enumeration** — ``USBAudio`` with ``Lost device``
  or ``unexpected disconnect``.
* **Input-overload sensor** — ``input overload detected`` (hardware
  AGC saturation — not fatal but voice-capture-relevant).
* **Watchdog reset** — ``coreaudiod watchdog`` / ``watchdog
  timeout`` (daemon restart — the cascade should be aware of this).

Design notes
============

* ``log show --last Nh --predicate ... --style syslog`` keeps the
  payload parser stable across macOS releases — the predicate
  filters in-kernel before output is materialised, so it's cheap
  and deterministic.
* Hard subprocess timeout, hard event cap, privacy-preserving
  truncation — same envelope the Linux module enforces.
* Non-macOS hosts return ``scan_attempted=False`` without touching
  any subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from dataclasses import dataclass
from typing import Literal

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_LOG_EXE = "log"
"""Resolved via PATH — ``log`` lives at ``/usr/bin/log`` on every
macOS since 10.12 (Sierra). PATH-lookup keeps the module portable
across edge configs."""


_DEFAULT_SUBPROCESS_TIMEOUT_S = 5.0
"""Hard cap on the ``log show`` subprocess. Healthy scans return
in 500-1500 ms on a laptop with a day of unified-log traffic; the
5 s cap accommodates the ~2× variance under Spotlight contention
without blocking the boot path indefinitely."""


_MESSAGE_TRUNCATE_CHARS = 512
"""Per-event message slice retained in
:class:`MacosDriverWatchdogEvent`. Matches the Linux/Windows
modules so downstream log processors see a consistent envelope."""


_MAX_EVENTS = 100
"""Upper bound on events returned per scan.

A misbehaving kext can spam coreaudiod with thousands of error
messages in seconds. Capping keeps downstream log + telemetry
bounded regardless of input volume."""


@dataclass(frozen=True, slots=True)
class _Pattern:
    """Named distress signal — stable across log-show format changes.

    Attributes:
        name: Telemetry bucket key. Treat renames as public-surface
            changes; SLO dashboards join on these strings.
        regex: Case-insensitive pattern matched against each log
            line's message body (post envelope strip).
        severity: ``"error"`` for hard-fail signals (codec lost,
            IOAudioFamily starvation), ``"warning"`` for degraded
            operation (xrun-equivalent, input overload).
    """

    name: str
    regex: re.Pattern[str]
    severity: Literal["warning", "error"]


_DISTRESS_PATTERNS: tuple[_Pattern, ...] = (
    # HAL engine error — CoreAudio couldn't talk to the driver.
    _Pattern(
        name="hal_io_engine_error",
        regex=re.compile(
            r"HALS_IOA1Engine.*(?:Err|error|failed)",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # Aggregate device dropped while in use — user-affecting.
    _Pattern(
        name="aggregate_device_disconnected",
        regex=re.compile(
            r"AudioAggregateDevice.*(?:Disconnected|Not\s*Ready|removed)",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # IOAudioFamily resource exhaustion — kernel allocation failed.
    _Pattern(
        name="io_audio_family_allocation_failed",
        regex=re.compile(
            r"IOAudioFamily.*(?:Failed\s*to\s*allocate|resource\s*unavailable)",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # USB audio device lost mid-stream.
    _Pattern(
        name="usb_audio_lost_device",
        regex=re.compile(
            r"USBAudio.*(?:Lost\s*device|unexpected\s*disconnect)",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # coreaudiod watchdog fired — daemon was restarted.
    _Pattern(
        name="coreaudiod_watchdog_reset",
        regex=re.compile(
            r"coreaudiod.*(?:watchdog\s*(?:reset|timeout|fired))",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # Audio Unit buffer overrun — the CoreAudio analogue of Linux xrun.
    _Pattern(
        name="audio_unit_buffer_overrun",
        regex=re.compile(
            r"kAudioUnitErr_TooManyFramesToProcess",
            re.IGNORECASE,
        ),
        severity="warning",
    ),
    # Hardware AGC tripped — input overdriven.
    _Pattern(
        name="input_overload_detected",
        regex=re.compile(
            r"input\s*overload\s*detected",
            re.IGNORECASE,
        ),
        severity="warning",
    ),
)
"""Pattern catalog — immutable tuple of named regexes.

Ordering is insertion order; tests assert on it. Adding a new
pattern: append at the end. Treat ``name`` changes as public-surface
renames (they flow into telemetry buckets + SLO dashboards)."""


# Device-hint extraction patterns. Run in order, first match wins,
# so more-specific patterns come first.
_DEVICE_HINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Explicit CoreAudio deviceUID in quotes.
    re.compile(r'deviceUID["\'\s:=]+([A-Za-z0-9_.:+\-]+)', re.IGNORECASE),
    # USB VID:PID bracketed in log-show output.
    re.compile(r"VendorID=0x([0-9a-f]{4}).*ProductID=0x([0-9a-f]{4})", re.IGNORECASE),
    # Short ``name=...`` hint when UID isn't logged.
    re.compile(r"\bname=([A-Za-z0-9_. \-]+?)(?:\s|$|,)", re.IGNORECASE),
)


# log show --style syslog produces lines like:
#   2026-04-24 10:20:30.123456-0700 0x123456  Default     0x0  456    0    coreaudiod: message body
# Our envelope extractor captures timestamp + process + message body.
_LOG_SHOW_LINE_RE = re.compile(
    r"^(?P<ts>\S+\s+\S+(?:-\d{4})?)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+"
    r"(?P<proc>\S+?):\s*(?P<msg>.+)$",
)
"""Regex for ``log show --style syslog`` lines.

Format reference: Apple TN2124 and ``man log``. macOS has rotated
the log-show format exactly once since macOS 10.12 (added the
nanosecond precision in 10.15) — the captured groups are positional
rather than named after the changing fields, so one parser covers
both variants."""


@dataclass(frozen=True, slots=True)
class MacosDriverWatchdogEvent:
    """Single matched distress signal from ``log show``.

    Attributes:
        kernel_timestamp_iso: Best-effort ISO-8601 timestamp from
            the log-show envelope. Empty string when the format
            didn't carry a parseable timestamp (rare).
        pattern_name: The matching :class:`_Pattern.name`. Stable
            telemetry key.
        severity: ``"warning"`` | ``"error"`` — matches the
            pattern's declared severity.
        message_excerpt: Log message body, truncated at
            :data:`_MESSAGE_TRUNCATE_CHARS`.
        device_hint: First device-identity string extracted from
            the message (deviceUID / USB VID:PID / device name).
            ``None`` when no hint could be extracted — the event
            still reports but won't pass
            :meth:`MacosDriverWatchdogScan.matches_device`.
    """

    kernel_timestamp_iso: str
    pattern_name: str
    severity: Literal["warning", "error"]
    message_excerpt: str
    device_hint: str | None


@dataclass(frozen=True, slots=True)
class MacosDriverWatchdogScan:
    """Result of a :func:`scan_recent_macos_driver_watchdog_events`
    call. Shape-compatible with the Linux/Windows counterparts so
    the factory can carry any of the three in one helper matrix.
    """

    events: tuple[MacosDriverWatchdogEvent, ...] = ()
    scan_attempted: bool = False
    scan_failed: bool = False

    @property
    def any_events(self) -> bool:
        return bool(self.events)

    def matches_device(
        self,
        *,
        device_uid: str | None = None,
        usb_vid_pid: str | None = None,
        device_name: str | None = None,
    ) -> bool:
        """Return ``True`` iff any event's hint matches a provided
        hardware identity.

        Accepts three orthogonal hint types (an endpoint typically
        has one or two — USB devices have ``device_uid`` +
        ``usb_vid_pid``; built-in has ``device_uid`` + ``device_name``).
        All three ``None`` → ``False``.

        Substring match, case-insensitive on both sides, so
        surface-level variation (``"VendorID=0x1532"`` vs ``"1532"``,
        ``"BuiltInMic"`` vs ``"builtinmicrophonedevice"``) doesn't
        drop a valid correlation.
        """
        needles: list[str] = []
        for hint in (device_uid, usb_vid_pid, device_name):
            if hint is None:
                continue
            stripped = hint.strip().lower()
            if stripped:
                needles.append(stripped)
        if not needles:
            return False
        for event in self.events:
            if event.device_hint is None:
                continue
            hay = event.device_hint.lower()
            if any(needle in hay or hay in needle for needle in needles):
                return True
        return False


async def scan_recent_macos_driver_watchdog_events(
    *,
    lookback_hours: int = 24,
    timeout_s: float = _DEFAULT_SUBPROCESS_TIMEOUT_S,
    log_exe: str = _LOG_EXE,
) -> MacosDriverWatchdogScan:
    """Scan ``log show`` for coreaudiod distress over the last window.

    Args:
        lookback_hours: Window behind now to scan. Default 24 h.
        timeout_s: Hard subprocess timeout.
        log_exe: Path / name of the ``log`` binary. Tests override.

    Returns:
        :class:`MacosDriverWatchdogScan` — ``scan_attempted=False``
        on non-macOS or on subprocess spawn failure (``log`` not
        in PATH); ``scan_failed=True`` when the subprocess spawned
        but timed out or exited non-zero.
    """
    if sys.platform != "darwin":
        return MacosDriverWatchdogScan(scan_attempted=False)

    since = f"{int(lookback_hours)}h"
    # Predicate scope: coreaudiod subsystem (covers both the daemon
    # itself and the in-kernel helpers that log under its
    # subsystem). Broader scope (``process IN {coreaudiod, ...}``)
    # adds false positives without improving recall for our catalog.
    predicate = 'subsystem == "com.apple.coreaudio" OR process == "coreaudiod"'
    argv = [
        log_exe,
        "show",
        "--last",
        since,
        "--predicate",
        predicate,
        "--style",
        "syslog",
        "--info",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "voice_driver_watchdog_macos_spawn_failed",
            error=str(exc),
        )
        return MacosDriverWatchdogScan(scan_attempted=False)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_s,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        logger.warning(
            "voice_driver_watchdog_macos_scan_timeout",
            timeout_s=timeout_s,
        )
        return MacosDriverWatchdogScan(scan_attempted=True, scan_failed=True)

    if proc.returncode != 0:
        logger.warning(
            "voice_driver_watchdog_macos_scan_nonzero_exit",
            returncode=proc.returncode,
            stderr=stderr_b.decode("utf-8", errors="replace")[:256],
        )
        return MacosDriverWatchdogScan(scan_attempted=True, scan_failed=True)

    payload = stdout_b.decode("utf-8", errors="replace")
    events = _parse_events(payload)
    return MacosDriverWatchdogScan(scan_attempted=True, events=events)


def _parse_events(payload: str) -> tuple[MacosDriverWatchdogEvent, ...]:
    """Walk ``payload`` line by line, apply the pattern catalog.

    At most :data:`_MAX_EVENTS` events returned, in insertion order
    (oldest first — matches ``log show``'s default output ordering).
    Lines that don't match the envelope regex are still scanned
    against the patterns (covers macOS variants that omit the
    process prefix).
    """
    out: list[MacosDriverWatchdogEvent] = []
    for raw_line in payload.splitlines():
        if len(out) >= _MAX_EVENTS:
            break
        line = raw_line.strip()
        if not line:
            continue
        envelope = _LOG_SHOW_LINE_RE.match(line)
        if envelope is not None:
            timestamp = envelope.group("ts")
            message = envelope.group("msg")
        else:
            timestamp = ""
            message = line
        for pattern in _DISTRESS_PATTERNS:
            if pattern.regex.search(message) is not None:
                out.append(
                    MacosDriverWatchdogEvent(
                        kernel_timestamp_iso=timestamp,
                        pattern_name=pattern.name,
                        severity=pattern.severity,
                        message_excerpt=message[:_MESSAGE_TRUNCATE_CHARS],
                        device_hint=_extract_device_hint(message),
                    ),
                )
                # First-pattern-wins policy — avoids double-counting
                # when two patterns happen to overlap on the same
                # message.
                break
    return tuple(out)


def _extract_device_hint(message: str) -> str | None:
    """First device-identity string found in ``message``, or ``None``.

    Tried in fixed order so results are deterministic across runs.
    For the VID:PID pattern we concatenate the two groups as
    ``<vid>:<pid>`` to match the form used by the caller's
    ``matches_device`` USB substring query.
    """
    for pattern in _DEVICE_HINT_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        groups = match.groups()
        if len(groups) == 2:  # noqa: PLR2004 — VID/PID tuple
            return f"{groups[0]}:{groups[1]}"
        return groups[0].strip()
    return None


__all__ = [
    "MacosDriverWatchdogEvent",
    "MacosDriverWatchdogScan",
    "scan_recent_macos_driver_watchdog_events",
]
