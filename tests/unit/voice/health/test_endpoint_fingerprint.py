"""Tests for the cross-platform endpoint → USB fingerprint façade.

Phase 5 / T5.43 + T5.51 wire-up. The façade
:mod:`sovyx.voice.health._endpoint_fingerprint` dispatches to:

* Windows: :mod:`sovyx.voice.health._endpoint_fingerprint_win`
  (IMMDevice → IPropertyStore → PnP device-instance ID → USB shape).
* Linux: parses inline ``{linux-usb-VVVV:PPPP-...}`` shape produced by
  :mod:`sovyx.voice.health._fingerprint_linux`.
* macOS / other: returns None.

Coverage:

* Empty endpoint ID short-circuits to None on every platform.
* Linux happy path: ``{linux-usb-VVVV:PPPP-N-capture}`` →
  ``"usb-VVVV:PPPP"`` (lowercase).
* Linux happy path: surrogate hash + PCI codec shape return None.
* Linux malformed inputs return None safely.
* Platform dispatch via ``sys.platform`` patch.
* Windows dispatch delegates to the win module's
  :func:`resolve_endpoint_to_usb_fingerprint`.
* macOS / unknown platforms return None unconditionally.
"""

from __future__ import annotations

from unittest.mock import patch

from sovyx.voice.health._endpoint_fingerprint import (
    _resolve_linux,
    resolve_endpoint_to_usb_fingerprint,
)


class TestPublicFacade:
    def test_empty_endpoint_id_returns_none(self) -> None:
        assert resolve_endpoint_to_usb_fingerprint("") is None

    def test_dispatches_to_linux_resolver(self) -> None:
        with patch(
            "sovyx.voice.health._endpoint_fingerprint.sys.platform",
            "linux",
        ):
            result = resolve_endpoint_to_usb_fingerprint(
                "{linux-usb-1532:0543-0-capture}",
            )
        assert result == "usb-1532:0543"

    def test_dispatches_to_windows_resolver(self) -> None:
        with (
            patch(
                "sovyx.voice.health._endpoint_fingerprint.sys.platform",
                "win32",
            ),
            patch(
                "sovyx.voice.health._endpoint_fingerprint_win.resolve_endpoint_to_usb_fingerprint",
                return_value="usb-1532:0543-WIN-DELEGATED",
            ) as mock_win,
        ):
            result = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert result == "usb-1532:0543-WIN-DELEGATED"
        mock_win.assert_called_once_with("{0.0.1.00000000}.{guid}")

    def test_macos_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._endpoint_fingerprint.sys.platform",
            "darwin",
        ):
            assert resolve_endpoint_to_usb_fingerprint("anything") is None

    def test_unknown_platform_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._endpoint_fingerprint.sys.platform",
            "freebsd",
        ):
            assert resolve_endpoint_to_usb_fingerprint("anything") is None


class TestLinuxResolver:
    def test_usb_capture_shape(self) -> None:
        # Format produced by :mod:`sovyx.voice.health._fingerprint_linux`
        # for a USB-audio device.
        assert _resolve_linux("{linux-usb-1532:0543-0-capture}") == "usb-1532:0543"

    def test_usb_playback_shape(self) -> None:
        # Same shape, playback direction. The fingerprint scheme is
        # bus-typed so direction doesn't change the VID/PID extraction.
        assert _resolve_linux("{linux-usb-1532:0543-2-playback}") == "usb-1532:0543"

    def test_uppercase_hex_normalized_to_lowercase(self) -> None:
        # The T5.43 canonical form is lowercase hex; the resolver must
        # normalize regardless of how the upstream emitted the guid.
        assert _resolve_linux("{linux-usb-AABB:CCDD-0-capture}") == "usb-aabb:ccdd"

    def test_pci_codec_shape_returns_none(self) -> None:
        # Internal HDA codec — non-USB hardware, no fingerprint possible.
        assert _resolve_linux("{linux-pci-0000_00_1f.3-codec-14F1:5045-0-capture}") is None

    def test_surrogate_hash_returns_none(self) -> None:
        # Sysfs unreadable at enumeration time → fallback hash.
        # No USB info to extract.
        assert _resolve_linux("{surrogate-aabbccdd11223344}") is None

    def test_malformed_short_vid_returns_none(self) -> None:
        # 3-hex-digit VID is malformed — regex requires exactly 4.
        assert _resolve_linux("{linux-usb-153:0543-0-capture}") is None

    def test_malformed_non_hex_returns_none(self) -> None:
        # Non-hex chars in VID slot fail the regex.
        assert _resolve_linux("{linux-usb-XXXX:0543-0-capture}") is None

    def test_empty_string_returns_none(self) -> None:
        assert _resolve_linux("") is None

    def test_random_string_returns_none(self) -> None:
        # A free-form non-fingerprint string must not accidentally
        # match the regex.
        assert _resolve_linux("hello world") is None
