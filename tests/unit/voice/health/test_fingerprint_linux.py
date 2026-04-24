"""Tests for :mod:`sovyx.voice.health._fingerprint_linux`.

Sprint 0 deliverable: per-endpoint stable fingerprint on Linux that
replaces the ``{surrogate-...}`` SHA256 with a sysfs-derived ID
equivalent to Windows MMDevice GUIDs. Every path is path-injectable
and the symlink resolver is injectable so the tests run on Windows
dev hosts (where tmp_path cannot contain ``:`` from PCI BDFs).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from sovyx.voice.device_enum import DeviceEntry, DeviceKind
from sovyx.voice.health._fingerprint_linux import (
    compute_linux_endpoint_fingerprint,
)


class _FakePosixPath:
    """Minimal Path-like with fixed ``str`` form containing colons.

    Windows ``WindowsPath`` cannot represent ``0000:00:1f.3`` in a
    path segment (``:`` is the drive separator), so the production
    code's string analysis on real ``Path.resolve()`` output doesn't
    round-trip through Windows ``tmp_path``. This helper carries an
    explicit string and supports only the operations
    :mod:`_fingerprint_linux` performs on the resolved object:

    * ``str(obj)`` / ``os.fspath(obj)`` → the fixed string
    * ``obj / "name"`` → new ``_FakePosixPath`` with appended segment
    * ``obj.is_file()`` → ``False`` (USB extraction will skip)
    * ``obj.parent`` → ``_FakePosixPath`` one segment up, stops at root
    * equality against another ``_FakePosixPath`` by string.
    """

    __slots__ = ("_s",)

    def __init__(self, s: str) -> None:
        self._s = s

    def __str__(self) -> str:
        return self._s

    def __fspath__(self) -> str:
        return self._s

    def __truediv__(self, other: str) -> _FakePosixPath:
        return _FakePosixPath(f"{self._s.rstrip('/')}/{other}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FakePosixPath):
            return self._s == other._s
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._s)

    @property
    def parent(self) -> _FakePosixPath:
        if "/" not in self._s or self._s == "/":
            return self
        parent_str = self._s.rsplit("/", 1)[0]
        if not parent_str:
            parent_str = "/"
        return _FakePosixPath(parent_str)

    def is_file(self) -> bool:  # noqa: PLR6301 — mimics Path API
        return False

    def read_text(self, encoding: str = "utf-8") -> str:  # noqa: ARG002 — API parity
        raise FileNotFoundError(self._s)


# ── Fixture helpers ────────────────────────────────────────────────


def _make_entry(
    name: str,
    *,
    host_api: str = "ALSA",
    max_input: int = 2,
    max_output: int = 0,
) -> DeviceEntry:
    """Build a DeviceEntry with the PortAudio-style defaults.

    Only ``name`` and the channel counts are consulted by the
    fingerprint module; the rest of the record is plausible but
    semantically irrelevant here.
    """
    return DeviceEntry(
        index=0,
        name=name,
        canonical_name=name.lower()[:30].strip(),
        host_api_index=0,
        host_api_name=host_api,
        max_input_channels=max_input,
        max_output_channels=max_output,
        default_samplerate=48000,
        is_os_default=False,
        kind=DeviceKind.HARDWARE,
    )


def _build_proc_asound(
    tmp_path: Path,
    *,
    cards: list[tuple[int, str, str, str]],
    codecs: dict[int, str] | None = None,
) -> Path:
    """Create a fake ``/proc/asound`` tree.

    Args:
        cards: Tuples of ``(index, id, driver, short_name)``. Written
            to ``proc/asound/cards`` in the kernel's two-line-per-card
            format.
        codecs: Optional per-card hex string like ``"14f15045"`` —
            written to ``proc/asound/card<N>/codec#0`` as the HDA
            codec Vendor Id header.
    """
    proc = tmp_path / "proc_asound"
    proc.mkdir()
    lines: list[str] = []
    for index, card_id, driver, short_name in cards:
        lines.append(f" {index} [{card_id:<15}]: {driver} - {short_name}")
        lines.append(f"                      Long Name {card_id} at xyz")
    (proc / "cards").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for index, card_id, _, _ in cards:
        card_dir = proc / f"card{index}"
        card_dir.mkdir()
        (card_dir / "id").write_text(card_id + "\n", encoding="utf-8")
    if codecs:
        for idx, vendor_hex in codecs.items():
            card_dir = proc / f"card{idx}"
            if not card_dir.exists():
                card_dir.mkdir()
            (card_dir / "codec#0").write_text(
                dedent(
                    f"""
                    Codec: Fake Codec {idx}
                    Address: 0
                    Vendor Id: 0x{vendor_hex}
                    Subsystem Id: 0x104d910e
                    """,
                ).strip()
                + "\n",
                encoding="utf-8",
            )
    return proc


def _build_sysfs_shell(
    tmp_path: Path,
    card_indices: list[int],
) -> Path:
    """Create ``/sys/class/sound/card<N>/`` stubs (no 'device' entry).

    The 'device' symlink is injected via the test's ``resolve_symlink``
    callable rather than a real filesystem symlink — Windows tmp_path
    can't contain ``:`` so a real PCI-BDF directory is impossible.
    """
    sysfs = tmp_path / "sysfs_class_sound"
    sysfs.mkdir()
    for index in card_indices:
        (sysfs / f"card{index}").mkdir()
    return sysfs


def _pci_resolver(
    sysfs: Path,
    *,
    card_to_bdf: dict[int, str],
):
    """Return a resolve_symlink callable that maps each card's
    'device' symlink to a ``_FakePosixPath`` carrying the PCI BDF.

    Returns an object whose ``str`` form keeps the colons intact
    regardless of host OS (Windows tmp_path cannot contain ``:``).
    """

    def _resolve(path: Path):
        for idx, bdf in card_to_bdf.items():
            expected = sysfs / f"card{idx}" / "device"
            if path == expected:
                domain = bdf.split(":")[0]
                return _FakePosixPath(f"/sys/devices/pci{domain}:00/{bdf}")
        raise FileNotFoundError(str(path))

    return _resolve


def _usb_resolver(
    sysfs: Path,
    tmp_path: Path,
    *,
    card_to_usb_device: dict[int, tuple[str, str]],
) -> callable:  # type: ignore[valid-type]
    """resolve_symlink that maps each card's device link to a REAL
    tmp_path directory containing ``idVendor`` + ``idProduct`` files
    (nested one level inside, so the parent-walk discovers them)."""
    for idx, (vid, pid) in card_to_usb_device.items():
        usb_device = tmp_path / f"usb_dev_card{idx}"
        usb_device.mkdir(exist_ok=True)
        (usb_device / "idVendor").write_text(vid, encoding="utf-8")
        (usb_device / "idProduct").write_text(pid, encoding="utf-8")
        child = usb_device / "sound" / f"card{idx}"
        child.mkdir(parents=True, exist_ok=True)

    def _resolve(path: Path) -> Path:
        for idx in card_to_usb_device:
            expected = sysfs / f"card{idx}" / "device"
            if path == expected:
                return tmp_path / f"usb_dev_card{idx}" / "sound" / f"card{idx}"
        raise FileNotFoundError(str(path))

    return _resolve


# ── Happy paths ────────────────────────────────────────────────────


class TestHdaInternalCodec:
    """Onboard HDA codec — PCI BDF + codec vendor:device enrichment."""

    def test_vaio_pilot_hw_index_form(self, tmp_path: Path) -> None:
        """VAIO pilot case: Conexant SN6180 at 0000:00:1f.3 as card 0."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("hw:0,0")
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}"

    def test_vaio_pilot_card_id_form(self, tmp_path: Path) -> None:
        """Same hardware, but the DeviceEntry.name uses the CARD= form."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("plughw:CARD=PCH,DEV=0")
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}"

    def test_bare_card_id_form(self, tmp_path: Path) -> None:
        """PortAudio sometimes reports just the short name — ``"pch"``."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("pch")
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        # Bare form → pcm_device=None → defaults to 0.
        assert result == "{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}"

    def test_pci_card_without_codec_file(self, tmp_path: Path) -> None:
        """Non-HDA PCI card: no codec#0 file. The fingerprint drops
        the codec segment but still emits a stable PCI-BDF form."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "ENS1371", "ENS1371", "Ensoniq")],
            codecs=None,
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:01:01.0"})
        entry = _make_entry("hw:0,0")
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-pci-0000_01_01.0-0-capture}"


class TestUsbAudio:
    """USB-audio devices — VID:PID fingerprint."""

    def test_razer_usb_headset(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(
            tmp_path,
            cards=[(1, "Microphone", "USB-Audio", "USB Audio")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [1])
        resolver = _usb_resolver(
            sysfs,
            tmp_path,
            card_to_usb_device={1: ("1532", "0543")},
        )
        entry = _make_entry("hw:1,0", max_input=1, max_output=0)
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-usb-1532:0543-0-capture}"

    def test_usb_duplex_headset(self, tmp_path: Path) -> None:
        """Duplex device (mic + speakers) is tagged ``duplex``."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(2, "USB", "USB-Audio", "Generic USB Audio")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [2])
        resolver = _usb_resolver(
            sysfs,
            tmp_path,
            card_to_usb_device={2: ("05ac", "8406")},
        )
        entry = _make_entry("hw:2,0", max_input=1, max_output=2)
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-usb-05AC:8406-0-duplex}"

    def test_usb_playback_only(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(
            tmp_path,
            cards=[(1, "HDMI", "USB-Audio", "USB HDMI")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [1])
        resolver = _usb_resolver(
            sysfs,
            tmp_path,
            card_to_usb_device={1: ("1d6b", "0003")},
        )
        entry = _make_entry("hw:1,0", max_input=0, max_output=2)
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-usb-1D6B:0003-0-playback}"


class TestMultipleCardsDistinctIdentity:
    """Multiple cards on the same system → distinct fingerprints."""

    def test_internal_plus_usb_distinct(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(
            tmp_path,
            cards=[
                (0, "PCH", "HDA-Intel", "HD-Audio Generic"),
                (1, "Microphone", "USB-Audio", "USB Audio"),
            ],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0, 1])
        # Composite resolver: card 0 → PCI, card 1 → USB
        (tmp_path / "usb_dev_card1").mkdir()
        (tmp_path / "usb_dev_card1" / "idVendor").write_text("1532", encoding="utf-8")
        (tmp_path / "usb_dev_card1" / "idProduct").write_text("0543", encoding="utf-8")
        (tmp_path / "usb_dev_card1" / "sound" / "card1").mkdir(parents=True)

        def composite_resolver(path: Path):
            if path == sysfs / "card0" / "device":
                return _FakePosixPath("/sys/devices/pci0000:00/0000:00:1f.3")
            if path == sysfs / "card1" / "device":
                return tmp_path / "usb_dev_card1" / "sound" / "card1"
            raise FileNotFoundError(str(path))

        internal = compute_linux_endpoint_fingerprint(
            _make_entry("hw:0,0"),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=composite_resolver,
        )
        external = compute_linux_endpoint_fingerprint(
            _make_entry("hw:1,0", max_input=1),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=composite_resolver,
        )
        assert internal is not None
        assert external is not None
        assert internal != external
        assert "pci" in internal and "usb" in external


# ── Virtual-alias rejections ───────────────────────────────────────


class TestVirtualAliasesReturnNone:
    """Names like ``"default"`` / ``"pulse"`` have no stable card
    binding — the caller must fall back to the surrogate."""

    @pytest.mark.parametrize(
        "name",
        [
            "default",
            "pulse",
            "pipewire",
            "jack",
            "null",
            "sysdefault",
            "Default",
            "DEFAULT",
        ],
    )
    def test_virtual_name_returns_none(self, tmp_path: Path, name: str) -> None:
        proc = _build_proc_asound(tmp_path, cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")])
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry(name)
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=resolver,
            )
            is None
        )


# ── Graceful degradation paths ─────────────────────────────────────


class TestDegradationReturnsNone:
    """Any I/O or parse failure → None; caller surrogates."""

    def test_missing_proc_asound(self, tmp_path: Path) -> None:
        missing_proc = tmp_path / "no_such_proc"
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("plughw:CARD=PCH,DEV=0")
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=missing_proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=resolver,
            )
            is None
        )

    def test_resolver_raises(self, tmp_path: Path) -> None:
        """Resolver can't find the device link (sysfs gap)."""
        proc = _build_proc_asound(tmp_path, cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")])
        sysfs = _build_sysfs_shell(tmp_path, [0])

        def raising_resolver(_: Path) -> Path:
            raise FileNotFoundError("sysfs missing")

        entry = _make_entry("hw:0,0")
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=raising_resolver,
            )
            is None
        )

    def test_card_id_not_in_proc_cards(self, tmp_path: Path) -> None:
        """DeviceEntry references a card id that's not in /proc/asound/cards."""
        proc = _build_proc_asound(tmp_path, cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")])
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("plughw:CARD=NONEXISTENT,DEV=0")
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=resolver,
            )
            is None
        )

    def test_resolved_path_has_no_pci_no_usb(self, tmp_path: Path) -> None:
        """Path resolves but contains neither PCI BDF nor USB vendor files."""
        proc = _build_proc_asound(tmp_path, cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")])
        sysfs = _build_sysfs_shell(tmp_path, [0])

        def vague_resolver(_: Path):
            return _FakePosixPath("/sys/devices/virtual/sound/seq/card0")

        entry = _make_entry("hw:0,0")
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=vague_resolver,
            )
            is None
        )

    def test_empty_name_returns_none(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(tmp_path, cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")])
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("")
        assert (
            compute_linux_endpoint_fingerprint(
                entry,
                proc_asound=proc,
                sysfs_class_sound=sysfs,
                resolve_symlink=resolver,
            )
            is None
        )


# ── Deterministic invariants ───────────────────────────────────────


class TestDeterminism:
    """Same input → same fingerprint forever. ComboStore rules R6/R7
    key on this; a hash algorithm change would silently invalidate
    every persisted entry."""

    def test_repeated_invocation_same_output(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        entry = _make_entry("hw:0,0")
        first = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        for _ in range(5):
            assert (
                compute_linux_endpoint_fingerprint(
                    entry,
                    proc_asound=proc,
                    sysfs_class_sound=sysfs,
                    resolve_symlink=resolver,
                )
                == first
            )

    def test_different_pcm_devices_distinct(self, tmp_path: Path) -> None:
        """Same card, different PCM device → distinct fingerprints."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        pcm0 = compute_linux_endpoint_fingerprint(
            _make_entry("hw:0,0"),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        pcm3 = compute_linux_endpoint_fingerprint(
            _make_entry("hw:0,3"),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert pcm0 != pcm3

    def test_direction_flip_distinct(self, tmp_path: Path) -> None:
        """Same hardware, different direction → distinct fingerprint."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        capture = compute_linux_endpoint_fingerprint(
            _make_entry("hw:0,0", max_input=2, max_output=0),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        playback = compute_linux_endpoint_fingerprint(
            _make_entry("hw:0,0", max_input=0, max_output=2),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert capture != playback


# ── Case-insensitivity ─────────────────────────────────────────────


class TestCaseInsensitiveCardIdMatch:
    """CARD= lookups are case-insensitive — ``pch`` and ``PCH``
    resolve to the same card."""

    def test_card_id_case_insensitive(self, tmp_path: Path) -> None:
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "PCH", "HDA-Intel", "HD-Audio Generic")],
            codecs={0: "14f15045"},
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        resolver = _pci_resolver(sysfs, card_to_bdf={0: "0000:00:1f.3"})
        upper = compute_linux_endpoint_fingerprint(
            _make_entry("plughw:CARD=PCH,DEV=0"),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        lower = compute_linux_endpoint_fingerprint(
            _make_entry("plughw:CARD=pch,DEV=0"),
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert upper == lower
        assert upper is not None
