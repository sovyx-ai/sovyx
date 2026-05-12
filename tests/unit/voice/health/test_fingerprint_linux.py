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


class TestPortAudioFriendlyPrefixedNames:
    """v0.38.2 — Pattern 4 (friendly-prefixed PortAudio names).

    Pre-v0.38.2 ``_parse_device_name`` had three patterns
    (``hw:N,M`` / ``<plug>:CARD=`` / bare-id) all using ``re.match``
    (start-anchored). PortAudio's PulseAudio/PipeWire-backed Linux
    names commonly take the shape
    ``"<friendly>: <profile> (hw:N,M)"`` — the first 3 patterns
    don't match these names and the fingerprint pipeline collapsed
    to the surrogate hash, preventing F2-M10's USB walk + regex
    fallback from EVER being reached. See
    `LAUDO-voice-failover-root-cause-2026-05-12.md` §2 H2.

    These tests pin Pattern 4: a tail extractor that finds
    ``(hw:N,M)`` / ``(plughw:N,M)`` substrings inside the friendly-
    prefixed name shape.
    """

    def test_h2_operator_razer_friendly_name_resolves_to_card_2(self, tmp_path: Path) -> None:
        """Operator's exact device name from diag tarball.

        From `G_sovyx/api_voice_hardware_detect.json`:
            "Razer BlackShark V2 Pro: USB Audio (hw:2,0)"

        Pre-v0.38.2 this returned ``None`` from `_parse_device_name`
        → fingerprint cascaded to surrogate hash
        ``{surrogate-55c423ce-...}`` (verified in operator's
        `G_sovyx/api_voice_health.json` ComboStore dump).

        v0.38.2 fix: Pattern 4 extracts ``hw:2,0`` from the tail
        → resolves to card 2, PCM device 0 → fingerprint pipeline
        proceeds to ``_read_bus_identity`` and produces the proper
        ``{linux-usb-VID:PID-...}`` form (when sysfs is healthy).
        """
        proc = _build_proc_asound(
            tmp_path,
            cards=[(2, "Pro", "USB-Audio", "Razer BlackShark V2 Pro")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [2])
        resolver = _usb_resolver(
            sysfs,
            tmp_path,
            card_to_usb_device={2: ("1532", "0543")},
        )
        # Operator's exact device name shape from the tarball.
        entry = _make_entry(
            "Razer BlackShark V2 Pro: USB Audio (hw:2,0)",
            max_input=1,
            max_output=0,
        )
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        # Expected: stable {linux-usb-VID:PID-PCM-direction} form,
        # NOT a surrogate hash. Operator's Razer = 1532:0543.
        assert result == "{linux-usb-1532:0543-0-capture}", (
            f"v0.38.2 must extract hw:2,0 from operator's friendly-"
            f"prefixed device name and produce stable USB fingerprint. "
            f"Got: {result!r}. Pre-v0.38.2 returned None → surrogate."
        )

    def test_h2_operator_internal_mic_friendly_name_resolves_to_card_1(
        self, tmp_path: Path
    ) -> None:
        """Operator's internal mic name from diag tarball.

        From `G_sovyx/api_voice_hardware_detect.json`:
            "HD-Audio Generic: SN6180 Analog (hw:1,0)"

        Same Pattern 4 fix as the Razer test — but PCI bus, so the
        fingerprint is ``{linux-pci-...}`` not ``{linux-usb-...}``.
        Codec ID enrichment requires the codec#0 file in /proc/asound.
        """
        proc = _build_proc_asound(
            tmp_path,
            cards=[(1, "Generic", "HDA-Intel", "HD-Audio Generic")],
            codecs={1: "14f15045"},  # Conexant SN6180 vendor:device
        )
        sysfs = _build_sysfs_shell(tmp_path, [1])
        resolver = _pci_resolver(sysfs, card_to_bdf={1: "0000:04:00.6"})
        entry = _make_entry(
            "HD-Audio Generic: SN6180 Analog (hw:1,0)",
            max_input=2,
            max_output=0,
        )
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        # PCI BDF + codec id enrichment + PCM direction = stable.
        assert result is not None
        assert result.startswith("{linux-pci-")
        assert "14F1:5045" in result  # Codec vendor:device, uppercased

    def test_h2_plughw_tail_variant_also_resolves(self, tmp_path: Path) -> None:
        """Pattern 4 also handles ``(plughw:N,M)`` tail variants."""
        proc = _build_proc_asound(
            tmp_path,
            cards=[(3, "USB", "USB-Audio", "Generic USB")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [3])
        resolver = _usb_resolver(
            sysfs,
            tmp_path,
            card_to_usb_device={3: ("05ac", "8406")},
        )
        entry = _make_entry(
            "Some Vendor: Mic Profile (plughw:3,0)",
            max_input=1,
            max_output=0,
        )
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
            resolve_symlink=resolver,
        )
        assert result == "{linux-usb-05AC:8406-0-capture}"

    def test_h2_friendly_name_without_tail_still_returns_none(self, tmp_path: Path) -> None:
        """A friendly name with no ``hw:N,M`` tail correctly returns None.

        Pattern 4 is a tail-search; names that don't have the
        canonical shape still fall through to surrogate. Guards
        against over-matching.
        """
        proc = _build_proc_asound(
            tmp_path,
            cards=[(0, "X", "DRV", "Some Card")],
        )
        sysfs = _build_sysfs_shell(tmp_path, [0])
        entry = _make_entry("Razer BlackShark V2 Pro Mono", max_input=1)
        # No hw:N,M anywhere → Pattern 4 doesn't match → None.
        result = compute_linux_endpoint_fingerprint(
            entry,
            proc_asound=proc,
            sysfs_class_sound=sysfs,
        )
        assert result is None


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


class TestUsbWalkDepthAndNameFallback:
    """v0.38.0 / W3.G1 — F2-M10 (audit §3.P) closure.

    The pre-fix walk limit (12) collapsed to a surrogate hash on
    USB-through-hub topologies (Razer USB through a powered hub
    nests 14-16 levels deep on Linux). The fix raises the walk limit
    to 20 AND adds a regex name-based VID:PID fallback for paths
    that still hit the limit. Tests pin both behaviours.
    """

    @pytest.mark.parametrize("depth", [5, 10, 15, 18])
    def test_idvendor_walk_resolves_within_limit(self, tmp_path: Path, depth: int) -> None:
        """Sysfs trees of depths 5/10/15/18 all resolve via the walk."""
        from sovyx.voice.health._fingerprint_linux import _extract_usb_vid_pid

        # Build a directory tree N levels deep with idVendor/idProduct
        # at the ROOT and the resolve target at the leaf. The walker
        # ascends from the leaf; at the depth-th parent it finds the
        # files.
        usb_root = tmp_path / "usb_root"
        usb_root.mkdir()
        (usb_root / "idVendor").write_text("1532", encoding="utf-8")
        (usb_root / "idProduct").write_text("0543", encoding="utf-8")
        leaf = usb_root
        for i in range(depth):
            leaf = leaf / f"hop{i}"
            leaf.mkdir()
        result = _extract_usb_vid_pid(leaf)
        assert result == "1532:0543", f"depth {depth} should resolve"

    def test_walk_limit_exceeded_falls_back_to_name_regex(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Walk hits the 20-level cap → name regex fallback fires + logs.

        Uses a constructed (non-existent) path so the walker's
        ``is_file()`` returns False on every iteration — exhausts the
        walk, falls through to the regex fallback. Avoids Windows
        MAX_PATH limits that block real-directory deep nesting.
        """
        from sovyx.voice.health._fingerprint_linux import _extract_usb_vid_pid

        # 25 segments > _USB_ANCESTOR_WALK_LIMIT (20). Each segment
        # carries the usb-VID_PID_ pattern so the regex fallback finds
        # it on the resolved path.
        deep_root = Path("/fake/sysfs")
        for i in range(25):
            deep_root = deep_root / f"usb-1532_0543_razer_hub_seg{i}"
        caplog.set_level("INFO", logger="sovyx.voice.health._fingerprint_linux")
        result = _extract_usb_vid_pid(deep_root)
        assert result == "1532:0543", (
            "regex name fallback must extract VID:PID when walk hits limit"
        )
        # Telemetry event MUST fire so operators can correlate
        # surrogate-hash → fingerprint-stable transitions.
        events = [
            r.getMessage()
            for r in caplog.records
            if "voice_endpoint_fingerprint_usb_walk_limit_hit" in r.getMessage()
        ]
        assert events, "fallback hit MUST emit voice_endpoint_fingerprint_usb_walk_limit_hit"

    def test_walk_limit_exceeded_without_name_match_returns_none(self) -> None:
        """If neither the walk nor the regex match, return None (caller
        falls through to surrogate hash)."""
        from sovyx.voice.health._fingerprint_linux import _extract_usb_vid_pid

        # 25 opaque segments — no idVendor file (path doesn't exist),
        # no usb-VID_PID_ pattern in any segment. Walker exhausts +
        # regex returns None.
        deep_root = Path("/fake/sysfs")
        for i in range(25):
            deep_root = deep_root / f"opaque_segment{i}"
        result = _extract_usb_vid_pid(deep_root)
        assert result is None

    def test_walk_limit_constant_is_20(self) -> None:
        """The constant value pins the audit's 12 → 20 promotion."""
        from sovyx.voice.health._fingerprint_linux import _USB_ANCESTOR_WALK_LIMIT

        assert _USB_ANCESTOR_WALK_LIMIT == 20


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
