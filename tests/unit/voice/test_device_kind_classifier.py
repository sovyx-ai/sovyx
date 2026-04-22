"""T12.1 — unit tests for ``classify_device_kind`` + ``extract_alsa_card_device``.

Covers the :mod:`sovyx.voice.device_enum` classifier introduced by
voice-linux-cascade-root-fix T2. Exhaustive over the ``DeviceKind``
surface; non-Linux platforms always collapse to ``UNKNOWN``.
"""

from __future__ import annotations

import pytest

from sovyx.voice.device_enum import (
    DeviceKind,
    classify_device_kind,
    extract_alsa_card_device,
)


class TestClassifyDeviceKindLinux:
    """Linux — the only platform where the classifier does real work."""

    @pytest.mark.parametrize(
        "name",
        [
            "hw:0,0",
            "hw:1,0",
            "plughw:1,0",
            "HD-Audio Generic: SN6180 Analog (hw:1,0)",
            "USB Mic (hw:2,0)",
        ],
    )
    def test_hardware_nodes(self, name: str) -> None:
        assert (
            classify_device_kind(
                name=name,
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.HARDWARE
        )

    @pytest.mark.parametrize(
        "name",
        [
            "pipewire",
            "pulse",
            "pulseaudio",
            "jack",
        ],
    )
    def test_session_manager_virtuals(self, name: str) -> None:
        assert (
            classify_device_kind(
                name=name,
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.SESSION_MANAGER_VIRTUAL
        )

    @pytest.mark.parametrize("name", ["default", "sysdefault"])
    def test_os_default(self, name: str) -> None:
        assert (
            classify_device_kind(
                name=name,
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.OS_DEFAULT
        )

    @pytest.mark.parametrize(
        "name",
        [
            "random-unmatched-name",
            "hdmi",
            "my-cool-device",
            "",
            "    ",
        ],
    )
    def test_unknown_fallthrough(self, name: str) -> None:
        assert (
            classify_device_kind(
                name=name,
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.UNKNOWN
        )

    def test_case_insensitivity(self) -> None:
        # Linux PCM names are lowercased by convention; classifier strips
        # + lowercases internally so PIPEWIRE / Pipewire / pipewire all
        # collapse to the same kind.
        for variant in ("PIPEWIRE", "Pipewire", "pipewire"):
            assert (
                classify_device_kind(
                    name=variant,
                    host_api_name="ALSA",
                    platform_key="linux",
                )
                is DeviceKind.SESSION_MANAGER_VIRTUAL
            )

    def test_prefix_anchoring_is_strict(self) -> None:
        # "default-sink-alias" starts with "default" but has no word
        # boundary — should NOT match OS_DEFAULT.
        assert (
            classify_device_kind(
                name="default-sink-alias",
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.UNKNOWN
        )

    def test_hw_substring_matches_inside_vendor_prefix(self) -> None:
        # Vendor-style name with hw:X,Y embedded in parens.
        assert (
            classify_device_kind(
                name="HDA Intel PCH: ALC3254 Analog (hw:0,0)",
                host_api_name="ALSA",
                platform_key="linux",
            )
            is DeviceKind.HARDWARE
        )


class TestClassifyDeviceKindNonLinux:
    """Windows / macOS / other platforms — always UNKNOWN."""

    @pytest.mark.parametrize("platform", ["win32", "darwin", "cygwin", "freebsd"])
    @pytest.mark.parametrize(
        "name",
        [
            "pipewire",
            "default",
            "hw:0,0",
            "HD-Audio Generic",
            "Microfone (Razer BlackShark V2 Pro)",
        ],
    )
    def test_always_unknown(self, platform: str, name: str) -> None:
        assert (
            classify_device_kind(
                name=name,
                host_api_name="Windows WASAPI",
                platform_key=platform,
            )
            is DeviceKind.UNKNOWN
        )


class TestExtractAlsaCardDevice:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("hw:0,0", (0, 0)),
            ("hw:1,0", (1, 0)),
            ("hw:2,5", (2, 5)),
            ("plughw:3,2", (3, 2)),
            ("HD-Audio Generic: SN6180 Analog (hw:1,0)", (1, 0)),
            ("   hw:10,7   ", (10, 7)),
        ],
    )
    def test_valid_forms(self, name: str, expected: tuple[int, int]) -> None:
        assert extract_alsa_card_device(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "default",
            "pipewire",
            "",
            "hw:",
            "hw:0",
            "hw:a,b",
            "Razer BlackShark V2 Pro",
            "hw-1-0",  # wrong separator
        ],
    )
    def test_no_match(self, name: str) -> None:
        assert extract_alsa_card_device(name) is None

    def test_first_match_wins_for_multiple_occurrences(self) -> None:
        # Contrived: a name with two hw: tuples — we return the first.
        assert extract_alsa_card_device("hw:0,0 + hw:1,1 composite") == (0, 0)
