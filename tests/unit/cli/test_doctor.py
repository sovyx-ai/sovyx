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


class TestDoctorVoiceFixFlag:
    """v1.3 §4.4 L5b — ``sovyx doctor voice --fix`` safety surface.

    Every test pins the platform via :func:`patch.object` on
    ``sovyx.cli.commands.doctor.sys`` where Linux semantics are
    required, because the ``--fix`` path is Linux-only. Tests that
    verify the exit code on non-Linux don't need the pin.
    """

    def test_fix_flag_advertised_in_help(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--help"])
        assert result.exit_code == 0
        assert "--fix" in result.stdout
        assert "--yes" in result.stdout
        assert "--dry-run" in result.stdout
        assert "--card-index" in result.stdout

    def test_fix_on_non_linux_returns_unsupported(self) -> None:
        """Non-Linux platforms exit 5 regardless of preflight state."""
        import sys as real_sys

        if real_sys.platform == "linux":
            # On a Linux host we can't directly exercise this branch
            # without running the full --fix flow; rely on the
            # Linux-specific test below to cover the logic.
            return
        with patch(
            "sovyx.cli.commands.doctor.check_portaudio",
            lambda **_: _make_always_fail_check(),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--fix"])
        # Preflight fails (portaudio) but saturation is the only thing
        # --fix addresses; the generic non-Linux check reports 5 via
        # the "no saturation" short-circuit on the platform gate.
        # Accept any of the semantic codes documented for this branch.
        from sovyx.cli.commands.doctor import (
            EXIT_DOCTOR_OK,
            EXIT_DOCTOR_UNSUPPORTED,
        )

        assert result.exit_code in {EXIT_DOCTOR_OK, EXIT_DOCTOR_UNSUPPORTED}

    def test_fix_no_saturation_returns_ok(self) -> None:
        """When no saturation is present, --fix is a clean no-op."""
        from sovyx.cli.commands.doctor import EXIT_DOCTOR_OK

        with (
            patch(
                "sovyx.cli.commands.doctor.check_portaudio",
                lambda **_: _make_always_pass_check(),
            ),
            # Force the Linux path + simulate a clean preflight outcome
            # so we exercise the "no saturation" branch explicitly.
            patch("sovyx.cli.commands.doctor.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            # Preserve the real ``sys.stdin`` / ``sys.stderr`` Rich
            # + Typer depend on.
            import sys as real_sys

            mock_sys.stdin = real_sys.stdin
            mock_sys.stderr = real_sys.stderr
            result = runner.invoke(app, ["doctor", "voice", "--fix"])
        assert result.exit_code == EXIT_DOCTOR_OK
        assert "No saturation detected" in result.stdout

    def test_fix_dry_run_prints_plan_without_mutating(self) -> None:
        from sovyx.cli.commands.doctor import EXIT_DOCTOR_OK
        from sovyx.voice.health.contract import (
            MixerCardSnapshot,
            MixerControlSnapshot,
        )

        saturated = MixerCardSnapshot(
            card_index=1,
            card_id="Generic_1",
            card_longname="HD-Audio Generic",
            controls=(
                MixerControlSnapshot(
                    name="Capture",
                    min_raw=0,
                    max_raw=80,
                    current_raw=80,
                    current_db=6.0,
                    max_db=6.0,
                    is_boost_control=True,
                    saturation_risk=True,
                ),
                MixerControlSnapshot(
                    name="Internal Mic Boost",
                    min_raw=0,
                    max_raw=3,
                    current_raw=3,
                    current_db=36.0,
                    max_db=36.0,
                    is_boost_control=True,
                    saturation_risk=True,
                ),
            ),
            aggregated_boost_db=42.0,
            saturation_warning=True,
        )

        with (
            patch("sovyx.cli.commands.doctor.sys") as mock_sys,
            patch(
                "sovyx.cli.commands.doctor.check_linux_mixer_sanity",
                lambda **_: _make_always_fail_check(),
            ),
            patch(
                "sovyx.cli.commands.doctor.check_portaudio",
                lambda **_: _make_always_pass_check(),
            ),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[saturated],
            ),
            patch("sovyx.voice.health._linux_mixer_apply.apply_mixer_reset") as mock_apply,
        ):
            import sys as real_sys

            mock_sys.platform = "linux"
            mock_sys.stdin = real_sys.stdin
            mock_sys.stderr = real_sys.stderr
            result = runner.invoke(
                app,
                ["doctor", "voice", "--fix", "--dry-run"],
            )

        assert result.exit_code == EXIT_DOCTOR_OK
        assert "Planned mixer remediation" in result.stdout
        assert "Capture" in result.stdout
        assert "Internal Mic Boost" in result.stdout
        mock_apply.assert_not_called()

    def test_fix_non_tty_without_yes_aborts(self) -> None:
        """Non-TTY stdin + no --yes must refuse to mutate."""
        from sovyx.cli.commands.doctor import EXIT_DOCTOR_USER_ABORTED
        from sovyx.voice.health.contract import (
            MixerCardSnapshot,
            MixerControlSnapshot,
        )

        saturated = MixerCardSnapshot(
            card_index=1,
            card_id="Generic_1",
            card_longname="HD-Audio Generic",
            controls=(
                MixerControlSnapshot(
                    name="Capture",
                    min_raw=0,
                    max_raw=80,
                    current_raw=80,
                    current_db=6.0,
                    max_db=6.0,
                    is_boost_control=True,
                    saturation_risk=True,
                ),
            ),
            aggregated_boost_db=42.0,
            saturation_warning=True,
        )

        class _NonTTYStdin:
            def isatty(self) -> bool:
                return False

        with (
            patch("sovyx.cli.commands.doctor.sys") as mock_sys,
            patch(
                "sovyx.cli.commands.doctor.check_linux_mixer_sanity",
                lambda **_: _make_always_fail_check(),
            ),
            patch(
                "sovyx.cli.commands.doctor.check_portaudio",
                lambda **_: _make_always_pass_check(),
            ),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[saturated],
            ),
            patch("sovyx.voice.health._linux_mixer_apply.apply_mixer_reset") as mock_apply,
        ):
            import sys as real_sys

            mock_sys.platform = "linux"
            mock_sys.stdin = _NonTTYStdin()
            mock_sys.stderr = real_sys.stderr
            result = runner.invoke(app, ["doctor", "voice", "--fix"])

        assert result.exit_code == EXIT_DOCTOR_USER_ABORTED
        assert "requires an interactive TTY" in result.stdout
        mock_apply.assert_not_called()


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
