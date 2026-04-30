"""Cross-platform façade — endpoint ID → stable USB fingerprint.

Phase 5 / T5.43 + T5.51 wire-up. The combo store keys entries by
``endpoint_guid`` (Windows: opaque MMDevices GUID; Linux: synthetic
``{linux-...}`` shape). Both shapes drift across replug + firmware
update for the SAME physical USB device — Windows because the GUID
is endpoint-port-specific, Linux when the sysfs path was unreadable
at fingerprint-compute time and the synthetic surrogate hash kicked
in.

This module gives the combo store a single resolver entry point:

    fingerprint = resolve_endpoint_to_usb_fingerprint(endpoint_id)

It returns ``"usb-VVVV:PPPP[-SERIAL]"`` (the canonical T5.43 shape)
or ``None`` when no USB identity can be derived. The caller — the
:mod:`sovyx.voice.health.combo_store` ``ComboStore`` — uses the
fingerprint as a SECOND-CHANCE lookup index when the primary
``endpoint_guid`` lookup misses, recovering the validated combo
across port changes / firmware updates.

Per-platform delegation:

* **Windows** — delegates to
  :func:`sovyx.voice.health._endpoint_fingerprint_win.resolve_endpoint_to_usb_fingerprint`
  which resolves the IMMDevice endpoint ID via COM property store +
  PKEY_Device_InstanceId → PnP device-instance ID → USB descriptor
  parsing.
* **Linux** — the endpoint_guid itself often carries the canonical
  USB shape inline (e.g. ``{linux-usb-1532:0543-0-capture}`` from
  :mod:`sovyx.voice.health._fingerprint_linux`). When it does, we
  parse VID/PID out and reformat as ``"usb-VVVV:PPPP"`` so the
  fingerprint matches Windows-side strings byte-for-byte. When the
  endpoint_guid is the surrogate hash form (sysfs unreadable at
  enumeration time), Linux returns ``None`` and the combo store
  falls back to the existing endpoint-guid-only lookup.
* **macOS** / other — returns ``None`` (T5.43 macOS path activates
  alongside T5.20 IOKit native enumeration; no work to do here).

Best-effort by design: every failure path returns ``None`` rather
than raising. The combo store treats ``None`` as "no fingerprint
match available, fall through to surrogate lookup" so a missing
fingerprint never blocks the cascade.

Threading: synchronous. Windows COM calls block on driver IPC —
async callers MUST wrap invocations in :func:`asyncio.to_thread`
per CLAUDE.md anti-pattern #14. The combo-store call-sites that
currently consume the resolver are themselves already wrapped.
"""

from __future__ import annotations

import re
import sys

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Linux endpoint_guid USB shape, e.g. ``{linux-usb-1532:0543-0-capture}``.
# The enclosing braces are part of the format produced by
# :func:`sovyx.voice.health._fingerprint_linux.compute_endpoint_fingerprint`;
# the inner ``usb-VVVV:PPPP`` is the canonical T5.43 fingerprint
# we want to extract.
_LINUX_USB_GUID_PATTERN = re.compile(
    r"\{linux-usb-([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\b",
)


def resolve_endpoint_to_usb_fingerprint(endpoint_id: str) -> str | None:
    """Resolve an endpoint ID to a stable USB fingerprint.

    Args:
        endpoint_id: Platform-native endpoint identifier — Windows
            IMMDevice ID (``"{0.0.1.00000000}.{guid}"``), Linux
            ``{linux-...}`` synthetic shape, or other platforms.

    Returns:
        ``"usb-VVVV:PPPP[-SERIAL]"`` (lowercase hex, optional serial
        suffix) when the endpoint resolves to a USB device, ``None``
        otherwise. Non-USB endpoints (PCI codecs, virtual loopback,
        Bluetooth A2DP) return ``None`` — by design the combo store
        falls back to its endpoint-GUID-only lookup for those.
    """
    if not endpoint_id:
        return None

    if sys.platform == "win32":
        # Lazy import — keeps the comtypes dependency optional on
        # non-Windows hosts. The win module already gates on
        # comtypes import internally and returns ``None`` when
        # unavailable, with a once-per-process WARN.
        from sovyx.voice.health._endpoint_fingerprint_win import (
            resolve_endpoint_to_usb_fingerprint as _win_resolve,
        )

        return _win_resolve(endpoint_id)

    if sys.platform == "linux":
        return _resolve_linux(endpoint_id)

    # macOS + others — no resolver wired in this façade. T5.20
    # (native CoreAudio enumeration) closes the macOS path.
    return None


def _resolve_linux(endpoint_id: str) -> str | None:
    """Parse a Linux endpoint_guid for an inline USB fingerprint.

    The Linux fingerprint format
    (:mod:`sovyx.voice.health._fingerprint_linux`) embeds the
    canonical T5.43 USB shape directly in the endpoint_guid for
    USB-audio devices: ``{linux-usb-VVVV:PPPP-N-capture}``. We
    pattern-match VID/PID out of that shape and reformat as
    ``"usb-VVVV:PPPP"``. This is byte-equivalent to what
    :func:`sovyx.voice.health._usb_fingerprint.fingerprint_usb_device`
    produces, so cross-port lookups against entries written from
    Windows or from a previous Linux boot match cleanly.

    Returns ``None`` when:

    * The endpoint_guid is the synthetic surrogate hash shape
      (``{surrogate-...}``) — sysfs unreadable at enumerate time,
      no USB info to recover.
    * The endpoint_guid is a PCI/HDA codec
      (``{linux-pci-...}``) — non-USB hardware, fingerprint scheme
      doesn't apply.
    * The shape is malformed (unexpected chars, missing brace).

    Note: the Linux endpoint_guid is itself stable across port
    changes when sysfs IS readable — moving a USB mic between ports
    keeps the same VID/PID, so the synthetic guid stays the same.
    The fingerprint resolver still returns the canonical form so
    cross-platform lookups (Windows entry written, then user moves
    same physical device to a Linux machine) match.
    """
    match = _LINUX_USB_GUID_PATTERN.search(endpoint_id)
    if match is None:
        return None
    vendor = match.group(1).lower()
    product = match.group(2).lower()
    return f"usb-{vendor}:{product}"


__all__ = [
    "resolve_endpoint_to_usb_fingerprint",
]
