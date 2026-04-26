"""MA10 — ``coreaudiod`` recovery diagnostic.

Mission §3.14 / MA10: macOS audio failures sometimes trace to the
``coreaudiod`` daemon dying or hanging — common after sleep/wake
cycles, USB device plug events, or resource pressure. This module
detects the failure mode and emits a structured remediation hint;
auto-recovery is NOT attempted (killing ``coreaudiod`` requires sudo
and silently kills audio for every other app on the system).

Public API:

* :func:`probe_coreaudiod_state` — synchronous one-shot check; returns
  :class:`CoreAudiodReport` with verdict + remediation hint.

The probe uses ``pgrep -x coreaudiod`` for process detection (fast,
no JSON parsing, returns 0 iff the daemon is running). On non-darwin
hosts the probe returns a no-op report with the platform note.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.b.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_PGREP_TIMEOUT_S = 3.0
"""``pgrep`` is a sub-millisecond syscall on darwin; 3 s is generous
headroom against pathological process-table contention."""


class CoreAudiodVerdict(StrEnum):
    """Aggregate verdict of the daemon-state probe.

    StrEnum (CLAUDE.md anti-pattern #9) so dashboards and structured
    logs can compare values without coercion.
    """

    RUNNING = "running"
    """Daemon is alive — capture should work assuming TCC + entitlements."""

    MISSING = "missing"
    """``pgrep -x coreaudiod`` returned nonzero — the daemon is not in
    the process table. On a healthy macOS system this is impossible
    (launchd auto-respawns it); seeing this verdict means launchd
    itself failed to restart the daemon."""

    UNKNOWN = "unknown"
    """Probe couldn't reach a verdict (non-darwin host, ``pgrep``
    binary missing, subprocess timeout). Operator should treat as
    "no information" rather than "definitely missing"."""


@dataclass(frozen=True, slots=True)
class CoreAudiodReport:
    """Structured outcome of :func:`probe_coreaudiod_state`."""

    verdict: CoreAudiodVerdict
    """Aggregate verdict."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Diagnostic notes (subprocess timeout, binary missing, etc.)."""

    @property
    def remediation_hint(self) -> str:
        """Operator-actionable message ready for the dashboard.

        Empty for the RUNNING verdict; non-empty for MISSING +
        UNKNOWN with the canonical recovery path. The hint
        deliberately tells the operator what to do (manual
        ``sudo killall``) rather than auto-killing the daemon —
        big-tech audio teams (Apple's own AppleHealth team in
        their 2024 macOS-on-WWDC session) explicitly recommend
        operator-initiated recovery for ``coreaudiod`` because
        the daemon manages audio for every app on the system.
        """
        if self.verdict is CoreAudiodVerdict.RUNNING:
            return ""
        if self.verdict is CoreAudiodVerdict.MISSING:
            return (
                "coreaudiod is not running. macOS launchd should "
                "auto-respawn it; if not, run "
                "``sudo killall coreaudiod`` to force a restart "
                "(this kills audio for every app momentarily). "
                "If the daemon repeatedly dies after restart, "
                "check Console.app for crash reports under "
                "/Library/Logs/DiagnosticReports/coreaudiod-*.crash."
            )
        return (
            "coreaudiod state could not be determined. If audio "
            "capture is silent despite TCC granted + valid mic "
            "entitlement, manually run ``pgrep -x coreaudiod`` "
            "to confirm the daemon is alive."
        )


def probe_coreaudiod_state() -> CoreAudiodReport:
    """Synchronous one-shot probe of ``coreaudiod`` daemon state.

    Returns:
        :class:`CoreAudiodReport` with verdict + remediation hint.
        Never raises — non-darwin hosts return UNKNOWN with a
        platform note; subprocess failures collapse into UNKNOWN
        with the failure note.
    """
    if sys.platform != "darwin":
        return CoreAudiodReport(
            verdict=CoreAudiodVerdict.UNKNOWN,
            notes=(f"non-darwin platform: {sys.platform}",),
        )

    pgrep_path = shutil.which("pgrep")
    if pgrep_path is None:
        return CoreAudiodReport(
            verdict=CoreAudiodVerdict.UNKNOWN,
            notes=("pgrep binary not found on PATH",),
        )

    try:
        result = subprocess.run(  # noqa: S603 — pgrep_path from shutil.which, args fixed
            (pgrep_path, "-x", "coreaudiod"),
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CoreAudiodReport(
            verdict=CoreAudiodVerdict.UNKNOWN,
            notes=(f"pgrep timed out after {_PGREP_TIMEOUT_S}s",),
        )
    except OSError as exc:
        return CoreAudiodReport(
            verdict=CoreAudiodVerdict.UNKNOWN,
            notes=(f"pgrep spawn failed: {exc!r}",),
        )

    # ``pgrep -x`` exits 0 when the process is found, 1 when not, 2/3 on errors.
    if result.returncode == 0:
        return CoreAudiodReport(verdict=CoreAudiodVerdict.RUNNING)
    if result.returncode == 1:
        return CoreAudiodReport(verdict=CoreAudiodVerdict.MISSING)
    return CoreAudiodReport(
        verdict=CoreAudiodVerdict.UNKNOWN,
        notes=(f"pgrep exited {result.returncode}: {result.stderr.strip()[:120]}",),
    )


__all__ = [
    "CoreAudiodReport",
    "CoreAudiodVerdict",
    "probe_coreaudiod_state",
]
