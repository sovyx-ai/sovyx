"""Tests for USB-audio fingerprinting [Phase 5 T5.43].

Coverage:

* Windows PnP path with full ``USB\\VID_xxxx&PID_xxxx\\SERIAL``
  produces ``"usb-VVVV:PPPP-SERIAL"`` (lowercase hex).
* Windows PnP path WITHOUT real serial (``5&xxxx&0&...``
  placeholder) drops the serial — placeholder isn't stable.
* Windows PnP path with malformed string returns None.
* Linux sysfs ``card/usb_id`` shortcut → fingerprint with
  ``serial`` if available.
* Linux sysfs parent-walk fallback finds idVendor/idProduct on
  the upstream USB device node.
* Non-USB cards (PCI codec, virtual loopback) walk all hops
  without finding USB ancestors → None.
* Non-Linux platform short-circuits the sysfs branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.voice.health._usb_fingerprint import (
    _from_windows_pnp,
    fingerprint_usb_device,
)


class TestWindowsPnp:
    def test_full_pnp_with_real_serial(self) -> None:
        fp = _from_windows_pnp(
            r"USB\VID_1532&PID_0528\6&abcdef12345&0",
        )
        # The serial portion contains '&' so it's classified as
        # a placeholder + dropped.
        assert fp == "usb-1532:0528"

    def test_pnp_with_clean_serial(self) -> None:
        # A class-compliant device with a real iSerial descriptor.
        fp = _from_windows_pnp(r"USB\VID_046d&PID_0a45\AB12CD34")
        assert fp == "usb-046d:0a45-AB12CD34"

    def test_pnp_with_no_serial_segment(self) -> None:
        fp = _from_windows_pnp(r"USB\VID_1532&PID_0528")
        assert fp == "usb-1532:0528"

    def test_pnp_lowercase_hex_normalised(self) -> None:
        fp = _from_windows_pnp(r"USB\VID_1532&PID_0528\AB12")
        # vendor/product always lowercased; serial preserved.
        assert fp == "usb-1532:0528-AB12"

    def test_non_usb_returns_none(self) -> None:
        fp = _from_windows_pnp(r"PCI\VEN_10EC&DEV_8168&SUBSYS_85aa1043")
        assert fp is None

    def test_garbage_returns_none(self) -> None:
        fp = _from_windows_pnp("not a device id")
        assert fp is None


class TestLinuxSysfs:
    @pytest.fixture()
    def fake_card(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        # Pretend we're on Linux; tests must not require an
        # actual /sys/class/sound mount.
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "linux")
        card = tmp_path / "card1"
        card.mkdir()
        return card

    def test_usb_id_shortcut_with_serial(self, fake_card: Path) -> None:
        (fake_card / "usb_id").write_text("1532:0528\n")
        device = fake_card / "device"
        device.mkdir()
        (device / "serial").write_text("AB12CD34\n")
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp == "usb-1532:0528-AB12CD34"

    def test_usb_id_shortcut_without_serial(self, fake_card: Path) -> None:
        (fake_card / "usb_id").write_text("046d:0a45\n")
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp == "usb-046d:0a45"

    def test_parent_walk_finds_idvendor_idproduct(
        self,
        fake_card: Path,
    ) -> None:
        # No usb_id shortcut; force the parent-walk fallback by
        # placing idVendor/idProduct on the parent.
        parent = fake_card.parent
        (parent / "idVendor").write_text("1532\n")
        (parent / "idProduct").write_text("0528\n")
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp == "usb-1532:0528"

    def test_parent_walk_with_serial(self, fake_card: Path) -> None:
        parent = fake_card.parent
        (parent / "idVendor").write_text("046d\n")
        (parent / "idProduct").write_text("0a45\n")
        (parent / "serial").write_text("DEADBEEF\n")
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp == "usb-046d:0a45-DEADBEEF"

    def test_pci_card_no_usb_ancestor_returns_none(
        self,
        fake_card: Path,
    ) -> None:
        # No usb_id shortcut + no idVendor/idProduct anywhere
        # in the parent chain → None.
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp is None

    def test_malformed_idvendor_returns_none(
        self,
        fake_card: Path,
    ) -> None:
        # Parent-walk path: idVendor exists but isn't a 4-hex
        # string. Must fail safe rather than fingerprint with
        # garbage.
        parent = fake_card.parent
        (parent / "idVendor").write_text("not_hex\n")
        (parent / "idProduct").write_text("0528\n")
        fp = fingerprint_usb_device(sysfs_card_path=fake_card)
        assert fp is None

    def test_card_path_missing_returns_none(self, tmp_path: Path) -> None:
        # Caller passed a nonexistent sysfs path (hot-unplug race).
        fp = fingerprint_usb_device(sysfs_card_path=tmp_path / "missing")
        assert fp is None


class TestNonLinuxSysfs:
    def test_windows_caller_passing_sysfs_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Linux sysfs branch is gated on sys.platform == "linux".
        # A Windows caller that mistakenly passes a sysfs path
        # gets None instead of trying to read non-existent files.
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "win32")
        # Path "exists" (we just created it) but the platform
        # gate short-circuits before any file read.
        card = tmp_path / "card1"
        card.mkdir()
        fp = fingerprint_usb_device(sysfs_card_path=card)
        assert fp is None


class TestPnpWinsOverSysfs:
    def test_pnp_id_takes_precedence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When both arguments are passed, PnP wins (Windows
        # callers don't have sysfs to offer; Linux callers don't
        # have PnP IDs). The branch ordering documents the
        # expected platform-specificity.
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "linux")
        card = tmp_path / "card1"
        card.mkdir()
        (card / "usb_id").write_text("1111:2222\n")
        fp = fingerprint_usb_device(
            pnp_device_id=r"USB\VID_3333&PID_4444",
            sysfs_card_path=card,
        )
        # PnP wins → "3333:4444", not the sysfs's "1111:2222".
        assert fp == "usb-3333:4444"
