"""Linux hardware detection for the L2.5 ``MixerSanitySetup``.

Populates :class:`HardwareContext` from ``/sys`` + ``/proc`` +
``/etc/os-release`` + XDG socket probes — **no subprocess, no root,
no dmidecode**. Every helper is side-effect-free and returns
``None`` on any failure; the orchestrator handles partial detection
gracefully (a ``None`` field simply skips the corresponding KB glob
match).

Why filesystem-only
===================

* ``dmidecode`` requires ``CAP_SYS_RAWIO`` (root) on most distros —
  the Sovyx daemon runs unprivileged per invariant I7. The same
  vendor/product strings are exposed under ``/sys/class/dmi/id/*``
  as world-readable files on every mainstream kernel since 4.x, so
  we read them directly.
* ``lspci`` covers the PCI controller (``8086:51c8``) but not the
  codec behind the HDA controller (``14F1:5045`` for the VAIO pilot).
  The codec identity lives in ``/proc/asound/card*/codec#*`` —
  read-only, world-readable, authoritative.
* Session-manager detection via ``pactl info`` / ``pgrep`` is heavy
  and racy. XDG runtime sockets (``$XDG_RUNTIME_DIR/pipewire-0``,
  ``.../pulse/native``) are the authoritative presence signal on
  every systemd + Wayland distro of the last five years.

Non-Linux hosts receive an all-``None`` context with
``driver_family="unknown"`` so the KB match trivially returns no
candidate — L2.5 defers, cascade proceeds unchanged.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
from pathlib import Path
from typing import Literal

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import HardwareContext

logger = get_logger(__name__)


_PROC_ASOUND: Path = Path("/proc/asound")
_DMI_PATH: Path = Path("/sys/class/dmi/id")
_OS_RELEASE_PATH: Path = Path("/etc/os-release")


_VENDOR_ID_RE = re.compile(r"^Vendor Id:\s*0x([0-9a-fA-F]{8})\s*$")
"""Matches the ``Vendor Id`` line in ``/proc/asound/card*/codec#*``.

The 32-bit value packs vendor (high 16 bits) + device (low 16 bits);
e.g., ``0x14f15045`` → vendor=0x14F1 (Conexant), device=0x5045
(SN6180). The L2.5 KB keys profiles by the ``VVVV:DDDD`` string
form (uppercase hex, colon-separated) to match ``hda_codec.c``
convention.
"""


async def detect_hardware_context(
    *,
    proc_asound: Path | None = None,
    dmi_path: Path | None = None,
    os_release_path: Path | None = None,
    xdg_runtime_dir: Path | None = None,
    kernel_release: str | None = None,
) -> HardwareContext:
    """Build a :class:`HardwareContext` by reading live system state.

    Every path is injectable so tests can substitute a ``tmp_path``
    fixture without touching the real system — the production call
    site passes no kwargs and each helper uses the module-level
    default.

    All subprocess dependencies are avoided. Every helper is
    best-effort: on any read error it returns ``None`` and the
    caller ships a partial :class:`HardwareContext`. Invariant:
    this function NEVER raises. It may return
    ``HardwareContext(driver_family="unknown")`` on a system whose
    audio stack is entirely undetectable (non-Linux, stripped
    container, ...) — the L2.5 orchestrator handles that gracefully
    by deferring.

    Args:
        proc_asound: Path to ``/proc/asound``. Tests inject a fake
            directory tree.
        dmi_path: Path to ``/sys/class/dmi/id``.
        os_release_path: Path to ``/etc/os-release``.
        xdg_runtime_dir: Directory where PipeWire / PulseAudio
            sockets live. Defaults to ``$XDG_RUNTIME_DIR`` from the
            environment, or ``None`` if unset (audio_stack then
            returns ``None``).
        kernel_release: Override for the ``uname -r`` string.
            Defaults to :func:`platform.uname().release`.

    Returns:
        A :class:`HardwareContext` whose fields reflect the best
        available evidence. At minimum the ``driver_family`` is
        always set (``"unknown"`` fallback).
    """
    proc_asound_ = proc_asound if proc_asound is not None else _PROC_ASOUND
    dmi_path_ = dmi_path if dmi_path is not None else _DMI_PATH
    os_release_path_ = os_release_path if os_release_path is not None else _OS_RELEASE_PATH
    xdg_runtime_dir_ = _resolve_xdg_runtime_dir(xdg_runtime_dir)

    # Every helper is thread-safe + non-blocking — we still wrap
    # them in to_thread so a pathological NFS-backed /sys doesn't
    # stall the event loop.
    codec_id = await asyncio.to_thread(_detect_codec_id, proc_asound_)
    driver_family = await asyncio.to_thread(_detect_driver_family, proc_asound_)
    system_vendor = await asyncio.to_thread(_read_dmi_field, dmi_path_, "sys_vendor")
    system_product = await asyncio.to_thread(
        _read_dmi_field,
        dmi_path_,
        "product_name",
    )
    distro = await asyncio.to_thread(_read_distro, os_release_path_)
    audio_stack = await asyncio.to_thread(_detect_audio_stack, xdg_runtime_dir_)
    kernel = kernel_release if kernel_release is not None else _detect_kernel_release()

    try:
        return HardwareContext(
            driver_family=driver_family,
            codec_id=codec_id,
            system_vendor=system_vendor,
            system_product=system_product,
            distro=distro,
            audio_stack=audio_stack,
            kernel=kernel,
        )
    except ValueError as exc:
        # Extremely defensive: if a field validator somehow rejected
        # our detected value (shouldn't happen — the helpers return
        # only strings matching the contract's Literal sets), fall
        # back to a pure-"unknown" context so the cascade never
        # aborts on detection.
        logger.warning(
            "voice_hardware_detector_fallback_unknown",
            detail=str(exc)[:200],
        )
        return HardwareContext(driver_family="unknown")


def _resolve_xdg_runtime_dir(override: Path | None) -> Path | None:
    if override is not None:
        return override
    value = os.environ.get("XDG_RUNTIME_DIR")
    # Paranoid-QA R2 LOW #4: strip then truthy-check. A sysadmin
    # who accidentally exported ``XDG_RUNTIME_DIR="   "`` (shell
    # heredoc with stray whitespace) would otherwise send
    # ``Path("   ")`` into the socket probe, which then fails
    # silently with "no such file" rather than gracefully falling
    # back to the "no xdg, try /run/user/$(id -u)" path. Treat
    # whitespace-only as unset.
    if not value or not value.strip():
        return None
    return Path(value)


def _detect_codec_id(proc_asound: Path) -> str | None:
    """Scan ``/proc/asound/card*/codec#*`` for an HDA codec Vendor Id.

    Returns the first codec found on any card, in ``"VVVV:DDDD"``
    uppercase-hex form. ``None`` when no card has a codec file, or
    no codec file has a parseable ``Vendor Id`` line. Multi-codec
    systems are rare on laptops; the first codec is typically the
    internal capture chip which is what L2.5 cares about.
    """
    try:
        if not proc_asound.is_dir():
            return None
    except OSError:
        return None
    try:
        card_dirs = sorted(proc_asound.glob("card*"))
    except OSError:
        return None
    for card_dir in card_dirs:
        try:
            codec_files = sorted(card_dir.glob("codec#*"))
        except OSError:
            continue
        for codec_file in codec_files:
            codec_id = _parse_codec_vendor_id(codec_file)
            if codec_id is not None:
                return codec_id
    return None


def _parse_codec_vendor_id(codec_file: Path) -> str | None:
    try:
        text = codec_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        match = _VENDOR_ID_RE.match(line.strip())
        if match is None:
            continue
        try:
            packed = int(match.group(1), 16)
        except ValueError:
            return None
        vendor = (packed >> 16) & 0xFFFF
        device = packed & 0xFFFF
        return f"{vendor:04X}:{device:04X}"
    return None


def _detect_driver_family(
    proc_asound: Path,
) -> Literal["hda", "sof", "usb-audio", "bt", "unknown"]:
    """Heuristic driver-family detection based on ``/proc/asound`` shape.

    * ``codec#*`` file under ``card*/`` → HDA (covers ALSA's
      ``snd_hda_*`` driver family; the codec file is the
      canonical marker).
    * ``card*/id`` containing ``"USB"`` (case-insensitive) →
      ``usb-audio``.
    * Otherwise → ``"unknown"``. SOF is NOT auto-detected in F1
      (the SOF role table lands in F2); a SOF system with an
      HDA-compatible codec file will be detected as ``"hda"``
      which is fine for the resolver fallback. BT capture
      (``bluealsa``) is deferred to F2.
    """
    try:
        if not proc_asound.is_dir():
            return "unknown"
    except OSError:
        return "unknown"
    try:
        card_dirs = sorted(proc_asound.glob("card*"))
    except OSError:
        return "unknown"
    saw_hda = False
    saw_usb = False
    for card_dir in card_dirs:
        try:
            codec_files = any(card_dir.glob("codec#*"))
        except OSError:
            codec_files = False
        if codec_files:
            saw_hda = True
        id_file = card_dir / "id"
        try:
            card_id = id_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            card_id = ""
        if "usb" in card_id.lower():
            saw_usb = True
    if saw_hda:
        return "hda"
    if saw_usb:
        return "usb-audio"
    return "unknown"


def _read_dmi_field(dmi_path: Path, field: str) -> str | None:
    """Read a single ``/sys/class/dmi/id/<field>`` entry.

    Strips trailing whitespace and returns ``None`` when the file
    is missing, empty, or unreadable. The common failure is a
    containerised system (Docker, Flatpak) where ``/sys/class/dmi``
    is masked — graceful ``None`` keeps the orchestrator running.

    Paranoid-QA R2 LOW #3: NFKC-normalise the result so the
    downstream glob match in :func:`~sovyx.voice.health._mixer_kb.matcher.score_profile`
    is stable across decomposed-vs-composed accent encodings.
    BIOS vendors have historically emitted both — a Unicode
    contributor authoring a KB profile with ``Motherhood s.r.o.``
    NFC shouldn't fail to match DMI-reported ``Motherhood s.r.o.``
    NFD. Also strips non-breaking spaces (U+00A0) which NFKC
    converts to plain spaces.
    """
    path = dmi_path / field
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not value:
        return None
    import unicodedata  # noqa: PLC0415 — lazy to keep the import graph minimal

    return unicodedata.normalize("NFKC", value)


def _read_distro(os_release_path: Path) -> str | None:
    """Read ``/etc/os-release`` and return ``"<id>-<version_id>"``.

    Matches the KB convention (``"linuxmint-22.2"``,
    ``"ubuntu-24.04"``, ``"fedora-40"``). Returns just the ID when
    VERSION_ID is absent (rolling distros — Arch, NixOS). ``None``
    when the file is missing or lacks an ID key.
    """
    try:
        text = os_release_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    kv: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        kv[key.strip()] = value.strip().strip("'\"")
    distro_id = kv.get("ID")
    if not distro_id:
        return None
    version = kv.get("VERSION_ID")
    if version:
        return f"{distro_id}-{version}"
    return distro_id


def _detect_audio_stack(
    xdg_runtime_dir: Path | None,
) -> Literal["pipewire", "pulseaudio", "alsa"] | None:
    """Classify the userspace audio stack via XDG runtime sockets.

    Order of precedence (matches what a PortAudio open would see):

    1. ``<XDG_RUNTIME_DIR>/pipewire-0`` → ``"pipewire"``
       (PipeWire exposes this socket whether or not it emulates
       PulseAudio on top).
    2. ``<XDG_RUNTIME_DIR>/pulse/native`` → ``"pulseaudio"``
       (pure PulseAudio without PipeWire).
    3. Neither → ``"alsa"`` when ``/proc/asound/pcm`` exists
       (implying a raw ALSA playback path is available).
    4. Otherwise → ``None`` (couldn't classify).
    """
    if xdg_runtime_dir is not None:
        pipewire_sock = xdg_runtime_dir / "pipewire-0"
        pulse_sock = xdg_runtime_dir / "pulse" / "native"
        try:
            if pipewire_sock.exists():
                return "pipewire"
        except OSError:
            pass
        try:
            if pulse_sock.exists():
                return "pulseaudio"
        except OSError:
            pass
    # Fallback: if ALSA's /proc interface is live, call it ALSA.
    try:
        if (_PROC_ASOUND / "pcm").exists():
            return "alsa"
    except OSError:
        return None
    return None


def _detect_kernel_release() -> str | None:
    try:
        release = platform.uname().release
    except (OSError, AttributeError):
        return None
    return release or None


__all__ = [
    "detect_hardware_context",
]
