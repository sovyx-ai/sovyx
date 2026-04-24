"""Per-endpoint stable identifier for Linux audio devices.

The platform baseline for endpoint identity — Windows uses the
MMDevices registry GUID (see
:func:`sovyx.voice._apo_detector.detect_capture_apos`). Linux has no
equivalent kernel-level GUID store, so without this module the fallback
at :func:`sovyx.voice.health._factory_integration.derive_endpoint_guid`
emits a ``{surrogate-...}`` SHA256 over
``(canonical_name, host_api_name, platform)``. The surrogate is
stable across a single install, but two sources of drift break it:

1. A hot-plug reorders ALSA card indices (``hw:0,0`` is now the USB
   headset, not the internal mic). ``canonical_name`` on
   ``"hw:0,0"``-style PCM names then changes across reboots.

2. PipeWire / PulseAudio version upgrades can change the default
   name generation scheme, invalidating every surrogate in
   :class:`~sovyx.voice.health.combo_store.ComboStore`.

The real stable identity on Linux lives in sysfs: the PCI bus /
device / function address (BDF) for onboard codecs, or the USB
vendor:product pair for USB-audio devices. These are kernel-assigned
per physical hardware and do not shift across reboots (PCI BDF is
SMBIOS-determined; USB VID:PID is in the device descriptor).

Fingerprint shape
=================

* **Internal HDA codec**:
  ``{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}``
* **USB-audio device**:
  ``{linux-usb-1532:0543-0-capture}``
* **Insufficient info** (name is ``"default"`` / ``"pulse"`` / sysfs
  unreadable / card not mappable): ``None`` — the caller falls back
  to the surrogate hash so the pipeline never aborts on a missing
  fingerprint.

The ``linux-`` prefix visually distinguishes this class of ID from
both the Windows ``{GUID}`` form and the ``{surrogate-...}``
fallback — operators reading logs can tell at a glance which
identity mechanism produced the record.

Scope
=====

F1 deliberately avoids the ``pw-cli`` / ``pactl`` subprocess route
even though PipeWire and PulseAudio expose per-node properties that
could enrich the fingerprint. The sysfs + ``/proc/asound`` path
covers every realistic hardware configuration without introducing
new optional dependencies or subprocess overhead. A follow-up may
add PipeWire native bindings when the subprocess cost is justified
by a concrete bug the sysfs path can't resolve.

Contract
========

* Pure function (single sync entry point, no I/O on the event loop).
* Best-effort: any read error logs at DEBUG and returns ``None``.
* Path-injectable: ``proc_asound`` and ``sysfs_class_sound`` default
  to live system paths; tests substitute ``tmp_path`` fixtures.
* No subprocess, no network, no environment reads (beyond sysfs /
  procfs which are memory-backed by the kernel).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.voice.device_enum import DeviceEntry


SymlinkResolver = "Callable[[Path], Path]"
"""Signature for the sysfs-symlink resolver.

Production uses :meth:`pathlib.Path.resolve` strictly; tests inject
a fake that returns a pre-constructed Path with the PCI BDF /
USB bus segments embedded — without needing the underlying
filesystem to support colons in directory names (Windows dev
hosts). Keeps the production code path unchanged while enabling
cross-platform unit tests.
"""


def _default_resolve_symlink(path: Path) -> Path:
    """Real sysfs resolver — used at production call sites."""
    return path.resolve(strict=True)


logger = get_logger(__name__)


_PROC_ASOUND = Path("/proc/asound")
"""Kernel-exported ALSA runtime state.

``/proc/asound/cards`` lists every registered card as
``<index> [<id>]: <driver> - <short-name>`` blocks.
``/proc/asound/card<N>/id`` repeats the card id as a single line.
``/proc/asound/card<N>/pcm<M><c|p>/info`` carries per-PCM metadata
including the stream direction.
"""


_SYSFS_CLASS_SOUND = Path("/sys/class/sound")
"""udev-managed symlink tree for sound-subsystem devices.

``/sys/class/sound/card<N>/device`` is a symlink back to the
underlying bus device — PCI (``/sys/devices/pci0000:00/0000:00:1f.3``)
for onboard codecs, USB (``/sys/devices/pci.../usb.../...``) for
USB-audio. The symlink target encodes the kernel-stable address.
"""


# ALSA PCM name patterns supported by the parser.
#
# Examples from live systems:
#   hw:0,0                           → card 0, device 0
#   hw:CARD=PCH,DEV=0                → card id "PCH", device 0
#   plughw:CARD=PCH,DEV=0            → ditto with plug conversion
#   front:CARD=PCH,DEV=0             → ditto, "front" routing PCM
#   sysdefault:CARD=PCH              → card id "PCH", device unspecified (0)
#   dsnoop:CARD=PCH,DEV=0            → dsnoop plugin on PCH
#   pch                              → bare card id (ALSA short name)
#
_HW_INDEX_RE = re.compile(r"^(?:plug)?hw:(?P<index>\d+)(?:,(?P<dev>\d+))?$")
_HW_ID_RE = re.compile(
    r"^[\w-]+:CARD=(?P<id>[^,\s]+)(?:,DEV=(?P<dev>\d+))?$",
    re.IGNORECASE,
)
_BARE_ID_RE = re.compile(r"^(?P<id>[A-Za-z][A-Za-z0-9_-]*)$")


# PCI BDF in canonical form ``<domain>:<bus>:<device>.<function>``
# e.g. ``0000:00:1f.3`` — 4 hex domain, 2 hex bus, 2 hex device,
# 1 hex function. Match group captures the whole BDF so downstream
# normalisation can slash/dash it without re-parsing.
_PCI_BDF_RE = re.compile(
    r"/(?P<bdf>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/?",
    re.IGNORECASE,
)


_CARD_HEADER_RE = re.compile(
    r"^\s*(?P<index>\d+)\s*\[(?P<id>[^\]]+)\]:\s*(?P<driver>\S+)\s*-\s*(?P<name>.+)$",
)
"""Mirrors ``_linux_mixer_probe._CARD_HEADER_RE`` for parsing
``/proc/asound/cards``. Kept as a module-private copy rather than
imported so this file has no dependency on the mixer-probe module
(they live on independent call paths and the probe is Linux-only
plumbing)."""


_VIRTUAL_DEVICE_NAMES: frozenset[str] = frozenset(
    {
        "default",
        "sysdefault",
        "pulse",
        "pipewire",
        "jack",
        "null",
        "samplerate",
        "surround21",
        "surround40",
        "surround41",
        "surround50",
        "surround51",
        "surround71",
        "front",
        "rear",
        "center_lfe",
        "side",
        "iec958",
        "hdmi",
        "modem",
        "phoneline",
    }
)
"""ALSA PCM aliases that do NOT correspond to a specific physical
card. A DeviceEntry with one of these names gives us no stable
hardware identity; the caller falls back to the surrogate.

``"default"`` in particular is the system-wide default which can be
mapped to any card via ``~/.asoundrc`` or PulseAudio/PipeWire
routing — picking a fingerprint for it would give inconsistent
identity across boots.
"""


class _CardLookup(NamedTuple):
    """Result of parsing a DeviceEntry name into card coordinates.

    Attributes:
        card_index: Kernel-assigned card number (0, 1, 2, ...). Unstable
            across hotplug, but stable within a single boot; used only
            to index into ``/proc/asound`` and ``/sys/class/sound``.
        pcm_device: PCM device number within the card (0-indexed). The
            same card can expose multiple PCM devices (HDA has PCM0 for
            analog, PCM3 for HDMI, etc.); ``None`` means "unspecified,
            assume 0 for capture".
    """

    card_index: int
    pcm_device: int | None


def compute_linux_endpoint_fingerprint(
    device_entry: DeviceEntry,
    *,
    proc_asound: Path | None = None,
    sysfs_class_sound: Path | None = None,
    resolve_symlink: Callable[[Path], Path] | None = None,
) -> str | None:
    """Return a stable per-hardware identifier, or ``None`` on failure.

    Walks, in order:

    1. Parse ``device_entry.name`` (ALSA PCM name) into a
       ``(card_index, pcm_device)`` pair via three pattern matchers
       (``hw:N,M``, ``<plugin>:CARD=id,DEV=M``, bare card-id).
    2. Read ``/sys/class/sound/card<N>/device`` symlink and extract
       the PCI BDF or USB VID:PID from the target path.
    3. For HDA cards, read the codec Vendor:Device from
       ``/proc/asound/card<N>/codec#0`` — enriches the fingerprint
       with chip identity (useful across codec-family variations).
    4. Determine stream direction from ``/proc/asound/card<N>/pcm<M><c|p>``.
    5. Compose ``{linux-<bus>-<identity>-<pcm>-<direction>}``.

    Any step that can't produce its share returns ``None``; the
    caller (``derive_endpoint_guid``) then emits the surrogate.

    Args:
        device_entry: The PortAudio device record. Only ``name`` and
            ``max_input_channels`` / ``max_output_channels`` are
            consulted.
        proc_asound: Override for ``/proc/asound`` root. Tests pass
            a ``tmp_path`` fixture; production passes ``None`` to use
            :data:`_PROC_ASOUND`.
        sysfs_class_sound: Override for ``/sys/class/sound``.
    """
    proc = proc_asound if proc_asound is not None else _PROC_ASOUND
    sysfs = sysfs_class_sound if sysfs_class_sound is not None else _SYSFS_CLASS_SOUND
    resolver = resolve_symlink if resolve_symlink is not None else _default_resolve_symlink

    lookup = _parse_device_name(device_entry.name, proc=proc)
    if lookup is None:
        # Name is virtual, malformed, or references an unknown card —
        # no stable identity extractable.
        return None

    bus_identity = _read_bus_identity(lookup.card_index, sysfs=sysfs, resolver=resolver)
    if bus_identity is None:
        # Card exists in /proc/asound but /sys/class/sound has no
        # entry (unusual — typically means the kernel module exposes
        # the card via a non-standard path). Surrogate is the honest
        # choice.
        return None

    # HDA codec ID enriches the fingerprint but is optional; USB /
    # other bus types skip this step.
    codec_id: str | None = None
    if bus_identity.bus == "pci":
        codec_id = _read_hda_codec_id(lookup.card_index, proc=proc)

    # Direction: derived from PortAudio channel counts (authoritative —
    # these are the channels the caller will actually use). /proc PCM
    # info is a cross-check but the PA counts reflect the intended
    # use.
    direction = _derive_direction(device_entry)

    pcm = lookup.pcm_device if lookup.pcm_device is not None else 0

    return _compose_fingerprint(
        bus=bus_identity.bus,
        address=bus_identity.address,
        codec_id=codec_id,
        pcm=pcm,
        direction=direction,
    )


# ── Internal: name parsing ─────────────────────────────────────────


def _parse_device_name(name: str, *, proc: Path) -> _CardLookup | None:
    """Map a PortAudio/ALSA device name to (card_index, pcm_device).

    Returns ``None`` for virtual aliases, default routes, or names
    whose card cannot be located in ``/proc/asound``.
    """
    stripped = name.strip()
    if not stripped:
        return None

    # Virtual-alias fast path — ``"default"``, ``"pulse"``, etc.
    # Lowercase comparison matches both ``"Default"`` and
    # ``"DEFAULT"`` edge cases.
    lowered = stripped.lower()
    if lowered in _VIRTUAL_DEVICE_NAMES:
        return None

    # Pattern 1: explicit card index via ``hw:N,M``.
    m = _HW_INDEX_RE.match(stripped)
    if m is not None:
        try:
            card_index_hw = int(m.group("index"))
        except ValueError:
            return None
        dev: int | None = None
        raw_dev = m.group("dev")
        if raw_dev is not None:
            try:
                dev = int(raw_dev)
            except ValueError:
                dev = None
        return _CardLookup(card_index=card_index_hw, pcm_device=dev)

    # Pattern 2: card id via ``<plugin>:CARD=<id>[,DEV=<n>]``.
    m = _HW_ID_RE.match(stripped)
    if m is not None:
        card_id = m.group("id")
        card_index_id: int | None = _lookup_card_index_by_id(card_id, proc=proc)
        if card_index_id is None:
            return None
        raw_dev = m.group("dev")
        dev = None
        if raw_dev is not None:
            try:
                dev = int(raw_dev)
            except ValueError:
                dev = None
        return _CardLookup(card_index=card_index_id, pcm_device=dev)

    # Pattern 3: bare card id matching a /proc/asound/cards row.
    # The bare form is common when PortAudio reports ALSA short
    # names (e.g., ``"pch"``).
    m = _BARE_ID_RE.match(stripped)
    if m is not None:
        card_id = m.group("id")
        bare_card_index: int | None = _lookup_card_index_by_id(card_id, proc=proc)
        if bare_card_index is not None:
            return _CardLookup(card_index=bare_card_index, pcm_device=None)

    return None


def _lookup_card_index_by_id(card_id: str, *, proc: Path) -> int | None:
    """Return the kernel index for a card whose id matches ``card_id``.

    Matches case-insensitively against the ALSA short-name column in
    ``/proc/asound/cards``. Returns ``None`` when the file is absent
    or the id is not found.
    """
    try:
        text = (proc / "cards").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(
            "linux_fingerprint_proc_cards_read_failed",
            detail=str(exc)[:200],
        )
        return None
    target = card_id.strip().lower()
    for line in text.splitlines():
        match = _CARD_HEADER_RE.match(line)
        if match is None:
            continue
        row_id = match.group("id").strip().lower()
        if row_id == target:
            try:
                return int(match.group("index"))
            except (ValueError, TypeError):
                return None
    return None


# ── Internal: sysfs bus identity ───────────────────────────────────


class _BusIdentity(NamedTuple):
    """Normalised representation of the hardware bus address.

    Attributes:
        bus: ``"pci"`` or ``"usb"``. Other bus types (virtual codecs,
            platform devices) are not distinguished in F1; they map
            to ``None`` via the caller's fallback.
        address: For PCI, the BDF slash-replaced with underscores
            (``"0000_00_1f.3"``) to survive shell-safe contexts. For
            USB, the ``VID:PID`` pair in uppercase hex
            (``"1532:0543"``).
    """

    bus: Literal["pci", "usb"]
    address: str


def _read_bus_identity(
    card_index: int,
    *,
    sysfs: Path,
    resolver: Callable[[Path], Path],
) -> _BusIdentity | None:
    """Resolve ``/sys/class/sound/card<N>/device`` to a bus address.

    The symlink target's path-form encodes the bus: ``/devices/pci.../``
    segments yield the PCI BDF; ``/devices/pci.../usb.../`` yields a
    USB path where the VID:PID live in ``idVendor`` / ``idProduct``
    files (ascending the tree until the first USB device node).

    The ``resolver`` injection lets unit tests bypass the real
    :meth:`pathlib.Path.resolve` — Windows dev hosts can't create
    directories with ``:`` in the name (PCI BDF segments). Production
    passes :func:`_default_resolve_symlink` which delegates to
    ``Path.resolve(strict=True)``.
    """
    device_link = sysfs / f"card{card_index}" / "device"
    try:
        resolved = resolver(device_link)
    except (OSError, RuntimeError) as exc:
        logger.debug(
            "linux_fingerprint_sysfs_resolve_failed",
            card_index=card_index,
            detail=str(exc)[:200],
        )
        return None

    resolved_str = str(resolved)

    # USB detection: walk up from ``resolved`` looking for any
    # ancestor that carries both ``idVendor`` and ``idProduct``
    # files. This is the canonical sysfs signal for a USB device
    # (the files exist on the USB device node, not on the ALSA
    # sound-card child). Tried FIRST because a USB-audio device's
    # path ALSO contains a PCI BDF (the USB controller), but the
    # VID:PID is the more-specific identity — one USB port, many
    # possible devices.
    usb = _extract_usb_vid_pid(resolved)
    if usb is not None:
        return _BusIdentity(bus="usb", address=usb)

    # PCI BDF extraction — regex matches the first BDF-shaped segment
    # in the resolved path. Onboard codecs live directly under a PCI
    # device with no USB hop, so this matches only when USB extraction
    # returned nothing.
    bdf_match = _PCI_BDF_RE.search(resolved_str)
    if bdf_match is not None:
        bdf = bdf_match.group("bdf").lower()
        # ``0000:00:1f.3`` → ``0000_00_1f.3`` (underscores instead of
        # colons keep the fingerprint shell-safe while preserving the
        # ``.function`` separator).
        address = bdf.replace(":", "_")
        return _BusIdentity(bus="pci", address=address)

    return None


_USB_ANCESTOR_WALK_LIMIT = 12
"""Safety cap on the parent-walk depth when searching for USB
``idVendor``/``idProduct`` files.

Real sysfs USB device nodes are at most ~8 levels deep from the
sound-card ``device`` link (pci.../usb/port/device/interface/sound/cardN).
12 gives headroom without letting a pathological path traverse to
the filesystem root.
"""


def _extract_usb_vid_pid(resolved: Path) -> str | None:
    """Walk up from a USB-child sysfs path to find idVendor+idProduct.

    Sysfs USB devices expose ``idVendor`` and ``idProduct`` files
    containing 4-character lowercase hex strings. They live at the
    USB device node, which is an ancestor of the sound card's
    ``device`` link. We walk up to ``_USB_ANCESTOR_WALK_LIMIT``
    parents until both files are present or we hit the filesystem
    root.

    The walk is depth-bounded rather than prefix-bounded so unit
    tests can materialise the sysfs tree anywhere (tmp_path on
    Windows / Linux / macOS) — the production code path still
    starts at a ``/sys/class/sound/card<N>/device`` resolve target,
    which never escapes ``/sys`` within the walk limit.
    """
    current: Path = resolved
    for _ in range(_USB_ANCESTOR_WALK_LIMIT):
        vendor_file = current / "idVendor"
        product_file = current / "idProduct"
        if vendor_file.is_file() and product_file.is_file():
            try:
                vid = vendor_file.read_text(encoding="utf-8").strip()
                pid = product_file.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if _is_hex4(vid) and _is_hex4(pid):
                return f"{vid.upper()}:{pid.upper()}"
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _is_hex4(value: str) -> bool:
    """True iff ``value`` is exactly 4 hex characters."""
    if len(value) != 4:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


# ── Internal: HDA codec identity ───────────────────────────────────


_CODEC_VENDOR_RE = re.compile(r"Vendor Id:\s*0x(?P<vendor>[0-9A-Fa-f]{8})")


def _read_hda_codec_id(card_index: int, *, proc: Path) -> str | None:
    """Read the first codec's ``Vendor:Device`` ID for an HDA card.

    Enriches PCI-bus fingerprints for HDA cards — the PCI BDF gives
    the bus slot but not the codec chip itself. A motherboard
    refresh that swaps codec silicon (same board, new codec
    stepping) would produce the same BDF but a different codec ID,
    which we want to detect.

    Returns ``None`` when the file is absent (USB-audio, non-HDA) or
    the Vendor Id line is unparseable.
    """
    codec_file = proc / f"card{card_index}" / "codec#0"
    try:
        text = codec_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CODEC_VENDOR_RE.search(text)
    if match is None:
        return None
    raw = match.group("vendor")
    try:
        vendor_int = int(raw[:4], 16)
        device_int = int(raw[4:], 16)
    except ValueError:
        return None
    return f"{vendor_int:04X}:{device_int:04X}"


# ── Internal: stream direction ─────────────────────────────────────


def _derive_direction(device_entry: DeviceEntry) -> Literal["capture", "playback", "duplex"]:
    """Classify the endpoint by PortAudio channel counts.

    PortAudio populates ``max_input_channels`` and
    ``max_output_channels`` from the underlying host-API probe. A
    device with both non-zero is a duplex endpoint (USB headset
    with mic); we surface all three cases so dashboards can
    distinguish.
    """
    has_input = device_entry.max_input_channels > 0
    has_output = device_entry.max_output_channels > 0
    if has_input and has_output:
        return "duplex"
    if has_input:
        return "capture"
    return "playback"


# ── Internal: fingerprint composition ──────────────────────────────


def _compose_fingerprint(
    *,
    bus: str,
    address: str,
    codec_id: str | None,
    pcm: int,
    direction: str,
) -> str:
    """Assemble the final fingerprint string.

    Layout keeps the bus discriminator up front so log greps like
    ``{linux-usb-*}`` can filter by bus type.
    """
    if codec_id is None:
        return f"{{linux-{bus}-{address}-{pcm}-{direction}}}"
    return f"{{linux-{bus}-{address}-codec-{codec_id}-{pcm}-{direction}}}"


__all__ = [
    "compute_linux_endpoint_fingerprint",
]
