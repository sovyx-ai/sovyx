"""DOCTOR-14 regression — `sovyx doctor linux_session_manager_grab` header.

Mission anchor:
``docs-internal/missions/MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02-FINDINGS.md``
§DOCTOR-14.

Pre-fix every ``has_grab=None`` verdict rendered the fixed
parenthetical "(pactl missing and/or /proc scan timed out)" — on
Windows/macOS the detector short-circuits before ever attempting
pactl or the /proc scan, so the header misattributed the inconclusive
verdict; the true reason (Linux-only detector) lived only in the
evidence line. Post-fix the non-Linux header says "not applicable",
as the command docstring always promised.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from sovyx.cli.commands.doctor import _run_linux_session_manager_grab
from sovyx.voice import _session_manager_detector as detector_mod
from sovyx.voice._session_manager_detector import SessionManagerGrabReport


def _run_with_report(report: SessionManagerGrabReport) -> int:
    async def _fake_detect(*, tuning: object) -> SessionManagerGrabReport:
        return report

    with patch.object(detector_mod, "detect_session_manager_grab", _fake_detect):
        return asyncio.run(_run_linux_session_manager_grab(output_json=False))


class TestInconclusiveHeader:
    @pytest.mark.skipif(sys.platform == "linux", reason="non-Linux header under test")
    def test_non_linux_header_says_not_applicable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = SessionManagerGrabReport(
            has_grab=None,
            detection_method="unavailable",
            evidence=f"detector is Linux-only; running on {sys.platform}",
        )
        exit_code = _run_with_report(report)
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "Not applicable on this OS" in captured.out
        # The misattributing parenthetical must be gone on non-Linux.
        assert "pactl missing" not in captured.out
        # Honest evidence line still present.
        assert "Linux-only" in captured.out

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux header under test")
    def test_linux_inconclusive_header_keeps_tool_attribution(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = SessionManagerGrabReport(
            has_grab=None,
            detection_method="unavailable",
            evidence="pactl absent; /proc scan timed out",
        )
        exit_code = _run_with_report(report)
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "pactl missing" in captured.out
        assert "Not applicable" not in captured.out


class TestConclusiveVerdictsUnchanged:
    def test_no_grab_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SessionManagerGrabReport(has_grab=False, detection_method="pactl")
        exit_code = _run_with_report(report)
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Capture hardware is free" in captured.out

    def test_grab_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SessionManagerGrabReport(has_grab=True, detection_method="pactl")
        exit_code = _run_with_report(report)
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "held by another client" in captured.out
