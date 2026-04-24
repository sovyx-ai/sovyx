"""Per-endpoint stable identifier for macOS audio devices.

Peer of :mod:`sovyx.voice.health._fingerprint_linux`. macOS has no
MMDevices-like registry GUID, but CoreAudio assigns every physical
audio device a **deviceUID** — a string that is stable across reboots
(``"BuiltInMicrophoneDevice"`` for the internal mic,
``"AppleUSBAudioEngine:Razer…:2015-00-00000-0000-0-0:0"`` for USB
devices, ``"BluetoothA2DP_<mac-addr>"`` for BT). That UID is what we
hash into our fingerprint; without it, callers fall back to the
``{surrogate-…}`` SHA which hotplug-reorder invalidates.

Fingerprint shape
=================

* **Built-in endpoints**:
  ``{macos-builtin-BuiltInMicrophoneDevice-input}``
* **USB audio**:
  ``{macos-usb-<vendor>_<product>_<serial>-input}``
* **Bluetooth**:
  ``{macos-bluetooth-<mac-addr-suffix>-input}``
* **Aggregate / virtual (BlackHole, Loopback, Soundflower)**:
  ``{macos-aggregate-<uid-hash>-input}``
* **Insufficient info** (system_profiler missing, JSON malformed,
  device not found): ``None`` — caller falls back to the surrogate
  hash so the pipeline never aborts.

The ``macos-<transport>-`` prefix visually distinguishes this class
from the Windows ``{GUID}`` / Linux ``{linux-…}`` / ``{surrogate-…}``
forms — operators reading logs can tell at a glance which mechanism
produced each record.

Scope
=====

Subprocess-only via ``system_profiler -json SPAudioDataType``,
consistent with Sprint 1B/2 Linux conventions:

* No PyObjC dependency (``pyobjc-framework-CoreAudio`` adds ~20 MB).
* No C-extension for direct CoreAudio calls (``AudioObjectGet…`` via
  ctypes is a future sprint if performance becomes a concern).
* ``system_profiler`` is standard on every macOS install since 10.0;
  it does not require admin privileges and runs in ~500 ms even on
  busy laptops.

Contract
========

* Pure function with a single synchronous entry point.
* Best-effort: any subprocess / JSON error collapses to ``None``.
* Subprocess-injectable: the ``run`` callable defaults to
  :func:`subprocess.run`; tests substitute a pure-Python stub.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 — short-timeout subprocess call to trusted macOS tool
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.voice.device_enum import DeviceEntry

logger = get_logger(__name__)


_SYSTEM_PROFILER_EXE = "system_profiler"
"""Resolved via PATH — ``system_profiler`` lives at
``/usr/sbin/system_profiler`` on every supported macOS release, but
PATH-lookup keeps the module portable across NixOS-on-Mac and other
non-standard installs."""


_SUBPROCESS_TIMEOUT_S = 3.0
"""Hard cap on the ``system_profiler`` subprocess. Healthy calls
return in 300-600 ms on a modern laptop; 3 s catches pathological
cases (pending IOKit lock, Spotlight contention) without blocking
the boot path."""


_TRANSPORT_MAP: dict[str, str] = {
    # CoreAudio transport strings (as emitted by system_profiler)
    # normalised to a short discriminator that lives in the
    # fingerprint prefix. Unknown transports are kept verbatim.
    "coreaudio_device_type_builtin": "builtin",
    "coreaudio_device_type_usb": "usb",
    "coreaudio_device_type_bluetooth": "bluetooth",
    "coreaudio_device_type_bluetooth_le": "bluetooth",
    "coreaudio_device_type_aggregate": "aggregate",
    "coreaudio_device_type_virtual": "virtual",
    "coreaudio_device_type_display_port": "displayport",
    "coreaudio_device_type_hdmi": "hdmi",
    "coreaudio_device_type_airplay": "airplay",
    "coreaudio_device_type_thunderbolt": "thunderbolt",
}
"""system_profiler → short-form transport map.

The short form lands in the fingerprint prefix so dashboards can
filter by transport (``{macos-usb-…}`` vs ``{macos-builtin-…}``)
without parsing the full CoreAudio string.
"""


@dataclass(frozen=True, slots=True)
class _DeviceRecord:
    """Subset of system_profiler fields we care about.

    Attributes:
        name: The user-visible device name (``_name`` key in the JSON).
            Used to match a :class:`DeviceEntry` passed by the caller.
        device_uid: CoreAudio deviceUID — the stable identity string.
            Empty when system_profiler didn't populate it (rare —
            typically aggregates without a registered UID).
        transport: Normalised transport discriminator from
            :data:`_TRANSPORT_MAP`, or the raw CoreAudio string when
            unmapped.
        direction: ``"input"`` | ``"output"`` | ``"duplex"`` derived
            from the presence of ``coreaudio_device_input`` /
            ``coreaudio_device_output`` channel counts.
    """

    name: str
    device_uid: str
    transport: str
    direction: Literal["input", "output", "duplex"]


def compute_macos_endpoint_fingerprint(
    device_entry: DeviceEntry,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    system_profiler_exe: str = _SYSTEM_PROFILER_EXE,
    timeout_s: float = _SUBPROCESS_TIMEOUT_S,
) -> str | None:
    """Return a stable per-endpoint identifier, or ``None`` on failure.

    Args:
        device_entry: PortAudio device record. Only ``name`` is
            consulted; matched case-insensitively against the
            system_profiler ``_name`` field.
        run: Injectable subprocess entry point for tests. Production
            callers pass ``None`` to use the stdlib default.
        system_profiler_exe: Override the tool name. Tests inject a
            fake to guarantee subprocess isolation.
        timeout_s: Hard subprocess timeout.

    Strategy:

    1. Run ``system_profiler -json SPAudioDataType``.
    2. Walk the returned device list to find the entry whose
       ``_name`` matches ``device_entry.name`` (case-insensitive).
    3. Compose ``{macos-<transport>-<identity>-<direction>}``.

    Identity selection (in order of preference):

    * CoreAudio deviceUID when non-empty (the stable kernel-assigned
      identifier — survives reboots, USB hotplug, OS upgrades).
    * Short hash of ``(name, manufacturer)`` otherwise (still
      better than ``canonical_name``-based surrogate — the name
      doesn't change when the device shifts kernel index).
    """
    if sys.platform != "darwin":
        return None

    record = _lookup_device_record(
        target_name=device_entry.name,
        run=run or subprocess.run,
        exe=system_profiler_exe,
        timeout_s=timeout_s,
    )
    if record is None:
        return None

    identity = _choose_identity(record)
    if not identity:
        # Neither deviceUID nor a name/manufacturer fallback could
        # be derived — surrogate is the honest choice.
        return None

    return f"{{macos-{record.transport}-{identity}-{record.direction}}}"


def _lookup_device_record(
    *,
    target_name: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
    exe: str,
    timeout_s: float,
) -> _DeviceRecord | None:
    """Invoke system_profiler and find the matching record."""
    target = target_name.strip().lower()
    if not target:
        return None

    try:
        proc = run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            [exe, "-json", "SPAudioDataType"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        logger.debug(
            "macos_fingerprint_system_profiler_failed",
            detail=str(exc)[:200],
        )
        return None

    if proc.returncode != 0:
        logger.debug(
            "macos_fingerprint_system_profiler_nonzero",
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[:200],
        )
        return None

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug(
            "macos_fingerprint_system_profiler_malformed",
            detail=str(exc)[:200],
        )
        return None

    # The structure is:
    #   {"SPAudioDataType": [ {"_items": [ {device}, ... ]} ]}
    # where the outer list sometimes contains multiple groups
    # (e.g. one per domain on multi-user setups). We flatten.
    items = _flatten_items(payload)
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("_name")
        if not isinstance(name, str):
            continue
        if name.strip().lower() != target:
            continue
        return _build_record(raw)
    return None


def _flatten_items(payload: object) -> list[object]:
    """Return every device dict from a system_profiler payload.

    Tolerates the two shapes observed in the wild:

    * ``{"SPAudioDataType": [{"_items": [...]}]}`` — most common,
      Mac mini / iMac / MacBook builds.
    * ``{"SPAudioDataType": [{device}, {device}, ...]}`` — seen
      on some hackintosh reports where each top-level entry is a
      device dict directly.

    Anything that doesn't match these shapes yields an empty list.
    """
    if not isinstance(payload, dict):
        return []
    groups = payload.get("SPAudioDataType")
    if not isinstance(groups, list):
        return []
    out: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        items = group.get("_items")
        if isinstance(items, list):
            out.extend(items)
        else:
            # Hackintosh / minimal shape — the group IS a device.
            out.append(group)
    return out


def _build_record(raw: dict[str, Any]) -> _DeviceRecord:
    """Extract our subset from one system_profiler device dict."""
    name = str(raw.get("_name", "")).strip()

    device_uid_raw = raw.get("coreaudio_device_id")
    device_uid = str(device_uid_raw).strip() if isinstance(device_uid_raw, str) else ""

    transport_raw = raw.get("coreaudio_device_transport")
    transport_key = str(transport_raw).strip() if isinstance(transport_raw, str) else ""
    transport = _TRANSPORT_MAP.get(transport_key, transport_key or "unknown")

    # direction — presence of non-zero input/output counts.
    inputs = _coerce_int(raw.get("coreaudio_device_input"))
    outputs = _coerce_int(raw.get("coreaudio_device_output"))
    direction: Literal["input", "output", "duplex"]
    if inputs > 0 and outputs > 0:
        direction = "duplex"
    elif inputs > 0:
        direction = "input"
    elif outputs > 0:
        direction = "output"
    else:
        # No channel info — default to "input" (this module is called
        # from the capture boot path; the caller expects capture-shape
        # fingerprints). The ambiguity is rare enough that leaking
        # "input" here is safer than returning None and losing the
        # stable UID.
        direction = "input"

    return _DeviceRecord(
        name=name,
        device_uid=device_uid,
        transport=transport,
        direction=direction,
    )


def _coerce_int(value: object) -> int:
    """Parse a system_profiler channel-count field to int.

    Values in the JSON payload arrive as ``int`` already on modern
    macOS, but older releases occasionally emit ``"2"`` as a string.
    Best-effort — anything unparseable → 0 (treated as "no channels
    in this direction").
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _choose_identity(record: _DeviceRecord) -> str:
    """Return the shortest stable identifier for ``record``.

    Preference order:

    1. ``device_uid`` — CoreAudio kernel-assigned, maximally stable.
       Rewritten to survive hotplug and OS upgrades.
    2. Sanitised device name — not as stable as UID but still
       survives kernel index reshuffle (one-way function from the
       kernel's perspective).
    3. Empty string when neither is available (caller falls back to
       surrogate).

    UIDs are sanitised to a filesystem-safe form: colons, slashes,
    and spaces → underscore; everything else is passed through. Keeps
    log-grep friendly substring search working.
    """
    if record.device_uid:
        return _sanitise(record.device_uid)
    if record.name:
        return _sanitise(record.name)
    return ""


_UID_SANITISE_TRANSLATION = str.maketrans(
    {
        ":": "_",
        "/": "_",
        " ": "_",
        "\t": "_",
        "\n": "_",
        "\r": "_",
        "\\": "_",
        "{": "_",
        "}": "_",
    },
)


def _sanitise(value: str) -> str:
    """Normalise a UID / name into a fingerprint-safe token.

    Cap length at 120 characters to keep log output bounded — a few
    MIDI devices emit extraordinarily long manufacturer-name strings
    that would otherwise dominate the fingerprint.
    """
    cleaned = value.translate(_UID_SANITISE_TRANSLATION).strip("_")
    if len(cleaned) > 120:
        cleaned = cleaned[:120]
    return cleaned


__all__ = [
    "compute_macos_endpoint_fingerprint",
]
