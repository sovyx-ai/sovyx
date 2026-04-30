"""Stable USB-audio device fingerprinting (Phase 5 / T5.43).

PortAudio's device index + name combo is NOT stable across:

* Replug — index changes; name often gains/loses an enumeration
  suffix (``USB Mic`` → ``USB Mic (2)``).
* Firmware update — same physical device, name unchanged, but
  PortAudio internal device hash changes because the descriptor
  bytes shifted by one or two bytes.
* OS reboot — index assignment is order-of-discovery; a different
  USB plug order produces different indices.

The combo store relies on a stable identifier for the cache key.
Pre-T5.43 it used a SHA over ``(name, host_api, sample_rate,
channels)`` — robust across reboot but NOT across firmware update
(the device claims a slightly different sample rate after the
update). The combo entry then becomes a stale "hit" for the new
firmware AND a permanent miss for the old one.

T5.43 fingerprint shape: ``"usb-{vendor:04x}:{product:04x}-{serial}"``
where vendor/product are the 16-bit USB descriptor IDs and
serial is the iSerial string descriptor. When iSerial is missing
(class-compliant generic mics often omit it), falls back to
``"usb-{vendor:04x}:{product:04x}"`` — still survives replug + most
firmware updates, just doesn't disambiguate two identical mics.

Cross-platform implementation:

* **Linux**: read ``/sys/class/sound/cardN/usb_id`` and ``/sys/...``
  device tree. Class-compliant USB audio always exposes these
  via the kernel's USB subsystem.
* **Windows**: parse the PnP device-instance ID format
  ``USB\\VID_xxxx&PID_xxxx\\<serial>`` returned by SetupDiGetDeviceProperty.
* **macOS**: read IOKit USB device registry via PyObjC (deferred —
  Phase 5 macOS block T5.1-T5.20 ships the IOKit infrastructure
  this would consume; T5.43's macOS path closes alongside).

This module ships the Linux + Windows paths; macOS path returns
``None`` and falls back to the existing PortAudio surrogate hash.
The macOS implementation activates when T5.20 (native CoreAudio
device enumeration) lands.

Best-effort: every probe path returns ``None`` on failure rather
than raising. The combo store treats ``None`` as "no USB
fingerprint available, use the surrogate" so absent USB info
never blocks combo-store lookups.
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# USB device-instance-path regex matching Windows + Linux usb_id
# format. Both expose the (vendor, product) pair as 4-hex-digit
# uppercase tokens; the regex tolerates both the
# ``USB\VID_xxxx&PID_xxxx`` Windows shape and the ``xxxx:xxxx``
# Linux ``/sys/.../idVendor`` + ``idProduct`` shape via two
# parallel patterns checked in order.
_WIN_PNP_PATTERN = re.compile(
    r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(?:\\([^\\]+))?",
)
_LINUX_USB_ID_PATTERN = re.compile(
    r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$",
)


def fingerprint_usb_device(
    *,
    pnp_device_id: str | None = None,
    sysfs_card_path: Path | None = None,
) -> str | None:
    """Return a stable USB fingerprint or ``None`` when unavailable.

    Callers pass EITHER the Windows PnP device ID OR the Linux
    sysfs card path (e.g. ``/sys/class/sound/card1``). Both
    branches probe for a (vendor, product, serial) tuple and
    format it identically so combo-store keys stay
    cross-platform-compatible.

    Args:
        pnp_device_id: Windows PnP device-instance ID
            (``USB\\VID_1532&PID_0528\\...``). When provided,
            wins over ``sysfs_card_path`` — Windows callers
            don't have a sysfs path to offer.
        sysfs_card_path: Linux sysfs card directory. Read at
            most one level deep to extract ``id`` (vendor:product)
            + ``serial`` from the parent USB device.

    Returns:
        Fingerprint string ``"usb-VVVV:PPPP[-SERIAL]"`` (lowercase
        hex, optional serial suffix), OR ``None`` when no USB
        info could be extracted (non-USB device, missing files,
        permission denial, etc.).
    """
    if pnp_device_id:
        result = _from_windows_pnp(pnp_device_id)
        if result is not None:
            return result

    if sysfs_card_path is not None:
        result = _from_linux_sysfs(sysfs_card_path)
        if result is not None:
            return result

    return None


def _from_windows_pnp(device_id: str) -> str | None:
    """Parse a Windows PnP device-instance ID into a fingerprint."""
    match = _WIN_PNP_PATTERN.search(device_id)
    if match is None:
        return None
    vendor = match.group(1).lower()
    product = match.group(2).lower()
    serial = match.group(3)
    # Some PnP IDs use a placeholder like ``5&xxxx&0&...`` for the
    # serial when iSerial is absent; the leading ``&`` segment is
    # not a real serial. Strip those so the fingerprint stays
    # stable across the same device reattaching to a different
    # USB hub (which alters the placeholder).
    if serial and "&" in serial:
        serial = None
    if serial:
        return f"usb-{vendor}:{product}-{serial}"
    return f"usb-{vendor}:{product}"


def _from_linux_sysfs(card_path: Path) -> str | None:
    """Extract a fingerprint from a Linux sysfs card directory.

    Walks up from ``/sys/class/sound/cardN`` to the parent USB
    device node and reads ``idVendor``, ``idProduct``, ``serial``
    (when present). The walk is bounded to ``_SYSFS_PARENT_HOPS``
    levels so a non-USB card (PCI codec) terminates without an
    infinite loop.

    Returns:
        Fingerprint string OR ``None`` when:
        * Card path doesn't exist (caller bug or hot-unplug race).
        * Card isn't on a USB bus (PCI / virtual / loopback).
        * Sysfs files unreadable (permission denial, sandbox).
    """
    if sys.platform != "linux":
        return None
    if not card_path.exists():
        return None

    # First try the cheap ``card/usb_id`` shortcut some kernels
    # provide ("xxxx:xxxx" format).
    quick = card_path / "usb_id"
    if quick.is_file():
        try:
            content = quick.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            content = ""
        match = _LINUX_USB_ID_PATTERN.match(content)
        if match is not None:
            vendor = match.group(1).lower()
            product = match.group(2).lower()
            serial = _read_sysfs_serial(card_path)
            if serial:
                return f"usb-{vendor}:{product}-{serial}"
            return f"usb-{vendor}:{product}"

    # Fall back to walking parents until we find a USB device
    # node carrying ``idVendor`` + ``idProduct``.
    return _walk_sysfs_parents(card_path)


_SYSFS_PARENT_HOPS = 6
"""Maximum sysfs hops to walk upward looking for a USB device
node. 6 covers cardN → device → usbN.M → usbN → usb_host
even on deep hub topologies; beyond that we're past the USB
controller and a PCI device would never expose idVendor."""


def _walk_sysfs_parents(start: Path) -> str | None:
    """Walk up to ``_SYSFS_PARENT_HOPS`` parents looking for USB IDs."""
    current = start.resolve() if start.exists() else start
    for _ in range(_SYSFS_PARENT_HOPS):
        vendor_path = current / "idVendor"
        product_path = current / "idProduct"
        if vendor_path.is_file() and product_path.is_file():
            try:
                vendor = vendor_path.read_text(encoding="ascii", errors="ignore").strip().lower()
                product = product_path.read_text(encoding="ascii", errors="ignore").strip().lower()
            except OSError:
                return None
            if not (
                re.fullmatch(r"[0-9a-f]{4}", vendor) and re.fullmatch(r"[0-9a-f]{4}", product)
            ):
                return None
            serial_path = current / "serial"
            serial: str | None = None
            if serial_path.is_file():
                try:
                    serial = serial_path.read_text(encoding="ascii", errors="ignore").strip()
                except OSError:
                    serial = None
            if serial:
                return f"usb-{vendor}:{product}-{serial}"
            return f"usb-{vendor}:{product}"
        parent = current.parent
        if parent == current:
            # Hit /; no USB ancestor found.
            return None
        current = parent
    return None


def _read_sysfs_serial(card_path: Path) -> str | None:
    """Best-effort serial read from a sysfs card path.

    Tries ``card_path / "device" / "serial"`` first (covers most
    USB audio cards), then walks up via :func:`_walk_sysfs_parents`'s
    parent search if that fails. Returns ``None`` on any read
    failure or when the file simply isn't there (the common case
    for class-compliant generic USB audio).
    """
    try_paths = [
        card_path / "device" / "serial",
        card_path / "serial",
    ]
    for path in try_paths:
        if path.is_file():
            try:
                serial = path.read_text(encoding="ascii", errors="ignore").strip()
            except OSError:
                continue
            if serial:
                return serial
    return None
