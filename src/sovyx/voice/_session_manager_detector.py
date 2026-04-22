"""Linux session-manager grab detector.

Diagnoses the "another audio client is holding ``hw:X,Y``" pathology
that powers :class:`~sovyx.voice._capture_task.CaptureDeviceContendedError`
from the outside. Runs as a ``sovyx doctor linux_session_manager_grab``
subcommand and feeds the ``/api/voice/capture-diagnostics`` endpoint
so operators can tell *before* hitting "Enable voice" whether the
mic is actually free.

Detection strategy (in order):

1. ``pactl list source-outputs`` — preferred. Returns the set of apps
   currently capturing PulseAudio-compatible sources (covers
   PipeWire-pulse too). Fast (< 200 ms typical), machine-parseable,
   attributable to process ID + name.
2. ``/proc/*/fd/*`` scan — fallback. Walks process file descriptors
   looking for open links to ``/dev/snd/pcm*C*`` (capture) nodes.
   Bounded by :attr:`VoiceTuningConfig.detector_proc_max_scan` and
   :attr:`VoiceTuningConfig.detector_proc_timeout_s` — we cap the
   scan rather than iterate every PID on a machine with thousands.
3. ``"unavailable"`` — both methods failed (no pactl, no /proc, or
   both timed out). The detector returns ``has_grab=None`` and the
   caller renders a "could not determine" state.

Never raises. Outermost ``try/except Exception`` wraps both the
subprocess and the /proc scan; a broken detector must never block
the dashboard or break ``sovyx doctor``.

Security posture:

* ``subprocess.run`` is called with ``shell=False`` + a literal arg
  list ``["pactl", "list", "source-outputs"]`` (no user input ever
  reaches argv).
* ``/proc`` access is read-only via :mod:`os` primitives. No write
  ever happens.
* All network, filesystem, and subprocess boundaries return bounded
  output (``evidence`` truncated to
  :attr:`VoiceTuningConfig.detector_evidence_max_chars`).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import VoiceTuningConfig

logger = get_logger(__name__)


DetectionMethod = Literal["pactl", "proc_fd_scan", "unavailable"]


@dataclass(frozen=True)
class ProcessInfo:
    """Single process holding a capture source.

    ``name`` is populated opportunistically from ``/proc/<pid>/comm``
    (always 16 chars max on Linux) or the ``application.name`` /
    ``application.process.binary`` pactl fields. May be ``""`` when
    neither source yielded a value.
    """

    pid: int
    name: str = ""


@dataclass(frozen=True)
class SessionManagerGrabReport:
    """Outcome of :func:`detect_session_manager_grab`.

    Attributes:
        has_grab: ``True`` — at least one process is capturing on a
            source Sovyx would fight for. ``False`` — no contention
            detected. ``None`` — inconclusive (detection tooling
            absent or both methods failed / timed out).
        grabbing_processes: Best-effort list of processes responsible
            for the grab. May be empty even when ``has_grab`` is
            ``True`` (e.g. pactl returned one block but name parsing
            failed — we still know *something* is there).
        detection_method: Which path actually produced the report.
        evidence: Truncated human-readable fragment of the underlying
            data (parsed pactl section, ``/proc`` hit paths, or a
            failure string). Bounded by
            :attr:`VoiceTuningConfig.detector_evidence_max_chars`.
    """

    has_grab: bool | None
    grabbing_processes: tuple[ProcessInfo, ...] = ()
    detection_method: DetectionMethod = "unavailable"
    evidence: str = ""
    # Internal — ``field`` so equality comparisons work but the field
    # doesn't clutter the repr.
    _platform: str = field(default_factory=lambda: sys.platform, repr=False)


async def detect_session_manager_grab(
    *,
    tuning: VoiceTuningConfig,
) -> SessionManagerGrabReport:
    """Return a :class:`SessionManagerGrabReport` for the current host.

    Linux-only — returns ``has_grab=None`` on other platforms (Windows
    has its own WASAPI exclusive-contention detection; macOS has no
    equivalent mechanism).

    Fast by design: ``pactl`` runs with a 2 s wall-clock ceiling
    (tunable via :attr:`VoiceTuningConfig.detector_pactl_timeout_s`)
    and ``/proc`` scans bail at ``detector_proc_max_scan`` PIDs or
    ``detector_proc_timeout_s``, whichever comes first. The function
    is safe to call on every dashboard render — median cost on a
    laptop with PipeWire is ~30 ms.

    Args:
        tuning: Effective :class:`VoiceTuningConfig`.

    Returns:
        :class:`SessionManagerGrabReport`. Never raises.
    """
    if sys.platform != "linux":
        return SessionManagerGrabReport(
            has_grab=None,
            detection_method="unavailable",
            evidence=f"detector is Linux-only; running on {sys.platform}",
        )

    try:
        # Method 1 — pactl.
        pactl_report = await _detect_via_pactl(tuning=tuning)
        if pactl_report is not None:
            return pactl_report

        # Method 2 — /proc/*/fd/* scan.
        proc_report = await _detect_via_proc(tuning=tuning)
        if proc_report is not None:
            return proc_report

        # Method 3 — both unavailable.
        return SessionManagerGrabReport(
            has_grab=None,
            detection_method="unavailable",
            evidence=(
                "pactl not in PATH or returned non-zero; /proc scan yielded no hits or timed out"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — detector must never raise
        evidence = str(exc)[: tuning.detector_evidence_max_chars]
        logger.debug("session_manager_detector_unexpected_error", exc_info=True)
        return SessionManagerGrabReport(
            has_grab=None,
            detection_method="unavailable",
            evidence=f"detector raised: {evidence}",
        )


async def _detect_via_pactl(
    *,
    tuning: VoiceTuningConfig,
) -> SessionManagerGrabReport | None:
    """Run ``pactl list source-outputs`` and parse the response.

    Returns ``None`` when pactl is absent / times out / returns
    non-zero — the caller falls through to the /proc scan.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,  # noqa: S603 — args are a literal list, shell=False
            ["pactl", "list", "source-outputs"],
            capture_output=True,
            text=True,
            timeout=tuning.detector_pactl_timeout_s,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        logger.debug("session_manager_detector_pactl_not_in_path")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(
            "session_manager_detector_pactl_timeout",
            timeout_s=tuning.detector_pactl_timeout_s,
        )
        return None
    except Exception:  # noqa: BLE001 — any subprocess failure → fallback
        logger.debug("session_manager_detector_pactl_failed", exc_info=True)
        return None

    if result.returncode != 0:
        return None

    stdout = (result.stdout or "").strip()
    if not stdout:
        return SessionManagerGrabReport(
            has_grab=False,
            detection_method="pactl",
            evidence="pactl returned 0 source-outputs",
        )

    processes = _parse_pactl_source_outputs(stdout)
    evidence = stdout[: tuning.detector_evidence_max_chars]
    return SessionManagerGrabReport(
        has_grab=True,
        grabbing_processes=tuple(processes),
        detection_method="pactl",
        evidence=evidence,
    )


_PACTL_SECTION_SEPARATOR = re.compile(r"\n\s*\n")
_PACTL_PROCESS_ID_PATTERN = re.compile(
    r'application\.process\.id\s*=\s*"(\d+)"',
    re.IGNORECASE,
)
_PACTL_APP_NAME_PATTERN = re.compile(
    r'application\.(?:name|process\.binary)\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)


def _parse_pactl_source_outputs(stdout: str) -> list[ProcessInfo]:
    """Extract ``(pid, name)`` pairs from pactl section output.

    Best-effort: a section without a ``application.process.id`` is
    skipped (we never invent a PID). Multiple matches for
    ``application.name`` in one section keep the first.
    """
    out: list[ProcessInfo] = []
    seen_pids: set[int] = set()
    for section in _PACTL_SECTION_SEPARATOR.split(stdout):
        pid_match = _PACTL_PROCESS_ID_PATTERN.search(section)
        if pid_match is None:
            continue
        try:
            pid = int(pid_match.group(1))
        except ValueError:
            continue
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        name_match = _PACTL_APP_NAME_PATTERN.search(section)
        name = name_match.group(1) if name_match is not None else ""
        out.append(ProcessInfo(pid=pid, name=name))
    return out


async def _detect_via_proc(
    *,
    tuning: VoiceTuningConfig,
) -> SessionManagerGrabReport | None:
    """Scan ``/proc/*/fd/*`` for open handles to ``/dev/snd/pcm*C*``.

    The ``*C*`` suffix distinguishes capture nodes from playback
    (``*P*``). We never follow into the kernel device directly —
    :func:`os.readlink` reads the symlink target without opening the
    underlying PCM. Safe + cheap.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None

    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(_scan_proc_fds, proc_root, tuning),
            timeout=tuning.detector_proc_timeout_s,
        )
    except TimeoutError:
        logger.debug(
            "session_manager_detector_proc_timeout",
            timeout_s=tuning.detector_proc_timeout_s,
        )
        return SessionManagerGrabReport(
            has_grab=None,
            detection_method="proc_fd_scan",
            evidence="proc fd scan timed out",
        )
    except Exception:  # noqa: BLE001
        logger.debug("session_manager_detector_proc_failed", exc_info=True)
        return None

    return report


_PCM_CAPTURE_PATTERN = re.compile(r"/dev/snd/pcmC\d+D\d+c")


def _scan_proc_fds(
    proc_root: Path,
    tuning: VoiceTuningConfig,
) -> SessionManagerGrabReport:
    """Synchronous /proc scan — wrapped in :func:`asyncio.to_thread`."""
    grabbing: list[ProcessInfo] = []
    evidence_bits: list[str] = []
    scanned = 0

    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        if scanned >= tuning.detector_proc_max_scan:
            evidence_bits.append(f"(scan capped at {scanned} PIDs)")
            break
        scanned += 1

        fd_dir = pid_dir / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue

        for fd_entry in entries:
            try:
                target = os.readlink(fd_entry)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            if _PCM_CAPTURE_PATTERN.search(target):
                try:
                    pid = int(pid_dir.name)
                except ValueError:
                    continue
                comm = _read_proc_comm(pid_dir)
                grabbing.append(ProcessInfo(pid=pid, name=comm))
                evidence_bits.append(f"pid={pid} {comm!r} → {target}")
                break

    evidence = "; ".join(evidence_bits)[: tuning.detector_evidence_max_chars]
    if grabbing:
        return SessionManagerGrabReport(
            has_grab=True,
            grabbing_processes=tuple(grabbing),
            detection_method="proc_fd_scan",
            evidence=evidence,
        )
    return SessionManagerGrabReport(
        has_grab=False,
        detection_method="proc_fd_scan",
        evidence=evidence or f"scanned {scanned} PIDs, no PCM capture handles",
    )


def _read_proc_comm(pid_dir: Path) -> str:
    """Return the 16-char ``comm`` for a PID, or ``""`` on any failure."""
    try:
        with (pid_dir / "comm").open(encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except (PermissionError, FileNotFoundError, OSError):
        return ""


__all__ = [
    "DetectionMethod",
    "ProcessInfo",
    "SessionManagerGrabReport",
    "detect_session_manager_grab",
]
