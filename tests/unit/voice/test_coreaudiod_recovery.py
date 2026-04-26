"""Tests for MA10 — coreaudiod recovery diagnostic (Step 6.b).

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.b.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

from sovyx.voice.health._coreaudiod_recovery import (
    CoreAudiodVerdict,
    probe_coreaudiod_state,
)


class TestProbeCoreaudiodState:
    def test_non_darwin_returns_unknown_with_note(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.UNKNOWN
        assert any("non-darwin" in n for n in report.notes)

    def test_darwin_pgrep_missing_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value=None,
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.UNKNOWN
        assert any("pgrep binary not found" in n for n in report.notes)

    def test_pgrep_returncode_0_means_running(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="123\n", stderr="")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value="/usr/bin/pgrep",
            ),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.RUNNING
        assert report.remediation_hint == ""

    def test_pgrep_returncode_1_means_missing(self) -> None:
        mock_result = MagicMock(returncode=1, stdout="", stderr="")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value="/usr/bin/pgrep",
            ),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.MISSING
        # MISSING verdict carries the canonical sudo killall hint.
        assert "sudo killall coreaudiod" in report.remediation_hint

    def test_pgrep_returncode_other_returns_unknown(self) -> None:
        mock_result = MagicMock(returncode=2, stdout="", stderr="syntax error")
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value="/usr/bin/pgrep",
            ),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.UNKNOWN
        assert any("pgrep exited 2" in n for n in report.notes)

    def test_pgrep_timeout_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value="/usr/bin/pgrep",
            ),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=3.0),
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.UNKNOWN
        assert any("timed out" in n for n in report.notes)

    def test_pgrep_oserror_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.shutil.which",
                return_value="/usr/bin/pgrep",
            ),
            patch(
                "sovyx.voice.health._coreaudiod_recovery.subprocess.run",
                side_effect=OSError("permission denied"),
            ),
        ):
            report = probe_coreaudiod_state()
        assert report.verdict is CoreAudiodVerdict.UNKNOWN
        assert any("spawn failed" in n for n in report.notes)


class TestRemediationHints:
    def test_running_verdict_has_no_hint(self) -> None:
        from sovyx.voice.health._coreaudiod_recovery import CoreAudiodReport

        assert CoreAudiodReport(verdict=CoreAudiodVerdict.RUNNING).remediation_hint == ""

    def test_missing_verdict_includes_killall(self) -> None:
        from sovyx.voice.health._coreaudiod_recovery import CoreAudiodReport

        hint = CoreAudiodReport(verdict=CoreAudiodVerdict.MISSING).remediation_hint
        assert "sudo killall coreaudiod" in hint
        assert "DiagnosticReports" in hint  # forensic pointer

    def test_unknown_verdict_has_diagnostic_hint(self) -> None:
        from sovyx.voice.health._coreaudiod_recovery import CoreAudiodReport

        hint = CoreAudiodReport(verdict=CoreAudiodVerdict.UNKNOWN).remediation_hint
        assert "pgrep -x coreaudiod" in hint
