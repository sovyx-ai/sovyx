"""T12.1 — unit tests for the Linux session-manager grab detector."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._session_manager_detector import (
    ProcessInfo,
    SessionManagerGrabReport,
    _parse_pactl_source_outputs,
    detect_session_manager_grab,
)

_TUNING = VoiceTuningConfig()


class TestParsePactlOutputs:
    def test_single_app_yields_process_info(self) -> None:
        stdout = """
Source Output #123
  Driver: protocol-native.c
  Owner Module: 10
  Properties:
    application.name = "Zoom Meeting"
    application.process.id = "4567"
""".strip()
        result = _parse_pactl_source_outputs(stdout)
        assert result == [ProcessInfo(pid=4567, name="Zoom Meeting")]

    def test_multiple_apps_dedup_by_pid(self) -> None:
        stdout = """
Source Output #1
  Properties:
    application.name = "First"
    application.process.id = "111"

Source Output #2
  Properties:
    application.name = "Second"
    application.process.id = "111"

Source Output #3
  Properties:
    application.name = "Third"
    application.process.id = "222"
""".strip()
        result = _parse_pactl_source_outputs(stdout)
        assert len(result) == 2
        assert {p.pid for p in result} == {111, 222}

    def test_missing_pid_skips_section(self) -> None:
        stdout = """
Source Output #1
  Properties:
    application.name = "No PID"
""".strip()
        result = _parse_pactl_source_outputs(stdout)
        assert result == []

    def test_missing_name_falls_back_to_empty_string(self) -> None:
        stdout = """
Source Output #1
  Properties:
    application.process.id = "999"
""".strip()
        result = _parse_pactl_source_outputs(stdout)
        assert result == [ProcessInfo(pid=999, name="")]

    def test_application_process_binary_is_also_accepted(self) -> None:
        stdout = """
Source Output #1
  Properties:
    application.process.binary = "pipewire"
    application.process.id = "321"
""".strip()
        result = _parse_pactl_source_outputs(stdout)
        assert result == [ProcessInfo(pid=321, name="pipewire")]

    def test_empty_stdout_returns_empty_list(self) -> None:
        assert _parse_pactl_source_outputs("") == []


class TestDetectSessionManagerGrabNonLinux:
    @pytest.mark.asyncio()
    async def test_windows_returns_unavailable(self) -> None:
        with patch("sovyx.voice._session_manager_detector.sys.platform", "win32"):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is None
        assert report.detection_method == "unavailable"
        assert "Linux-only" in report.evidence

    @pytest.mark.asyncio()
    async def test_darwin_returns_unavailable(self) -> None:
        with patch("sovyx.voice._session_manager_detector.sys.platform", "darwin"):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is None


class TestDetectSessionManagerGrabLinuxPactl:
    @pytest.mark.asyncio()
    async def test_pactl_with_grab(self) -> None:
        stdout = """
Source Output #1
  Properties:
    application.name = "Firefox"
    application.process.id = "4321"
""".strip()
        fake_result = MagicMock(returncode=0, stdout=stdout, stderr="")
        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector.subprocess.run",
                return_value=fake_result,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is True
        assert report.detection_method == "pactl"
        assert report.grabbing_processes[0].pid == 4321
        assert report.grabbing_processes[0].name == "Firefox"

    @pytest.mark.asyncio()
    async def test_pactl_with_no_output_returns_false(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector.subprocess.run",
                return_value=fake_result,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is False
        assert report.detection_method == "pactl"

    @pytest.mark.asyncio()
    async def test_pactl_missing_falls_back_to_proc(self) -> None:
        def raise_fnf(*_args: object, **_kw: object) -> None:
            raise FileNotFoundError("pactl not in PATH")

        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector.subprocess.run",
                side_effect=raise_fnf,
            ),
            patch(
                "sovyx.voice._session_manager_detector.Path.is_dir",
                return_value=False,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        # pactl missing + /proc not a dir → unavailable.
        assert report.has_grab is None
        assert report.detection_method == "unavailable"

    @pytest.mark.asyncio()
    async def test_pactl_timeout_falls_back(self) -> None:
        def raise_timeout(*_args: object, **_kw: object) -> None:
            raise subprocess.TimeoutExpired(cmd="pactl", timeout=2.0)

        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector.subprocess.run",
                side_effect=raise_timeout,
            ),
            patch(
                "sovyx.voice._session_manager_detector.Path.is_dir",
                return_value=False,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is None

    @pytest.mark.asyncio()
    async def test_pactl_non_zero_exit_falls_back(self) -> None:
        fake_result = MagicMock(returncode=1, stdout="", stderr="error")
        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector.subprocess.run",
                return_value=fake_result,
            ),
            patch(
                "sovyx.voice._session_manager_detector.Path.is_dir",
                return_value=False,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        # Non-zero exit → pactl path returned None → /proc path also
        # unavailable → overall unavailable.
        assert report.has_grab is None


class TestDetectSessionManagerGrabSwallowsExceptions:
    @pytest.mark.asyncio()
    async def test_unexpected_exception_returns_unavailable(self) -> None:
        def boom(*_args: object, **_kw: object) -> None:
            raise RuntimeError("totally unexpected")

        with (
            patch("sovyx.voice._session_manager_detector.sys.platform", "linux"),
            patch(
                "sovyx.voice._session_manager_detector._detect_via_pactl",
                side_effect=boom,
            ),
        ):
            report = await detect_session_manager_grab(tuning=_TUNING)
        assert report.has_grab is None
        assert "detector raised" in report.evidence


class TestSessionManagerGrabReportDefaults:
    def test_default_field_values(self) -> None:
        report = SessionManagerGrabReport(has_grab=None)
        assert report.grabbing_processes == ()
        assert report.detection_method == "unavailable"
        assert report.evidence == ""
