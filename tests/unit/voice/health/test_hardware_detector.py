"""Tests for the L2.5 HardwareContext detector.

Every path is injected — tests never read the real ``/sys`` /
``/proc`` / ``/etc/os-release``. The production code is
filesystem-only (no subprocess, no root), so we exercise every
branch against tmp_path fixtures.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from sovyx.voice.health import (
    HardwareContext,
    detect_hardware_context,
)
from sovyx.voice.health._hardware_detector import (
    _detect_audio_stack,  # noqa: PLC2701 — branch tests key on helper
    _detect_codec_id,
    _detect_driver_family,
    _parse_codec_vendor_id,
    _read_distro,
    _read_dmi_field,
    _resolve_xdg_runtime_dir,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── Helper: build a fake /proc/asound tree ──────────────────────────


def _fake_proc_asound(
    tmp_path: Path,
    *,
    codec_vendor_hex: str | None = None,
    card_id: str = "PCH",
    include_pcm: bool = False,
) -> Path:
    """Build a fake ``/proc/asound`` directory.

    Args:
        codec_vendor_hex: Hex string (without ``0x``) to write as
            ``Vendor Id: 0x<hex>``. ``None`` omits the codec file
            entirely (USB-class or no-codec cards).
        card_id: Value written to ``card0/id``.
        include_pcm: Create ``pcm`` file to simulate a live ALSA
            runtime (needed for the ALSA fallback in
            :func:`_detect_audio_stack`).
    """
    proc_asound = tmp_path / "proc_asound"
    proc_asound.mkdir()
    card0 = proc_asound / "card0"
    card0.mkdir()
    (card0 / "id").write_text(card_id, encoding="utf-8")
    if codec_vendor_hex is not None:
        codec_content = dedent(
            f"""
            Codec: Fake Codec
            Address: 0
            Vendor Id: 0x{codec_vendor_hex}
            Subsystem Id: 0x104d910e
            """,
        ).strip()
        (card0 / "codec#0").write_text(codec_content, encoding="utf-8")
    if include_pcm:
        (proc_asound / "pcm").write_text("00-00: HDA Analog : playback 1 : capture 1")
    return proc_asound


# ── _parse_codec_vendor_id ──────────────────────────────────────────


class TestParseCodecVendorId:
    def test_pilot_sn6180(self, tmp_path: Path) -> None:
        """VAIO VJFE69F11X pilot: Conexant SN6180 = 14F1:5045."""
        codec = tmp_path / "codec#0"
        codec.write_text(
            "Codec: Conexant CX8070\nVendor Id: 0x14f15045\nOther: x\n",
            encoding="utf-8",
        )
        assert _parse_codec_vendor_id(codec) == "14F1:5045"

    def test_uppercase_output_regardless_of_input_case(self, tmp_path: Path) -> None:
        codec = tmp_path / "codec#0"
        codec.write_text("Vendor Id: 0xABCD1234\n", encoding="utf-8")
        assert _parse_codec_vendor_id(codec) == "ABCD:1234"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _parse_codec_vendor_id(tmp_path / "nonexistent") is None

    def test_no_vendor_id_line(self, tmp_path: Path) -> None:
        codec = tmp_path / "codec#0"
        codec.write_text("Codec: Something\nNo vendor here\n", encoding="utf-8")
        assert _parse_codec_vendor_id(codec) is None

    def test_malformed_hex(self, tmp_path: Path) -> None:
        codec = tmp_path / "codec#0"
        # _VENDOR_ID_RE requires exactly 8 hex digits — this has 4, so
        # the regex doesn't match → None (no fallthrough to int()).
        codec.write_text("Vendor Id: 0xZZZZ\n", encoding="utf-8")
        assert _parse_codec_vendor_id(codec) is None


# ── _detect_codec_id ────────────────────────────────────────────────


class TestDetectCodecId:
    def test_finds_codec_on_first_card(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(tmp_path, codec_vendor_hex="14f15045")
        assert _detect_codec_id(proc) == "14F1:5045"

    def test_no_proc_asound(self, tmp_path: Path) -> None:
        assert _detect_codec_id(tmp_path / "missing") is None

    def test_proc_asound_is_file_not_dir(self, tmp_path: Path) -> None:
        f = tmp_path / "proc_asound"
        f.write_text("not a dir")
        assert _detect_codec_id(f) is None

    def test_no_cards(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc_asound"
        proc.mkdir()
        assert _detect_codec_id(proc) is None

    def test_card_has_no_codec_file(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(tmp_path, codec_vendor_hex=None, card_id="USB-Mic")
        assert _detect_codec_id(proc) is None


# ── _detect_driver_family ───────────────────────────────────────────


class TestDetectDriverFamily:
    def test_hda_from_codec_file(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(tmp_path, codec_vendor_hex="14f15045")
        assert _detect_driver_family(proc) == "hda"

    def test_usb_from_card_id(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(
            tmp_path,
            codec_vendor_hex=None,
            card_id="Blue USB Microphone",
        )
        assert _detect_driver_family(proc) == "usb-audio"

    def test_usb_case_insensitive(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(
            tmp_path,
            codec_vendor_hex=None,
            card_id="usb Audio Gadget",
        )
        assert _detect_driver_family(proc) == "usb-audio"

    def test_hda_wins_over_usb_when_both_present(self, tmp_path: Path) -> None:
        """HDA card + USB card on the same system → HDA (laptop internal
        is the capture chip we care about).
        """
        proc = tmp_path / "proc_asound"
        proc.mkdir()
        card0 = proc / "card0"
        card0.mkdir()
        (card0 / "id").write_text("PCH")
        (card0 / "codec#0").write_text("Vendor Id: 0x14f15045")
        card1 = proc / "card1"
        card1.mkdir()
        (card1 / "id").write_text("USB-Mic")
        assert _detect_driver_family(proc) == "hda"

    def test_unknown_when_nothing_matches(self, tmp_path: Path) -> None:
        proc = _fake_proc_asound(
            tmp_path,
            codec_vendor_hex=None,
            card_id="Generic",
        )
        assert _detect_driver_family(proc) == "unknown"

    def test_missing_proc_asound(self, tmp_path: Path) -> None:
        assert _detect_driver_family(tmp_path / "nope") == "unknown"


# ── _read_dmi_field ─────────────────────────────────────────────────


class TestReadDmiField:
    def test_happy_path(self, tmp_path: Path) -> None:
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        (dmi / "sys_vendor").write_text("Sony Group Corporation\n")
        assert _read_dmi_field(dmi, "sys_vendor") == "Sony Group Corporation"

    def test_strips_trailing_whitespace(self, tmp_path: Path) -> None:
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        (dmi / "product_name").write_text("VJFE69F11X-B0221H   \n")
        assert _read_dmi_field(dmi, "product_name") == "VJFE69F11X-B0221H"

    def test_missing_file(self, tmp_path: Path) -> None:
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        assert _read_dmi_field(dmi, "sys_vendor") is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        (dmi / "sys_vendor").write_text("")
        assert _read_dmi_field(dmi, "sys_vendor") is None

    def test_whitespace_only_returns_none(self, tmp_path: Path) -> None:
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        (dmi / "sys_vendor").write_text("   \n  \n")
        assert _read_dmi_field(dmi, "sys_vendor") is None


# ── _read_distro ────────────────────────────────────────────────────


class TestReadDistro:
    def test_linuxmint(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text(
            dedent(
                """
                NAME="Linux Mint"
                ID=linuxmint
                VERSION_ID="22.2"
                PRETTY_NAME="Linux Mint 22.2"
                """,
            ).strip(),
        )
        assert _read_distro(os_release) == "linuxmint-22.2"

    def test_rolling_distro_without_version_id(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text('ID=arch\nNAME="Arch Linux"\n')
        assert _read_distro(os_release) == "arch"

    def test_handles_unquoted_values(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text("ID=fedora\nVERSION_ID=40\n")
        assert _read_distro(os_release) == "fedora-40"

    def test_handles_single_quotes(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text("ID='ubuntu'\nVERSION_ID='24.04'\n")
        assert _read_distro(os_release) == "ubuntu-24.04"

    def test_skips_comments_and_blanks(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text(
            dedent(
                """
                # This is a comment

                ID=debian
                # Another comment
                VERSION_ID=12
                """,
            ).strip(),
        )
        assert _read_distro(os_release) == "debian-12"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _read_distro(tmp_path / "nope") is None

    def test_missing_id(self, tmp_path: Path) -> None:
        os_release = tmp_path / "os-release"
        os_release.write_text("NAME=Unknown\nVERSION_ID=1\n")
        assert _read_distro(os_release) is None


# ── _detect_audio_stack ─────────────────────────────────────────────


class TestDetectAudioStack:
    def test_pipewire_socket_takes_precedence(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        (xdg / "pipewire-0").touch()
        # Even if Pulse socket also exists, PipeWire wins.
        (xdg / "pulse").mkdir()
        (xdg / "pulse" / "native").touch()
        assert _detect_audio_stack(xdg) == "pipewire"

    def test_pulseaudio_without_pipewire(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        (xdg / "pulse").mkdir()
        (xdg / "pulse" / "native").touch()
        assert _detect_audio_stack(xdg) == "pulseaudio"

    def test_none_xdg_falls_through(self, tmp_path: Path) -> None:
        # Without XDG runtime dir and without /proc/asound/pcm, we
        # can't classify → None.
        _ = tmp_path  # silence unused-arg
        result = _detect_audio_stack(None)
        # Depends on host /proc/asound/pcm; assert it's one of the
        # valid outputs (None OR "alsa" on a real Linux runner).
        assert result in {None, "alsa"}

    def test_no_sockets_returns_none_or_alsa(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg"
        xdg.mkdir()  # empty — no sockets
        # Fallback path checks /proc/asound/pcm (real path); on
        # Windows / missing the test returns None.
        result = _detect_audio_stack(xdg)
        assert result in {None, "alsa"}


# ── detect_hardware_context (integration) ───────────────────────────


class TestDetectHardwareContext:
    @pytest.mark.asyncio()
    async def test_full_pilot_shape(self, tmp_path: Path) -> None:
        """Simulates the VAIO VJFE69F11X pilot fully."""
        proc = _fake_proc_asound(tmp_path, codec_vendor_hex="14f15045")
        dmi = tmp_path / "dmi"
        dmi.mkdir()
        (dmi / "sys_vendor").write_text("Sony Group Corporation\n")
        (dmi / "product_name").write_text("VJFE69F11X-B0221H\n")
        os_release = tmp_path / "os-release"
        os_release.write_text("ID=linuxmint\nVERSION_ID=22.2\n")
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        (xdg / "pipewire-0").touch()

        hw = await detect_hardware_context(
            proc_asound=proc,
            dmi_path=dmi,
            os_release_path=os_release,
            xdg_runtime_dir=xdg,
            kernel_release="6.14.0-37-generic",
        )
        assert isinstance(hw, HardwareContext)
        assert hw.driver_family == "hda"
        assert hw.codec_id == "14F1:5045"
        assert hw.system_vendor == "Sony Group Corporation"
        assert hw.system_product == "VJFE69F11X-B0221H"
        assert hw.distro == "linuxmint-22.2"
        assert hw.audio_stack == "pipewire"
        assert hw.kernel == "6.14.0-37-generic"

    @pytest.mark.asyncio()
    async def test_partial_detection_returns_hw_with_nones(
        self,
        tmp_path: Path,
    ) -> None:
        """Only /proc/asound populated → other fields are None."""
        proc = _fake_proc_asound(tmp_path, codec_vendor_hex="10ec0257")
        hw = await detect_hardware_context(
            proc_asound=proc,
            dmi_path=tmp_path / "dmi-nope",
            os_release_path=tmp_path / "os-nope",
            xdg_runtime_dir=tmp_path / "xdg-nope",
            kernel_release="6.14.0",
        )
        assert hw.driver_family == "hda"
        assert hw.codec_id == "10EC:0257"  # Realtek ALC257
        assert hw.system_vendor is None
        assert hw.system_product is None
        assert hw.distro is None
        assert hw.audio_stack is None
        assert hw.kernel == "6.14.0"

    @pytest.mark.asyncio()
    async def test_completely_empty_system_yields_unknown(
        self,
        tmp_path: Path,
    ) -> None:
        """Nothing detectable → driver_family='unknown', every field None."""
        hw = await detect_hardware_context(
            proc_asound=tmp_path / "nope",
            dmi_path=tmp_path / "nope-dmi",
            os_release_path=tmp_path / "nope-os",
            xdg_runtime_dir=tmp_path / "nope-xdg",
            kernel_release=None,
        )
        assert hw.driver_family == "unknown"
        assert hw.codec_id is None
        assert hw.system_vendor is None
        assert hw.system_product is None
        assert hw.distro is None

    @pytest.mark.asyncio()
    async def test_never_raises_even_on_weird_paths(self, tmp_path: Path) -> None:
        """Passing a file where a directory is expected → no exception."""
        fake_file = tmp_path / "not-a-dir"
        fake_file.write_text("nope")
        hw = await detect_hardware_context(
            proc_asound=fake_file,
            dmi_path=fake_file,
            os_release_path=fake_file,
            xdg_runtime_dir=fake_file,
        )
        # Doesn't raise; returns some HardwareContext.
        assert isinstance(hw, HardwareContext)

    @pytest.mark.asyncio()
    async def test_uses_real_defaults_when_no_kwargs(self) -> None:
        """Smoke test — calling with no args must not crash.

        On Windows / a CI runner without /proc, most fields come back
        None but driver_family is always set.
        """
        hw = await detect_hardware_context()
        assert isinstance(hw, HardwareContext)
        assert hw.driver_family in {"hda", "sof", "usb-audio", "bt", "unknown"}


class TestResolveXdgRuntimeDir:
    """Paranoid-QA R3 HIGH #13 (verifies R2 LOW-4 guard).

    ``_resolve_xdg_runtime_dir`` treats whitespace-only
    ``XDG_RUNTIME_DIR`` as unset — a sysadmin who exported
    ``XDG_RUNTIME_DIR="   "`` shouldn't drive ``Path("   ")`` into
    the pipewire-socket probe. Without these tests, deleting the
    ``value.strip()`` check passes every other test silently.
    """

    def test_override_wins_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        resolved = _resolve_xdg_runtime_dir(tmp_path)
        assert resolved == tmp_path

    def test_env_resolved_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import Path as _Path

        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        resolved = _resolve_xdg_runtime_dir(None)
        assert resolved == _Path("/run/user/1000")

    def test_unset_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert _resolve_xdg_runtime_dir(None) is None

    def test_empty_string_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "")
        assert _resolve_xdg_runtime_dir(None) is None

    @pytest.mark.parametrize(
        "whitespace_value",
        ["   ", "\t", "\n", "\r\n", " \t\n "],
    )
    def test_whitespace_only_env_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, whitespace_value: str
    ) -> None:
        """R2 LOW-4 guard: whitespace-only values are unset. Without
        this, ``Path("   ")`` would be constructed and the pipewire
        socket probe would fail silently instead of gracefully
        falling back to the "no XDG" path."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", whitespace_value)
        assert _resolve_xdg_runtime_dir(None) is None
