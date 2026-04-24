"""Linux kernel-log scan for audio driver distress signals.

Mirrors :mod:`sovyx.voice.health._driver_watchdog_win` for Linux —
instead of the Windows Kernel-PnP Event Log, we read
``journalctl -k`` (the systemd journal's kernel ring buffer) and
grep for named patterns indicating HDA codec wedges, USB audio
descriptor failures, D3-wake trickle bugs, XRUN floods, and other
known distress signals.

When the scan surfaces events correlated to the device the cascade
is about to probe, the factory integration LOGS the signal loudly
so an operator triaging post-incident can tie the cascade outcome
to a concrete kernel event.

F1 scope is **detection-tier**: scan + log + emit structured
``driver.watchdog.linux.*`` events. The Windows path additionally
downgrades ``voice_clarity_autofix`` to shared-mode on a match —
no symmetric autofix is applied on Linux because PortAudio on
Linux already routes through ALSA shared by default (there is no
equivalent "safer" mode to fall back to). A follow-up sprint may
gate L2.5 preset-apply on a recent wedge, informed by field data
from the detection-tier telemetry this module produces.

Scope:

* **Linux-only.** On every other platform the scan returns an
  empty ``LinuxDriverWatchdogScan`` with ``scan_attempted=False``
  so the caller can invoke it unconditionally.

* **systemd-only.** ``journalctl`` is standard on every systemd
  host; non-systemd distros (Alpine, void with runit, some
  hardened containers) fall back to "not attempted" via the
  subprocess failure path. ``dmesg -T`` as an alternative was
  considered and rejected — the output format is less stable
  across kernel versions and requires CAP_SYSLOG on many
  hardened configs where journalctl runs without it.

* **Best-effort.** A subprocess spawn failure, timeout, or
  unexpected format never blocks boot. The scan returns flags
  distinguishing "clean bill of health" from "unknown" (same
  semantics as the Windows counterpart).

* **Privacy-preserving.** Each event keeps only a truncated
  message excerpt (512 chars), the pattern name, severity, an
  optional device hint, and the kernel timestamp. No process
  info, no user-id, no raw memory addresses.
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


_JOURNALCTL_EXE = "journalctl"
"""Resolved via PATH — matches ``_audio_service_linux`` conventions.
Non-systemd hosts fail the subprocess spawn → scan returns
``scan_attempted=False`` and the caller degrades gracefully."""


_DEFAULT_SUBPROCESS_TIMEOUT_S = 3.0
"""Hard cap on the ``journalctl`` subprocess. Healthy scans return
in 100-400 ms on a laptop with a day of kernel log history; 3 s
catches pathological cases (journal fsync stall, rotated logs
re-index) without stalling boot."""


_MESSAGE_TRUNCATE_CHARS = 512
"""Per-event message slice retained in
:class:`LinuxDriverWatchdogEvent`. Matches the Windows module's
budget so downstream log processors have a consistent envelope."""


_MAX_EVENTS = 100
"""Upper bound on events returned from a single scan.

Attacker-controllable kmsg (rare, requires root or CAP_SYS_ADMIN
on some kernel configs) could flood the journal with matched
patterns. Capping keeps the downstream log + telemetry payload
bounded regardless of input."""


@dataclass(frozen=True, slots=True)
class _Pattern:
    """Named kernel-log distress signal.

    Attributes:
        name: Stable identifier used in logs + telemetry buckets
            (``"hda_codec_timeout"`` etc.). Treat renames as public-
            surface changes — SLO dashboards key on these strings.
        regex: Compiled pattern matched against each journal line.
            Patterns are intentionally narrow (distinct kernel
            subsystem strings) to avoid false positives from
            innocuous messages.
        severity: ``"error"`` for hard-fail signals (codec won't
            respond, descriptor read truly failed), ``"warning"``
            for degraded operation (XRUN, D3 workaround). Operators
            key alert thresholds on severity.
    """

    name: str
    regex: re.Pattern[str]
    severity: Literal["warning", "error"]


_DISTRESS_PATTERNS: tuple[_Pattern, ...] = (
    # HDA codec not responding to commands → driver reload needed,
    # capture opens will return -EIO or block indefinitely.
    _Pattern(
        name="hda_codec_timeout",
        regex=re.compile(
            r"snd_hda_intel.*azx_get_response.*timeout",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # Codec dropped off PCI bus — hard failure, audio subsystem
    # needs a module reload or reboot.
    _Pattern(
        name="hda_codec_disconnected",
        regex=re.compile(
            r"snd_hda_intel.*codec_disconnected=1",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # Generic HDA driver init-time routing failure: no capture pin
    # widget mapped. L2.5 preset apply would be a no-op in this
    # state.
    _Pattern(
        name="hda_no_pin_widget",
        regex=re.compile(
            r"snd_hda_codec_generic.*no pin widget",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # D3-wake trickle bug: codec takes ~500 ms to return from
    # power-save, first ~500 ms of capture is silence. L2.5 heals
    # this via runtime_pm=on on the codec PCI device.
    _Pattern(
        name="hda_irq_timing_workaround",
        regex=re.compile(
            r"snd_hda_intel.*IRQ timing workaround",
            re.IGNORECASE,
        ),
        severity="warning",
    ),
    # USB audio device descriptor read failed — likely cable
    # quality issue or port instability. Device may disconnect
    # intermittently during capture.
    _Pattern(
        name="usb_descriptor_read_fail",
        regex=re.compile(
            r"usb [\d.-]+:.*device descriptor read/64.*error -\d+",
            re.IGNORECASE,
        ),
        severity="error",
    ),
    # USB disconnect events (not necessarily bad alone, but
    # multiple in a short window signals cable/port/hub issues).
    _Pattern(
        name="usb_disconnect",
        regex=re.compile(
            r"usb [\d.-]+:\s+USB disconnect",
            re.IGNORECASE,
        ),
        severity="warning",
    ),
    # ALSA xrun — buffer underrun. One is noise; a flood (caller
    # aggregates by count) signals IRQ contention or a pathological
    # application gobbling the audio thread.
    _Pattern(
        name="alsa_xrun",
        regex=re.compile(
            r"xrun!!!\s*\(at least [\d.]+\s*ms",
            re.IGNORECASE,
        ),
        severity="warning",
    ),
)
"""Pattern catalog — immutable tuple of named regexes.

Ordering is insertion order for stability (tests assert on it).
Adding a new pattern: append at the end, bump the ``name`` only
under a deliberate rename (it flows into telemetry bucket keys
that dashboards consume).
"""


# Per-event device-hint extraction. We look for three distinct
# shapes in the matched line and keep the first one found:
#   * HDA card index / alias    → ``card0`` / ``card[PCH]``
#   * USB bus-port path         → ``1-1.2:1.0`` (interface nodes)
#   * HDA codec vendor:device   → ``0x14f15045``
_DEVICE_HINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcard(\d+|\[[^\]]+\])", re.IGNORECASE),
    re.compile(r"\busb\s+(\d+-[\d.]+(?::\d+\.\d+)?)", re.IGNORECASE),
    re.compile(r"0x([0-9a-f]{8})\b", re.IGNORECASE),
)


# journalctl short-iso format begins every line with the
# timestamp: ``2026-04-24T10:20:30+0000 host kernel: <msg>``.
# We capture the timestamp for per-event ISO carrying.
_JOURNALCTL_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+\S+\s+kernel:\s*(?P<msg>.+?)$",
)


@dataclass(frozen=True, slots=True)
class LinuxDriverWatchdogEvent:
    """Single matched distress signal.

    Attributes:
        kernel_timestamp_iso: Best-effort ISO-8601 timestamp from
            the journalctl line. Empty string when the format
            didn't carry a parseable timestamp (rare — indicates
            a non-systemd journalctl variant or malformed input).
        pattern_name: The matching ``_Pattern.name``. Stable for
            telemetry.
        severity: ``"warning"`` or ``"error"`` — matches the
            pattern's declared severity.
        message_excerpt: The kernel message body, truncated at
            ``_MESSAGE_TRUNCATE_CHARS``. Keeps the per-event log
            envelope bounded.
        device_hint: First device-identity string extracted from
            the message (``card0`` / ``1-1.2`` / hex codec id).
            ``None`` when no identifier could be extracted — the
            event is still reported but won't pass
            :meth:`LinuxDriverWatchdogScan.matches_device` for
            device-specific gating.
    """

    kernel_timestamp_iso: str
    pattern_name: str
    severity: Literal["warning", "error"]
    message_excerpt: str
    device_hint: str | None


@dataclass(frozen=True, slots=True)
class LinuxDriverWatchdogScan:
    """Result of a :func:`scan_recent_linux_driver_watchdog_events`
    call. Shape-compatible with the Windows counterpart so the
    factory can carry both in the same helper matrix.
    """

    events: tuple[LinuxDriverWatchdogEvent, ...] = ()
    scan_attempted: bool = False
    scan_failed: bool = False

    @property
    def any_events(self) -> bool:
        return bool(self.events)

    def matches_device(
        self,
        *,
        alsa_card_id: str | None = None,
        usb_vid_pid: str | None = None,
        codec_vendor_id: str | None = None,
    ) -> bool:
        """Return ``True`` iff any event's ``device_hint`` matches
        one of the provided hardware identities.

        Accepts three orthogonal hint types (an endpoint typically
        has one or two — internal HDA has ``alsa_card_id`` +
        ``codec_vendor_id``; USB has ``alsa_card_id`` +
        ``usb_vid_pid``). Any single match returns ``True``. All
        ``None`` → ``False`` (no hints provided, nothing to match).

        Hint matching is case-insensitive substring — both the
        event's ``device_hint`` and the provided value are lowered
        before comparison. This tolerates surface-level variation
        (``"PCH"`` vs ``"pch"``, ``"1-1.2"`` vs ``"1-1.2:1.0"``).
        """
        needles: list[str] = []
        for hint in (alsa_card_id, usb_vid_pid, codec_vendor_id):
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


async def scan_recent_linux_driver_watchdog_events(
    *,
    lookback_hours: int = 24,
    timeout_s: float = _DEFAULT_SUBPROCESS_TIMEOUT_S,
    journalctl_exe: str = _JOURNALCTL_EXE,
) -> LinuxDriverWatchdogScan:
    """Scan ``journalctl -k`` for the last ``lookback_hours`` hours.

    Args:
        lookback_hours: Window behind now to scan. Default 24 h.
            Longer windows surface more noise; shorter windows
            miss overnight incidents.
        timeout_s: Hard subprocess timeout. Boot path can never
            wait past this; on timeout we return a flag-set
            empty scan.
        journalctl_exe: Path / name of the ``journalctl`` binary.
            Tests substitute a fake so no real journal is read.

    Returns:
        :class:`LinuxDriverWatchdogScan` with the observed events.
        ``scan_attempted=False`` on non-Linux or on subprocess
        spawn failure (systemctl missing). ``scan_failed=True``
        when the subprocess spawned but timed out or produced
        unparseable output.
    """
    if sys.platform != "linux":
        return LinuxDriverWatchdogScan(scan_attempted=False)

    since = f"-{int(lookback_hours)}h"
    argv = [
        journalctl_exe,
        "-k",
        "--no-pager",
        "--since",
        since,
        "--output",
        "short-iso",
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
            "voice_driver_watchdog_linux_spawn_failed",
            error=str(exc),
        )
        return LinuxDriverWatchdogScan(scan_attempted=False)

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
            "voice_driver_watchdog_linux_scan_timeout",
            timeout_s=timeout_s,
        )
        return LinuxDriverWatchdogScan(scan_attempted=True, scan_failed=True)

    if proc.returncode != 0:
        # journalctl prints to stderr when the user lacks
        # permission (no adm / systemd-journal group) or when no
        # journal exists. Either case → no useful signal.
        logger.warning(
            "voice_driver_watchdog_linux_scan_nonzero_exit",
            returncode=proc.returncode,
            stderr=stderr_b.decode("utf-8", errors="replace")[:256],
        )
        return LinuxDriverWatchdogScan(scan_attempted=True, scan_failed=True)

    payload = stdout_b.decode("utf-8", errors="replace")
    events = _parse_events(payload)
    return LinuxDriverWatchdogScan(scan_attempted=True, events=events)


def _parse_events(payload: str) -> tuple[LinuxDriverWatchdogEvent, ...]:
    """Walk ``payload`` line by line, apply the pattern catalog.

    Returns at most ``_MAX_EVENTS`` events, in insertion order
    (oldest first — matches journalctl's default output ordering).
    Lines that don't match the journalctl envelope regex are
    scanned against the distress patterns directly (covers
    `short` format variants without a timestamp prefix).
    """
    out: list[LinuxDriverWatchdogEvent] = []
    for raw_line in payload.splitlines():
        if len(out) >= _MAX_EVENTS:
            break
        line = raw_line.strip()
        if not line:
            continue
        envelope = _JOURNALCTL_LINE_RE.match(line)
        if envelope is not None:
            timestamp = envelope.group("ts")
            message = envelope.group("msg")
        else:
            # Tolerate lines without the expected envelope. We
            # still try the distress patterns against the whole
            # line — a malformed timestamp field is no reason to
            # discard a potential signal.
            timestamp = ""
            message = line
        for pattern in _DISTRESS_PATTERNS:
            if pattern.regex.search(message) is not None:
                out.append(
                    LinuxDriverWatchdogEvent(
                        kernel_timestamp_iso=timestamp,
                        pattern_name=pattern.name,
                        severity=pattern.severity,
                        message_excerpt=message[:_MESSAGE_TRUNCATE_CHARS],
                        device_hint=_extract_device_hint(message),
                    ),
                )
                # Only emit one event per line — if two patterns
                # happen to match the same message, the first in
                # catalog order wins. Avoids double-count inflation
                # on telemetry.
                break
    return tuple(out)


def _extract_device_hint(message: str) -> str | None:
    """First device-identity string found in ``message``, or
    ``None`` when no hint pattern matched.

    Tried in fixed order so results are deterministic across runs.
    HDA card index first (most common shape in audio kernel logs),
    then USB path, then codec vendor hex.
    """
    for pattern in _DEVICE_HINT_PATTERNS:
        match = pattern.search(message)
        if match is not None:
            # group(1) exists on all three patterns — returns the
            # captured identifier (without any surrounding prefix).
            return match.group(1)
    return None


__all__ = [
    "LinuxDriverWatchdogEvent",
    "LinuxDriverWatchdogScan",
    "scan_recent_linux_driver_watchdog_events",
]
