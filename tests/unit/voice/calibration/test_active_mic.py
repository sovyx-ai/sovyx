"""Tests for resolve_active_mic_card (v0.31.5 LE-1).

The helper bridges v0.31.4 GAP 5: it maps the operator's persisted
``MindConfig.voice_input_device_name`` to an ALSA card index by
parsing ``arecord -l`` output. ``None`` is the safe fallback that
preserves pre-v0.31.4 behaviour at every caller.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from sovyx.voice.calibration import _active_mic
from sovyx.voice.calibration._active_mic import resolve_active_mic_card

_ARECORD_L_OUTPUT = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC256 Analog [ALC256 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Pro [Razer BlackShark V2 Pro], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["arecord", "-l"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestResolveActiveMicCard:
    """resolve_active_mic_card returns the matching ALSA card index."""

    def test_substring_match_returns_card_index(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_case_insensitive_partial_match(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_first_card_match(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="HDA Intel PCH")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 0

    def test_none_mind_config_returns_none(self) -> None:
        assert resolve_active_mic_card(mind_config=None) is None

    def test_empty_persisted_name_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="")
        assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_whitespace_persisted_name_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="   ")
        assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_unavailable_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with patch.object(_active_mic.shutil, "which", return_value=None):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_oserror_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(_active_mic.subprocess, "run", side_effect=OSError("boom")),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_timeout_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="arecord", timeout=5),
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_nonzero_exit_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(_active_mic.subprocess, "run", return_value=_completed("", returncode=1)),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_no_match_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Bose QuietComfort")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_missing_attr_returns_none(self) -> None:
        mind_config = SimpleNamespace()
        assert resolve_active_mic_card(mind_config=mind_config) is None


_PACTL_LIST_SOURCES_OUTPUT = """Source #46
\tState: SUSPENDED
\tName: alsa_input.pci-0000_00_1f.3.analog-stereo
\tDescription: Built-in Audio Analog Stereo
\tDriver: PipeWire
\tProperties:
\t\talsa.card = "0"
\t\talsa.card_name = "HDA Intel PCH"
\t\tdevice.api = "alsa"

Source #47
\tState: IDLE
\tName: alsa_input.usb-Razer_Razer_BlackShark_V2_Pro-00.analog-stereo
\tDescription: Razer BlackShark V2 Pro Wireless Analog Stereo
\tDriver: PipeWire
\tProperties:
\t\talsa.card = "2"
\t\talsa.card_name = "Pro"
\t\tdevice.api = "alsa"
"""


def _pactl_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["pactl", "list", "sources"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _which_only(*available: str) -> object:
    """Return a ``shutil.which`` stub that resolves only the given names."""

    def _which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in available else None

    return _which


class TestResolveActiveMicCardPactl:
    """v0.31.6 T2.1: pactl path runs before the arecord fallback."""

    def test_pactl_match_returns_card_from_alsa_card_property(self) -> None:
        mind_config = SimpleNamespace(
            voice_input_device_name="Razer BlackShark V2 Pro Wireless Analog Stereo"
        )
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("pactl")),
            patch.object(
                _active_mic.subprocess,
                "run",
                return_value=_pactl_completed(_PACTL_LIST_SOURCES_OUTPUT),
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_pactl_unavailable_falls_through_to_arecord(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        # ``pactl`` missing → fall through; ``arecord`` matches.
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("arecord")),
            patch.object(
                _active_mic.subprocess,
                "run",
                return_value=_completed(_ARECORD_L_OUTPUT),
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_pactl_no_match_falls_through_to_arecord(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        # pactl runs but lists only an unrelated source; arecord then matches.
        unrelated_pactl = """Source #46
\tName: alsa_input.foo
\tDescription: Some Other Mic Analog Stereo
\tProperties:
\t\talsa.card = "9"
"""
        responses = [
            _pactl_completed(unrelated_pactl),
            _completed(_ARECORD_L_OUTPUT),
        ]
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("pactl", "arecord")),
            patch.object(_active_mic.subprocess, "run", side_effect=responses),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_pactl_timeout_falls_through_to_arecord(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        responses: list[object] = [
            subprocess.TimeoutExpired(cmd="pactl", timeout=5),
            _completed(_ARECORD_L_OUTPUT),
        ]
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("pactl", "arecord")),
            patch.object(_active_mic.subprocess, "run", side_effect=responses),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_pactl_nonzero_exit_falls_through_to_arecord(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        responses = [
            _pactl_completed("", returncode=1),
            _completed(_ARECORD_L_OUTPUT),
        ]
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("pactl", "arecord")),
            patch.object(_active_mic.subprocess, "run", side_effect=responses),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_normalize_device_name_strips_pulse_suffixes(self) -> None:
        normalised = _active_mic._normalize_device_name(
            "Razer BlackShark V2 Pro Wireless Analog Stereo"
        )
        assert "razer blackshark v2 pro" in normalised
        assert "analog stereo" not in normalised

    def test_normalize_strips_hw_suffix(self) -> None:
        normalised = _active_mic._normalize_device_name("Razer: USB Audio (hw:2,0)")
        assert normalised == "razer"

    def test_pactl_partial_match_via_normalised_substring(self) -> None:
        # pactl Description has the long Pulse name; persisted is the shorter
        # operator wizard pick. Forward substring after normalise must match.
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        with (
            patch.object(_active_mic.shutil, "which", side_effect=_which_only("pactl")),
            patch.object(
                _active_mic.subprocess,
                "run",
                return_value=_pactl_completed(_PACTL_LIST_SOURCES_OUTPUT),
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2
