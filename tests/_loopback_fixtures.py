"""Virtual-loopback detection + pytest fixtures for capture path testing (TS2).

The voice capture path can only be exercised end-to-end against
a real audio device. CI runners typically have no microphone, so
the canonical workaround is a virtual loopback device:

* **Linux** — ``snd-aloop`` kernel module (created via ``modprobe
  snd-aloop``) exposes a paired playback / capture device.
* **macOS** — `BlackHole <https://existential.audio/blackhole/>`_
  is the de-facto virtual audio driver.
* **Windows** — `VB-CABLE <https://vb-audio.com/Cable/>`_ provides
  the equivalent.

This module ships:

* :func:`detect_loopback` — sync probe returning per-OS verdict.
* :func:`requires_loopback` — pytest marker decorator that skips
  the test when no loopback is available.
* :func:`loopback_device_name` — convenience helper returning the
  detected device name (or empty string).

The CI infrastructure changes (loading the kernel module / installing
the macOS / Windows drivers on the runner) are tracked separately —
this module is the consumer-side glue so tests can opt in TODAY
and the CI side can land independently.

Reference: F1 inventory mission task TS2.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted system binaries
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Detection types ──────────────────────────────────────────────


class LoopbackKind(StrEnum):
    """Closed-set vocabulary of supported loopback backends."""

    SND_ALOOP = "snd_aloop"
    """Linux ALSA loopback kernel module."""

    BLACKHOLE = "blackhole"
    """macOS BlackHole virtual audio driver."""

    VB_CABLE = "vb_cable"
    """Windows VB-CABLE virtual audio driver."""

    NONE = "none"
    """No loopback detected — tests requiring real audio must skip."""


@dataclass(frozen=True, slots=True)
class LoopbackReport:
    """Structured loopback-availability report."""

    kind: LoopbackKind
    """Which backend (if any) was detected."""

    available: bool
    """Convenience: ``kind != NONE``."""

    device_name: str = ""
    """Human-readable device identifier (e.g. ``"hw:Loopback,1"``,
    ``"BlackHole 2ch"``, ``"CABLE Output (VB-Audio Virtual Cable)"``).
    Empty string when not available."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes for trace observability."""


# ── Detection ─────────────────────────────────────────────────────


def detect_loopback() -> LoopbackReport:
    """OS-aware loopback detection probe.

    Returns:
        :class:`LoopbackReport` with verdict + diagnostic notes.
        Never raises — subprocess / FS failures collapse into
        NONE with structured notes.
    """
    if sys.platform == "linux":
        return _detect_snd_aloop()
    if sys.platform == "darwin":
        return _detect_blackhole()
    if sys.platform == "win32":
        return _detect_vb_cable()
    return LoopbackReport(
        kind=LoopbackKind.NONE,
        available=False,
        notes=(f"unsupported platform: {sys.platform}",),
    )


def _detect_snd_aloop() -> LoopbackReport:
    """Linux probe — check ``/proc/modules`` for ``snd_aloop``
    AND verify the corresponding ALSA card exists in
    ``/proc/asound/cards``."""
    notes: list[str] = []
    modules_file = Path("/proc/modules")
    if not modules_file.exists():
        notes.append("/proc/modules not found (containerized runner?)")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    try:
        modules_text = modules_file.read_text(encoding="utf-8")
    except OSError as exc:
        notes.append(f"/proc/modules read failed: {exc!r}")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    if "snd_aloop" not in modules_text:
        notes.append("snd_aloop kernel module not loaded")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    # Module loaded — find the card index.
    cards_file = Path("/proc/asound/cards")
    device_name = "hw:Loopback,1"
    if cards_file.exists():
        try:
            cards_text = cards_file.read_text(encoding="utf-8")
            for line in cards_text.splitlines():
                # "<idx> [Loopback ]: Loopback - Loopback" canonical
                if "Loopback" in line and "[" in line:
                    idx = line.split("[", 1)[0].strip()
                    device_name = f"hw:{idx},1"
                    break
        except OSError as exc:
            notes.append(f"/proc/asound/cards read failed: {exc!r}")
    return LoopbackReport(
        kind=LoopbackKind.SND_ALOOP,
        available=True,
        device_name=device_name,
        notes=tuple(notes),
    )


def _detect_blackhole() -> LoopbackReport:
    """macOS probe — check that ``system_profiler SPAudioDataType``
    lists BlackHole. Returns the canonical device name on success.

    BlackHole installs add an audio device with the literal
    ``"BlackHole"`` substring in the device name; we match
    case-insensitively to handle the 2ch / 16ch variants."""
    notes: list[str] = []
    sp = shutil.which("system_profiler")
    if sp is None:
        notes.append("system_profiler binary not found")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    try:
        result = subprocess.run(
            (sp, "SPAudioDataType"),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append("system_profiler SPAudioDataType timed out")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    except OSError as exc:
        notes.append(f"system_profiler spawn failed: {exc!r}")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    if result.returncode != 0:
        notes.append(f"system_profiler exited {result.returncode}")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if "blackhole" in line.lower():
            # Strip trailing colon/whitespace if device-name line.
            name = line.rstrip(":").strip()
            return LoopbackReport(
                kind=LoopbackKind.BLACKHOLE,
                available=True,
                device_name=name,
                notes=tuple(notes),
            )
    notes.append("BlackHole device not present in SPAudioDataType output")
    return LoopbackReport(
        kind=LoopbackKind.NONE,
        available=False,
        notes=tuple(notes),
    )


def _detect_vb_cable() -> LoopbackReport:
    """Windows probe — query the registry for VB-Audio Virtual Cable's
    registered audio endpoint. Returns the canonical device name
    on success.

    VB-CABLE registers under
    ``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Capture``
    with a FriendlyName containing ``"CABLE Output"``."""
    notes: list[str] = []
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        notes.append("winreg unavailable")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    capture_root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, capture_root)
    except OSError as exc:
        notes.append(f"capture root open failed: {exc!r}")
        return LoopbackReport(
            kind=LoopbackKind.NONE,
            available=False,
            notes=tuple(notes),
        )
    try:
        idx = 0
        while True:
            try:
                endpoint_id = winreg.EnumKey(root, idx)
            except OSError:
                break
            idx += 1
            name = _read_endpoint_friendly_name(winreg, root, endpoint_id, notes)
            if name and "cable output" in name.lower():
                return LoopbackReport(
                    kind=LoopbackKind.VB_CABLE,
                    available=True,
                    device_name=name,
                    notes=tuple(notes),
                )
    finally:
        with contextlib.suppress(OSError):
            winreg.CloseKey(root)
    notes.append("VB-CABLE Output endpoint not found")
    return LoopbackReport(
        kind=LoopbackKind.NONE,
        available=False,
        notes=tuple(notes),
    )


def _read_endpoint_friendly_name(
    winreg_mod: object,
    root: object,
    endpoint_id: str,
    notes: list[str],
) -> str | None:
    try:
        ep = winreg_mod.OpenKey(root, endpoint_id)  # type: ignore[attr-defined]
    except OSError as exc:
        notes.append(f"endpoint {endpoint_id} open failed: {exc!r}")
        return None
    try:
        props = winreg_mod.OpenKey(ep, "Properties")  # type: ignore[attr-defined]
    except OSError:
        return None
    try:
        # PKEY_Device_FriendlyName.
        key_name = "{a45c254e-df1c-4efd-8020-67d146a850e0},14"
        value, _ = winreg_mod.QueryValueEx(props, key_name)  # type: ignore[attr-defined]
        return str(value) if value is not None else None
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            winreg_mod.CloseKey(props)  # type: ignore[attr-defined]


# ── Pytest helpers ────────────────────────────────────────────────


def loopback_device_name() -> str:
    """Convenience wrapper returning the detected device name (or
    empty string if no loopback is available)."""
    return detect_loopback().device_name


def requires_loopback(test_func: Callable[..., object]) -> Callable[..., object]:
    """Pytest decorator that skips ``test_func`` when no loopback
    is available on the host.

    Use::

        @requires_loopback
        def test_capture_via_loopback(...):
            ...

    Skip reason names the missing backend so CI logs are
    self-explanatory."""
    report = detect_loopback()
    if report.available:
        return test_func
    skip_reason = (
        f"virtual loopback unavailable on {sys.platform}: "
        f"{', '.join(report.notes) if report.notes else 'no detection signal'}"
    )
    return pytest.mark.skip(reason=skip_reason)(test_func)


__all__ = [
    "LoopbackKind",
    "LoopbackReport",
    "detect_loopback",
    "loopback_device_name",
    "requires_loopback",
]
