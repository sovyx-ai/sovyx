"""Tests for ``sovyx voice setup`` (Phase 2.T2.1).

Validates the headless / non-dashboard mic-configuration command:

* ``list_capture_devices`` — enumeration + host_api lookup + import-error path.
* ``find_matching_device`` — index / exact / case-insensitive substring /
  ambiguous / no-match precedence.
* ``run_voice_setup`` async orchestrator — interactive picker + ``--input-device``
  flag + ``--non-interactive`` refusal + zero-devices error + persistence.
* ``voice_setup_cmd`` Typer wrapper via :class:`CliRunner` — exit codes,
  rendered output, mind resolver integration.

CLAUDE.md anti-pattern #36: async helpers patched via ``patch.object``
so ``AsyncMock`` autodetect fires; sounddevice patched on the
:mod:`sovyx.cli.commands.voice_setup` import namespace (not the source
:mod:`sounddevice` module) so the local lazy import resolves to our
fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.commands import voice_setup as voice_setup_mod
from sovyx.cli.commands.voice_setup import (
    CaptureDevice,
    VoiceSetupError,
    VoiceSetupRequiredError,
    find_matching_device,
    run_voice_setup,
)
from sovyx.cli.main import app
from sovyx.engine.types import MindId

if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


_RAZER = CaptureDevice(
    index=2,
    name="Razer BlackShark V2 Pro",
    host_api="Windows WASAPI",
    input_channels=1,
    default_samplerate=48000.0,
)
_BUILTIN = CaptureDevice(
    index=1,
    name="Built-in Microphone",
    host_api="Windows WASAPI",
    input_channels=2,
    default_samplerate=44100.0,
)
_ALSA_MIC = CaptureDevice(
    index=0,
    name="hw:1,0",
    host_api="ALSA",
    input_channels=1,
    default_samplerate=16000.0,
)


def _seed_mind_yaml(data_dir: Path, mind_id: str) -> Path:
    """Create <data_dir>/<mind_id>/mind.yaml and return the path."""
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = mind_dir / "mind.yaml"
    yaml_path.write_text(f"name: {mind_id}\nid: {mind_id}\n", encoding="utf-8")
    return yaml_path


# ====================================================================
# find_matching_device — substring / index / exact / ambiguous matrix
# ====================================================================


class TestFindMatchingDevice:
    def test_exact_name_match(self) -> None:
        assert find_matching_device([_RAZER, _BUILTIN], "Razer BlackShark V2 Pro") == _RAZER

    def test_case_insensitive_substring_match(self) -> None:
        assert find_matching_device([_RAZER, _BUILTIN], "razer") == _RAZER
        assert find_matching_device([_RAZER, _BUILTIN], "RAZER") == _RAZER
        assert find_matching_device([_RAZER, _BUILTIN], "built-in") == _BUILTIN

    def test_whitespace_collapsed_in_match(self) -> None:
        assert find_matching_device([_RAZER, _BUILTIN], "  Razer  ") == _RAZER

    def test_integer_index_match(self) -> None:
        assert find_matching_device([_RAZER, _BUILTIN, _ALSA_MIC], "2") == _RAZER
        assert find_matching_device([_RAZER, _BUILTIN, _ALSA_MIC], "0") == _ALSA_MIC

    def test_integer_index_out_of_range_errors(self) -> None:
        with pytest.raises(VoiceSetupError, match="No capture device at index 99"):
            find_matching_device([_RAZER], "99")

    def test_ambiguous_substring_errors_with_full_list(self) -> None:
        twin_a = CaptureDevice(0, "Test Mic A", "WASAPI", 1, 48000.0)
        twin_b = CaptureDevice(1, "Test Mic B", "WASAPI", 1, 48000.0)
        with pytest.raises(VoiceSetupError, match="Ambiguous"):
            find_matching_device([twin_a, twin_b], "Test Mic")

    def test_no_match_errors_with_available_list(self) -> None:
        with pytest.raises(VoiceSetupError, match="No capture device matches"):
            find_matching_device([_RAZER, _BUILTIN], "Nonexistent")

    def test_empty_specifier_errors(self) -> None:
        with pytest.raises(VoiceSetupError, match="Empty device specifier"):
            find_matching_device([_RAZER], "")

    def test_whitespace_only_specifier_errors(self) -> None:
        with pytest.raises(VoiceSetupError, match="Empty device specifier"):
            find_matching_device([_RAZER], "   ")


# ====================================================================
# run_voice_setup — async orchestrator
# ====================================================================


class TestRunVoiceSetup:
    @pytest.mark.asyncio()
    async def test_non_interactive_with_input_device_persists(self, tmp_path: Path) -> None:
        """--input-device + non_interactive=True writes mind.yaml via the helper."""
        _seed_mind_yaml(tmp_path, "jonny")
        persist_mock = AsyncMock()
        with (
            patch.object(
                voice_setup_mod,
                "list_capture_devices",
                return_value=[_RAZER, _BUILTIN],
            ),
            patch.object(voice_setup_mod, "persist_voice_input_device", persist_mock),
        ):
            result = await run_voice_setup(
                mind_id=MindId("jonny"),
                data_dir=tmp_path,
                input_device="Razer",
                non_interactive=True,
            )
        assert result.mind_id == "jonny"
        assert result.device_name == _RAZER.name
        assert result.host_api == _RAZER.host_api
        persist_mock.assert_awaited_once()
        kwargs = persist_mock.await_args.kwargs
        assert kwargs["mind_yaml_path"] == tmp_path / "jonny" / "mind.yaml"
        assert kwargs["device_name"] == _RAZER.name
        assert kwargs["host_api"] == _RAZER.host_api

    @pytest.mark.asyncio()
    async def test_non_interactive_without_input_device_raises_required(
        self, tmp_path: Path
    ) -> None:
        """non_interactive=True + no input_device → VoiceSetupRequiredError."""
        _seed_mind_yaml(tmp_path, "jonny")
        with (
            patch.object(
                voice_setup_mod,
                "list_capture_devices",
                return_value=[_RAZER, _BUILTIN],
            ),
            pytest.raises(VoiceSetupRequiredError, match="non-interactively"),
        ):
            await run_voice_setup(
                mind_id=MindId("jonny"),
                data_dir=tmp_path,
                input_device=None,
                non_interactive=True,
            )

    @pytest.mark.asyncio()
    async def test_zero_devices_raises_setup_error(self, tmp_path: Path) -> None:
        """Empty PortAudio enumeration → VoiceSetupError pointing at OS audio."""
        _seed_mind_yaml(tmp_path, "jonny")
        with patch.object(voice_setup_mod, "list_capture_devices", return_value=[]):
            with pytest.raises(VoiceSetupError, match="No capture devices"):
                await run_voice_setup(
                    mind_id=MindId("jonny"),
                    data_dir=tmp_path,
                    input_device="Razer",
                    non_interactive=True,
                )

    @pytest.mark.asyncio()
    async def test_input_device_no_match_propagates_error(self, tmp_path: Path) -> None:
        """--input-device matching nothing surfaces the available-list error."""
        _seed_mind_yaml(tmp_path, "jonny")
        with (
            patch.object(
                voice_setup_mod,
                "list_capture_devices",
                return_value=[_RAZER],
            ),
            pytest.raises(VoiceSetupError, match="No capture device matches"),
        ):
            await run_voice_setup(
                mind_id=MindId("jonny"),
                data_dir=tmp_path,
                input_device="Ghost",
                non_interactive=True,
            )


# ====================================================================
# Typer command — exit codes + rendered output
# ====================================================================


class TestVoiceSetupCommand:
    """End-to-end via CliRunner — Phase 2.T2.1 wire-up."""

    def test_command_registered_under_voice(self) -> None:
        """``sovyx voice setup --help`` exits 0 + describes the command."""
        result = runner.invoke(app, ["voice", "setup", "--help"])
        assert result.exit_code == 0
        # Phase 2.T2.1: the help text mentions the persistence target.
        assert "mind.yaml" in result.output or "mic" in result.output.lower()

    def test_command_non_interactive_with_input_device_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path — --non-interactive --input-device 'Razer' persists."""
        _seed_mind_yaml(tmp_path, "jonny")
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))
        persist_mock = AsyncMock()
        with (
            patch.object(
                voice_setup_mod,
                "list_capture_devices",
                return_value=[_RAZER, _BUILTIN],
            ),
            patch.object(voice_setup_mod, "persist_voice_input_device", persist_mock),
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "setup",
                    "--mind-id",
                    "jonny",
                    "--input-device",
                    "Razer",
                    "--non-interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Mic configured" in result.output
        assert _RAZER.name in result.output
        persist_mock.assert_awaited_once()

    def test_command_non_interactive_without_input_device_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--non-interactive without --input-device → VoiceSetupRequiredError → exit 2."""
        _seed_mind_yaml(tmp_path, "jonny")
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))
        with patch.object(
            voice_setup_mod,
            "list_capture_devices",
            return_value=[_RAZER, _BUILTIN],
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "setup",
                    "--mind-id",
                    "jonny",
                    "--non-interactive",
                ],
            )
        assert result.exit_code == 2, result.output
        assert "non-interactively" in result.output

    def test_command_invalid_input_device_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--input-device with no matching enumeration → VoiceSetupError → exit 1."""
        _seed_mind_yaml(tmp_path, "jonny")
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))
        with patch.object(
            voice_setup_mod,
            "list_capture_devices",
            return_value=[_RAZER],
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "setup",
                    "--mind-id",
                    "jonny",
                    "--input-device",
                    "Ghost",
                    "--non-interactive",
                ],
            )
        assert result.exit_code == 1, result.output
        assert "No capture device matches" in result.output

    def test_command_missing_mind_id_errors_via_resolver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--mind-id <ghost> with no <ghost>/mind.yaml → resolver BadParameter."""
        _seed_mind_yaml(tmp_path, "jonny")
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))
        result = runner.invoke(
            app,
            [
                "voice",
                "setup",
                "--mind-id",
                "ghost",
                "--input-device",
                "Razer",
                "--non-interactive",
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "ghost" in combined
        assert "not found" in combined.lower()


# ====================================================================
# list_capture_devices — sounddevice import + enumeration paths
# ====================================================================


class TestListCaptureDevices:
    def test_import_error_raises_setup_error(self) -> None:
        """sounddevice ImportError → VoiceSetupError with install hint."""

        def _raise_import_error(*_args: object, **_kwargs: object) -> object:
            msg = "No module named 'sounddevice'"
            raise ImportError(msg)

        # voice_setup imports sounddevice lazily inside the function;
        # patch the import machinery by stubbing `sys.modules` so the
        # lazy `import sounddevice as sd` line raises.
        import sys as _sys

        original = _sys.modules.pop("sounddevice", None)
        with patch.dict(_sys.modules, {"sounddevice": None}):
            with pytest.raises(VoiceSetupError, match="PortAudio"):
                voice_setup_mod.list_capture_devices()
        if original is not None:
            _sys.modules["sounddevice"] = original

    def test_filters_to_input_devices_only(self) -> None:
        """Devices with max_input_channels=0 are skipped (output-only / system)."""
        fake_devices = [
            {"name": "OnlyOutput", "max_input_channels": 0, "hostapi": 0},
            {
                "name": "Mic",
                "max_input_channels": 1,
                "hostapi": 0,
                "default_samplerate": 48000.0,
            },
        ]
        fake_hostapis = [{"name": "TestAPI"}]
        with patch.dict(
            "sys.modules",
            {"sounddevice": _StubSounddevice(fake_devices, fake_hostapis)},
        ):
            result = voice_setup_mod.list_capture_devices()
        assert len(result) == 1
        assert result[0].name == "Mic"
        assert result[0].host_api == "TestAPI"


class _StubSounddevice:
    """Minimal sounddevice replacement for the lazy import."""

    def __init__(
        self,
        devices: list[dict[str, object]],
        hostapis: list[dict[str, object]],
    ) -> None:
        self._devices = devices
        self._hostapis = hostapis

    def query_devices(self) -> list[dict[str, object]]:
        return self._devices

    def query_hostapis(self) -> list[dict[str, object]]:
        return self._hostapis
