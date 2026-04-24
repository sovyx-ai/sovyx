"""Tests for :mod:`sovyx.voice.health._fingerprint_macos`.

Sprint 3 deliverable: per-endpoint stable fingerprint on macOS
that replaces the ``{surrogate-...}`` SHA256 with a CoreAudio-UID
derived ID equivalent to Windows MMDevice GUIDs / Linux
``{linux-...}`` fingerprints. The detector shells out to
``system_profiler -json SPAudioDataType``; tests inject a fake
subprocess callable so no real system_profiler runs.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.health._fingerprint_macos import (
    compute_macos_endpoint_fingerprint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    *,
    name: str = "MacBook Pro Microphone",
    max_input_channels: int = 2,
    max_output_channels: int = 0,
) -> DeviceEntry:
    return DeviceEntry(
        index=0,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name="Core Audio",
        max_input_channels=max_input_channels,
        max_output_channels=max_output_channels,
        default_samplerate=48_000,
        is_os_default=True,
    )


class _FakeCompleted:
    """Duck-typed :class:`subprocess.CompletedProcess`."""

    def __init__(self, stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _sp_payload(*items: dict[str, Any]) -> str:
    """Build a realistic ``system_profiler -json`` payload."""
    return json.dumps(
        {
            "SPAudioDataType": [
                {
                    "_name": "Devices",
                    "_items": list(items),
                },
            ],
        },
    )


def _fake_run(stdout: str = "", *, returncode: int = 0) -> Any:
    """Return a ``subprocess.run`` stand-in."""

    def _run(argv, **_kwargs):  # noqa: ANN001 — mimic subprocess.run signature
        _ = argv
        return _FakeCompleted(stdout=stdout, returncode=returncode)

    return _run


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


class TestPlatformGate:
    """Non-darwin hosts must collapse to None without running subprocess."""

    def test_non_darwin_returns_none(self) -> None:
        with patch("sys.platform", "win32"):
            result = compute_macos_endpoint_fingerprint(_entry(), run=_fake_run("{}"))
        assert result is None

    def test_linux_returns_none(self) -> None:
        with patch("sys.platform", "linux"):
            result = compute_macos_endpoint_fingerprint(_entry(), run=_fake_run("{}"))
        assert result is None


# ---------------------------------------------------------------------------
# Happy paths — each transport type
# ---------------------------------------------------------------------------


class TestTransportMapping:
    """The transport prefix in the fingerprint must reflect the
    normalised form from ``_TRANSPORT_MAP``."""

    def test_builtin_microphone(self) -> None:
        payload = _sp_payload(
            {
                "_name": "MacBook Pro Microphone",
                "coreaudio_device_id": "BuiltInMicrophoneDevice",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 2,
                "coreaudio_device_output": 0,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="MacBook Pro Microphone"),
                run=_fake_run(payload),
            )
        assert result == "{macos-builtin-BuiltInMicrophoneDevice-input}"

    def test_usb_audio_device(self) -> None:
        payload = _sp_payload(
            {
                "_name": "Razer Seiren X",
                "coreaudio_device_id": "AppleUSBAudioEngine:Razer:Seiren:0001",
                "coreaudio_device_transport": "coreaudio_device_type_usb",
                "coreaudio_device_input": 2,
                "coreaudio_device_output": 0,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Razer Seiren X"),
                run=_fake_run(payload),
            )
        # Colons in the UID must be sanitised.
        assert result is not None
        assert result.startswith("{macos-usb-AppleUSBAudioEngine_Razer_Seiren_0001-")
        assert result.endswith("-input}")

    def test_bluetooth_device(self) -> None:
        payload = _sp_payload(
            {
                "_name": "AirPods Pro",
                "coreaudio_device_id": "BluetoothA2DP_AA_BB_CC_DD_EE_FF",
                "coreaudio_device_transport": "coreaudio_device_type_bluetooth",
                "coreaudio_device_input": 1,
                "coreaudio_device_output": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="AirPods Pro", max_input_channels=1, max_output_channels=2),
                run=_fake_run(payload),
            )
        # Duplex path — both channel counts populated.
        assert result == "{macos-bluetooth-BluetoothA2DP_AA_BB_CC_DD_EE_FF-duplex}"

    def test_aggregate_device(self) -> None:
        payload = _sp_payload(
            {
                "_name": "BlackHole 2ch",
                "coreaudio_device_id": "BlackHoleAudioDevice2ch_UID",
                "coreaudio_device_transport": "coreaudio_device_type_aggregate",
                "coreaudio_device_input": 2,
                "coreaudio_device_output": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="BlackHole 2ch", max_input_channels=2, max_output_channels=2),
                run=_fake_run(payload),
            )
        assert result == "{macos-aggregate-BlackHoleAudioDevice2ch_UID-duplex}"

    def test_unknown_transport_passes_through(self) -> None:
        payload = _sp_payload(
            {
                "_name": "Exotic Interface",
                "coreaudio_device_id": "exotic",
                "coreaudio_device_transport": "coreaudio_device_type_newbus",
                "coreaudio_device_input": 1,
                "coreaudio_device_output": 0,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Exotic Interface", max_input_channels=1),
                run=_fake_run(payload),
            )
        # Unmapped transport lands verbatim so we still get some signal.
        assert result == "{macos-coreaudio_device_type_newbus-exotic-input}"


# ---------------------------------------------------------------------------
# Identity fallback
# ---------------------------------------------------------------------------


class TestIdentityFallback:
    """When deviceUID is missing we fall back to sanitised _name."""

    def test_no_uid_uses_name(self) -> None:
        payload = _sp_payload(
            {
                "_name": "Weird Device",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 1,
                "coreaudio_device_output": 0,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Weird Device"),
                run=_fake_run(payload),
            )
        # Name contains a space → sanitised to underscore.
        assert result == "{macos-builtin-Weird_Device-input}"

    def test_no_uid_no_name_returns_none(self) -> None:
        payload = _sp_payload(
            {
                "_name": "",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 1,
            },
        )
        with patch("sys.platform", "darwin"):
            # Empty name → target match fails anyway (we only match on name).
            result = compute_macos_endpoint_fingerprint(
                _entry(name=""),
                run=_fake_run(payload),
            )
        assert result is None


# ---------------------------------------------------------------------------
# Direction resolution
# ---------------------------------------------------------------------------


class TestDirectionResolution:
    @pytest.mark.parametrize(
        ("inputs", "outputs", "expected"),
        [
            (2, 0, "input"),
            (0, 2, "output"),
            (2, 2, "duplex"),
            (0, 0, "input"),  # fallback — capture-path default
        ],
    )
    def test_direction_matrix(self, inputs: int, outputs: int, expected: str) -> None:
        payload = _sp_payload(
            {
                "_name": "Test Device",
                "coreaudio_device_id": "TestDevice",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": inputs,
                "coreaudio_device_output": outputs,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(
                    name="Test Device",
                    max_input_channels=max(inputs, 0),
                    max_output_channels=max(outputs, 0),
                ),
                run=_fake_run(payload),
            )
        assert result is not None
        assert result.endswith(f"-{expected}}}")

    def test_stringy_channel_count_accepted(self) -> None:
        # Older macOS sometimes emits the channel count as a string.
        payload = _sp_payload(
            {
                "_name": "Old macOS Device",
                "coreaudio_device_id": "OldDev",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": "2",
                "coreaudio_device_output": "0",
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Old macOS Device"),
                run=_fake_run(payload),
            )
        assert result == "{macos-builtin-OldDev-input}"


# ---------------------------------------------------------------------------
# Device matching
# ---------------------------------------------------------------------------


class TestDeviceMatching:
    """``_name`` match is case-insensitive and whitespace-tolerant."""

    def test_case_insensitive(self) -> None:
        payload = _sp_payload(
            {
                "_name": "MacBook Pro Microphone",
                "coreaudio_device_id": "BuiltInMic",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="MACBOOK PRO MICROPHONE"),
                run=_fake_run(payload),
            )
        assert result is not None

    def test_whitespace_normalised(self) -> None:
        payload = _sp_payload(
            {
                "_name": "  MacBook Pro Microphone  ",
                "coreaudio_device_id": "BuiltInMic",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="MacBook Pro Microphone"),
                run=_fake_run(payload),
            )
        assert result is not None

    def test_no_match_returns_none(self) -> None:
        payload = _sp_payload(
            {
                "_name": "Some Other Device",
                "coreaudio_device_id": "other",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="MacBook Pro Microphone"),
                run=_fake_run(payload),
            )
        assert result is None


# ---------------------------------------------------------------------------
# Subprocess degradation
# ---------------------------------------------------------------------------


class TestSubprocessDegradation:
    """Every subprocess failure mode collapses to None cleanly."""

    def test_file_not_found(self) -> None:
        def _run(argv, **_kwargs):  # noqa: ANN001
            _ = argv
            raise FileNotFoundError("system_profiler")

        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(_entry(), run=_run)
        assert result is None

    def test_timeout(self) -> None:
        def _run(argv, **_kwargs):  # noqa: ANN001
            _ = argv
            raise subprocess.TimeoutExpired(cmd=argv, timeout=3.0)

        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(_entry(), run=_run)
        assert result is None

    def test_non_zero_exit(self) -> None:
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(),
                run=_fake_run(stdout="", returncode=1),
            )
        assert result is None

    def test_malformed_json(self) -> None:
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(),
                run=_fake_run(stdout="<<not json>>"),
            )
        assert result is None

    def test_json_is_not_dict(self) -> None:
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(),
                run=_fake_run(stdout="[]"),
            )
        assert result is None

    def test_empty_items(self) -> None:
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(),
                run=_fake_run(
                    stdout=json.dumps({"SPAudioDataType": [{"_items": []}]}),
                ),
            )
        assert result is None


# ---------------------------------------------------------------------------
# UID sanitisation — long strings clamped
# ---------------------------------------------------------------------------


class TestUidSanitisation:
    """Excessively long UIDs are truncated to keep logs bounded."""

    def test_very_long_uid_clamped(self) -> None:
        long_uid = "A" * 500
        payload = _sp_payload(
            {
                "_name": "Test",
                "coreaudio_device_id": long_uid,
                "coreaudio_device_transport": "coreaudio_device_type_usb",
                "coreaudio_device_input": 2,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Test"),
                run=_fake_run(payload),
            )
        assert result is not None
        # Fingerprint length must stay < 150 even with a 500-char UID.
        assert len(result) < 150

    def test_uid_with_braces_sanitised(self) -> None:
        # Braces would break the fingerprint envelope parsing.
        payload = _sp_payload(
            {
                "_name": "Braced Device",
                "coreaudio_device_id": "{some-uid}",
                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                "coreaudio_device_input": 1,
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Braced Device"),
                run=_fake_run(payload),
            )
        assert result is not None
        # Braces in the identity portion must be stripped.
        inner = result.removeprefix("{macos-builtin-").removesuffix("-input}")
        assert "{" not in inner
        assert "}" not in inner


# ---------------------------------------------------------------------------
# Hackintosh-shape tolerance
# ---------------------------------------------------------------------------


class TestHackintoshShape:
    """Some hackintosh installs emit devices directly at the top level."""

    def test_direct_device_entries_under_spaudiodatatype(self) -> None:
        # Unusual shape — devices are at the group level, not under _items.
        payload = json.dumps(
            {
                "SPAudioDataType": [
                    {
                        "_name": "Hackintosh Mic",
                        "coreaudio_device_id": "HackintoshMic",
                        "coreaudio_device_transport": "coreaudio_device_type_builtin",
                        "coreaudio_device_input": 2,
                    },
                ],
            },
        )
        with patch("sys.platform", "darwin"):
            result = compute_macos_endpoint_fingerprint(
                _entry(name="Hackintosh Mic"),
                run=_fake_run(payload),
            )
        assert result == "{macos-builtin-HackintoshMic-input}"
