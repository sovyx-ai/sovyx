"""Unit tests for :mod:`sovyx.cli.commands.doctor` — general + voice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app

runner = CliRunner()


class _FakeSD:
    """Minimal ``sounddevice`` stub reused from preflight tests."""

    def __init__(
        self,
        *,
        host_apis: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        raise_on_query: Exception | None = None,
    ) -> None:
        self._host_apis = host_apis if host_apis is not None else [{"name": "WASAPI"}]
        self._devices = (
            devices if devices is not None else [{"name": "default-mic", "max_input_channels": 2}]
        )
        self._raise = raise_on_query

    def query_hostapis(self) -> list[dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return self._host_apis

    def query_devices(self) -> list[dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return self._devices


class TestDoctorVoiceDefault:
    """``sovyx doctor voice`` — the no-flag ADR §4.8 baseline."""

    def test_all_pass_renders_table_and_exits_zero(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.doctor.check_portaudio",
            lambda **_: _make_always_pass_check(),
        ):
            result = runner.invoke(app, ["doctor", "voice"])
        assert result.exit_code == 0
        assert "Voice Doctor" in result.stdout
        assert "PortAudio" in result.stdout
        # Two steps now: PortAudio (step 4) + Linux mixer sanity (step 9).
        # The mixer check skips on non-Linux hosts and passes with no hint,
        # so both steps are green and the summary is "All 2 step(s) passed".
        assert "All 2 step(s) passed" in result.stdout

    def test_failure_exit_code_equals_failure_count(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.doctor.check_portaudio",
            lambda **_: _make_always_fail_check(),
        ):
            result = runner.invoke(app, ["doctor", "voice"])
        assert result.exit_code == 1
        assert "FAIL" in result.stdout
        # Mixer check passes on non-Linux (skipped), PortAudio fails → 1/2.
        assert "1 of 2 step(s) failed" in result.stdout
        assert "portaudio_unavailable" in result.stdout

    def test_json_output_is_parseable_and_exits_with_failure_count(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.doctor.check_portaudio",
            lambda **_: _make_always_fail_check(),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["passed"] is False
        assert payload["first_failure_code"] == "portaudio_unavailable"
        assert payload["steps_run"] == 2
        assert len(payload["steps"]) == 2
        portaudio_step = next(s for s in payload["steps"] if s["step"] == 4)
        assert portaudio_step["code"] == "portaudio_unavailable"
        assert portaudio_step["passed"] is False
        mixer_step = next(s for s in payload["steps"] if s["step"] == 9)
        assert mixer_step["code"] == "linux_mixer_saturated"
        assert mixer_step["passed"] is True
        assert payload["device_filter"] is None

    def test_device_flag_is_recorded_in_json_payload(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.doctor.check_portaudio",
            lambda **_: _make_always_pass_check(),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--json", "--device", "{GUID-XYZ}"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["device_filter"] == "{GUID-XYZ}"
        assert payload["passed"] is True


class TestDoctorGeneral:
    """Existing top-level ``sovyx doctor`` still works under the sub-app."""

    def test_offline_runs_and_prints_summary(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Sovyx Health Check" in result.stdout
        assert "passed" in result.stdout.lower()

    def test_offline_json(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert len(payload) >= 4
        names = {item["name"] for item in payload}
        assert "Disk Space" in names


def _make_always_pass_check() -> Any:
    """Return an async check that passes with empty hint."""

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        return True, "", {"host_api_count": 2, "input_device_count": 1}

    return _check


def _make_always_fail_check() -> Any:
    """Return an async check that fails with an informative hint."""

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        return False, "Audio service appears to be down.", {"error": "simulated"}

    return _check


class TestFormatDetails:
    """Sanity on the small helper used for table hint fallback."""

    def test_empty_dict_becomes_empty_string(self) -> None:
        from sovyx.cli.commands.doctor import _format_details

        assert _format_details({}) == ""

    def test_non_dict_becomes_empty_string(self) -> None:
        from sovyx.cli.commands.doctor import _format_details

        assert _format_details(None) == ""

    def test_dict_rendered_as_comma_separated_kv(self) -> None:
        from sovyx.cli.commands.doctor import _format_details

        rendered = _format_details({"host_api_count": 2, "input_device_count": 1})
        assert "host_api_count=2" in rendered
        assert "input_device_count=1" in rendered
        assert "," in rendered
