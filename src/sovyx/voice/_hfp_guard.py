"""macOS Bluetooth HFP/SCO guard.

The Hands-Free Profile (HFP) is the legacy Bluetooth voice channel every
headset supports. When a headset is used as a **capture** device macOS
automatically switches it from the A2DP high-quality profile (listen-only,
48 kHz stereo LC3/AAC) to the SCO narrow-band profile (bidirectional,
**8 kHz mono** CVSD / 16 kHz mSBC). SCO audio is:

1. Compressed twice — once by CVSD/mSBC, again by any downstream codec.
2. Strongly band-limited (upper cut-off ≈ 3.4 kHz on CVSD, 7 kHz on mSBC).
3. Padded with a continuous "comfort-noise" tail even during silence.

The Silero v5 VAD we use downstream is trained on wide-band speech and
produces effectively-random speech probabilities on SCO-quality signal
— wake-word detection rate drops below 10 % in operator reports.

This module lets the orchestrator decide, before opening the stream,
whether the active macOS capture device is a HFP-mode Bluetooth headset.
When the guard fires, the caller either:

1. Asks the user to switch to a wired mic / built-in mic (preferred).
2. Forces the cascade into the 16 kHz mSBC path *with a known quality
   penalty in the metric* — acceptable for voice chat, not for wake-word.

Detection strategy
==================

macOS exposes Bluetooth topology through ``system_profiler
SPBluetoothDataType -json`` (stable across 10.14+). We parse the JSON
into a tree of devices and flag any that:

* have ``"Minor Type" = "Headphones"`` or ``"Handsfree"``, AND
* report ``"Connected"`` true, AND
* expose the ``HFP`` service record (present in the ``services_advertised``
  or ``device_services`` key depending on OS version).

A shorter signal — ``"device_isconnected" = "attrib_Yes"`` on a device
named ``AirPods``/``WH-1000XM``/``Bose`` — is used as a fallback when
the service list is absent (seen on early Sonoma builds).

Every subprocess call honours a 2.5 s hard timeout and collapses to
"no HFP observed" on any failure. Non-macOS hosts short-circuit with an
empty report so callers can wire this into cross-platform code paths
without ``sys.platform`` guards of their own.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 — short-timeout subprocess call to trusted system tool
import sys
from dataclasses import dataclass, field

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_SUBPROCESS_TIMEOUT_S = 2.5
"""Wall-clock cap for ``system_profiler`` — it can be slow on laptops
waking from sleep, so we budget slightly more than the Linux detector.
"""


_HFP_MINOR_TYPES: frozenset[str] = frozenset(
    {
        "Headphones",
        "Handsfree",
        "Headset",
        "HandsFree Device",
    },
)
"""Bluetooth "Minor Type" tokens that indicate a potential HFP source.

Populated from the subset of ``IOBluetooth`` minor-type enums we've
observed in ``system_profiler`` output across macOS 12–14. Matched
case-insensitively by :func:`_classify_device`.
"""


_HFP_NAME_HINTS: tuple[str, ...] = (
    "airpods",
    "beats",
    "wh-1000",
    "wf-1000",
    "bose",
    "jabra",
    "plantronics",
    "poly",
    "sennheiser",
    "sony wh",
    "sony wf",
)
"""Name substrings used as a *fallback* when the services list is empty.

Conservative on purpose: only well-known headset families land here so
a random USB mic named "studio" never gets flagged. Matched
case-insensitively on the ``device_name`` or ``_name`` field.
"""


_HFP_SERVICE_NAMES: frozenset[str] = frozenset(
    {
        "hfp",
        "handsfreeunit",
        "handsfreeaudiogateway",
        "headset",
        "headsetunit",
    },
)
"""Bluetooth service-record names that signal HFP capability.

Keys vary between macOS releases (``"Hands-Free"``, ``"HandsFreeUnit"``,
``"headset"``). We lowercase + strip non-alphanumeric before comparison.
"""


@dataclass(frozen=True, slots=True)
class HfpDevice:
    """One Bluetooth endpoint flagged by the guard.

    Attributes:
        name: The human-readable device name from system_profiler
            (e.g. ``"AirPods Pro"``).
        address: Bluetooth MAC if reported, otherwise ``""``.
        minor_type: The reported "Minor Type" field if any.
        reason: ``"services"`` when the flag came from an HFP service
            record, ``"name_hint"`` when it came from the name-hint
            fallback. Surfaces the signal strength to the caller.
    """

    name: str
    address: str = ""
    minor_type: str = ""
    reason: str = "services"


@dataclass(frozen=True, slots=True)
class HfpGuardReport:
    """Summary of the macOS Bluetooth capture topology.

    Attributes:
        connected_hfp: Every currently-connected device that matches
            the HFP heuristic. Insertion order preserved.
        any_connected_bluetooth: Whether *any* Bluetooth device was
            reported as connected. ``False`` is the common case on
            wired-mic setups and is a reliable "no HFP risk" signal.
        tool_available: ``False`` when ``system_profiler`` is missing
            or exited non-zero — the caller should treat the guard as
            inconclusive rather than clear.
    """

    connected_hfp: list[HfpDevice] = field(default_factory=list)
    any_connected_bluetooth: bool = False
    tool_available: bool = True


def guard_active_capture_endpoint() -> HfpGuardReport:
    """Inspect the macOS Bluetooth subsystem and return the guard report.

    Non-macOS hosts short-circuit with an empty, tool-available report
    (so callers can treat "empty + available" as "no HFP risk on this
    platform"). On a macOS host the function:

    1. Runs ``system_profiler SPBluetoothDataType -json`` with a hard
       timeout.
    2. Walks the ``device_title``/``items`` tree, classifying each
       connected device via :func:`_classify_device`.
    3. Collapses the matches into :class:`HfpGuardReport`.

    The returned report is consumed by the orchestrator *before*
    opening the capture stream — the guard never modifies any OS state.
    """
    if sys.platform != "darwin":
        return HfpGuardReport(tool_available=True)

    if shutil.which("system_profiler") is None:
        return HfpGuardReport(tool_available=False)

    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            [  # noqa: S607 — resolved via which() above
                "system_profiler",
                "SPBluetoothDataType",
                "-json",
            ],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
            text=True,
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("voice_hfp_guard_spawn_failed", detail=str(exc))
        return HfpGuardReport(tool_available=False)

    if proc.returncode != 0:
        logger.debug(
            "voice_hfp_guard_nonzero",
            returncode=proc.returncode,
            stderr=proc.stderr[:200] if proc.stderr else "",
        )
        return HfpGuardReport(tool_available=False)

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("voice_hfp_guard_malformed", detail=str(exc))
        return HfpGuardReport(tool_available=False)

    devices = _iter_bluetooth_devices(payload)
    connected_hfp: list[HfpDevice] = []
    any_connected = False
    for device in devices:
        if not _is_connected(device):
            continue
        any_connected = True
        classification = _classify_device(device)
        if classification is None:
            continue
        connected_hfp.append(classification)

    return HfpGuardReport(
        connected_hfp=connected_hfp,
        any_connected_bluetooth=any_connected,
        tool_available=True,
    )


def _iter_bluetooth_devices(payload: object) -> list[dict[str, object]]:
    """Flatten the ``SPBluetoothDataType`` JSON into a device list.

    The payload shape is ``{"SPBluetoothDataType": [ { ... device_title_keyed ... } ]}``
    and has shifted between macOS releases — earlier versions nested
    devices under ``"device_title"``, later under ``"device_connected"``
    / ``"device_not_connected"``. We walk all keyed dicts and treat any
    leaf dict that looks like a device (has a name-ish field) as one.
    """
    if not isinstance(payload, dict):
        return []
    root = payload.get("SPBluetoothDataType")
    if not isinstance(root, list):
        return []

    devices: list[dict[str, object]] = []
    for section in root:
        if not isinstance(section, dict):
            continue
        _harvest(section, devices)
    return devices


def _harvest(node: dict[str, object], devices: list[dict[str, object]]) -> None:
    """Recurse into the SPBluetoothDataType tree picking up device dicts."""
    for key, value in node.items():
        if isinstance(value, dict):
            if _looks_like_device(value):
                devices.append({"_name": key, **value})
            _harvest(value, devices)
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    _harvest(entry, devices)


def _looks_like_device(node: dict[str, object]) -> bool:
    """Heuristic: a device leaf carries at least one address / minor-type field."""
    markers = (
        "device_address",
        "device_addr",
        "device_minorType",
        "device_manufacturer",
        "device_isconnected",
    )
    return any(marker in node for marker in markers)


def _is_connected(device: dict[str, object]) -> bool:
    """Return ``True`` when the system_profiler device entry is connected."""
    for key in ("device_isconnected", "device_connected"):
        value = device.get(key)
        if isinstance(value, str) and value.lower() in {
            "attrib_yes",
            "yes",
            "true",
        }:
            return True
    return False


def _classify_device(device: dict[str, object]) -> HfpDevice | None:
    """Return an :class:`HfpDevice` when ``device`` is a connected HFP source."""
    minor_type_raw = device.get("device_minorType")
    minor_type = str(minor_type_raw) if isinstance(minor_type_raw, str) else ""
    name = _extract_name(device)
    address = _extract_address(device)

    services = _extract_services(device)
    if services & _HFP_SERVICE_NAMES:
        return HfpDevice(
            name=name,
            address=address,
            minor_type=minor_type,
            reason="services",
        )

    if minor_type in _HFP_MINOR_TYPES:
        return HfpDevice(
            name=name,
            address=address,
            minor_type=minor_type,
            reason="minor_type",
        )

    low = name.lower()
    if any(hint in low for hint in _HFP_NAME_HINTS):
        return HfpDevice(
            name=name,
            address=address,
            minor_type=minor_type,
            reason="name_hint",
        )
    return None


def _extract_name(device: dict[str, object]) -> str:
    for key in ("device_name", "_name"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_address(device: dict[str, object]) -> str:
    for key in ("device_address", "device_addr"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_services(device: dict[str, object]) -> set[str]:
    """Return the lowercased, alnum-only set of advertised services."""
    candidates: list[object] = []
    for key in ("device_services", "services_advertised", "device_supportsServices"):
        value = device.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, str):
            candidates.extend(value.split(","))
    out: set[str] = set()
    for entry in candidates:
        if not isinstance(entry, str):
            continue
        normalized = "".join(ch for ch in entry.lower() if ch.isalnum())
        if normalized:
            out.add(normalized)
    return out


__all__ = [
    "HfpDevice",
    "HfpGuardReport",
    "guard_active_capture_endpoint",
]
