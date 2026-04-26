"""Tests for MA13 — macOS sandbox detection (Step 6.c).

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.c.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

from sovyx.voice.health._macos_sandbox_detect import (
    SandboxVerdict,
    detect_sandbox_state,
)


class TestDetectSandboxState:
    def test_non_darwin_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = detect_sandbox_state()
        assert report.verdict is SandboxVerdict.UNKNOWN
        assert any("non-darwin" in n for n in report.notes)

    def test_darwin_codesign_absent_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.shutil.which",
                return_value=None,
            ),
        ):
            report = detect_sandbox_state()
        assert report.verdict is SandboxVerdict.UNKNOWN
        assert any("codesign binary not found" in n for n in report.notes)

    def test_sandboxed_binary_yields_sandboxed_verdict(self) -> None:
        sandboxed_output = (
            "Executable=/Applications/Sovyx.app/Contents/MacOS/sovyx\n"
            'designated => identifier "com.sovyx.app" and '
            "anchor apple generic and certificate leaf[subject.CN] = "
            '"Apple Distribution: Sovyx LLC" and '
            'entitlement ["com.apple.security.app-sandbox"] = true'
        )
        mock_result = MagicMock(returncode=0, stdout="", stderr=sandboxed_output)
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.shutil.which",
                return_value="/usr/bin/codesign",
            ),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = detect_sandbox_state(
                executable="/Applications/Sovyx.app/Contents/MacOS/sovyx"
            )
        assert report.verdict is SandboxVerdict.SANDBOXED
        # Hint includes the sandbox constraint explanation.
        assert "App Sandbox" in report.remediation_hint

    def test_unsandboxed_binary_yields_unsandboxed_verdict(self) -> None:
        unsandboxed_output = (
            "Executable=/usr/local/bin/python3\n"
            'designated => identifier "org.python.python3" and '
            "anchor apple generic"
        )
        mock_result = MagicMock(returncode=0, stdout="", stderr=unsandboxed_output)
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.shutil.which",
                return_value="/usr/bin/codesign",
            ),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = detect_sandbox_state(executable="/usr/local/bin/python3")
        assert report.verdict is SandboxVerdict.UNSANDBOXED
        assert report.remediation_hint == ""

    def test_codesign_returncode_1_means_unsigned_returns_unknown(self) -> None:
        """Unsigned binary — codesign exits 1. We can't tell either
        way (sandbox status is meaningful only for signed binaries)
        so we return UNKNOWN with a specific note."""
        mock_result = MagicMock(
            returncode=1,
            stdout="",
            stderr="code object is not signed at all",
        )
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.shutil.which",
                return_value="/usr/bin/codesign",
            ),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.subprocess.run",
                return_value=mock_result,
            ),
        ):
            report = detect_sandbox_state(executable="/tmp/unsigned-binary")  # noqa: S108
        assert report.verdict is SandboxVerdict.UNKNOWN
        assert any("codesign exited 1" in n for n in report.notes)

    def test_codesign_timeout_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.shutil.which",
                return_value="/usr/bin/codesign",
            ),
            patch(
                "sovyx.voice.health._macos_sandbox_detect.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="codesign", timeout=3.0),
            ),
        ):
            report = detect_sandbox_state()
        assert report.verdict is SandboxVerdict.UNKNOWN
        assert any("timed out" in n for n in report.notes)


class TestRemediationHints:
    def test_unsandboxed_has_no_hint(self) -> None:
        from sovyx.voice.health._macos_sandbox_detect import SandboxReport

        assert SandboxReport(verdict=SandboxVerdict.UNSANDBOXED).remediation_hint == ""

    def test_sandboxed_explains_constraints(self) -> None:
        from sovyx.voice.health._macos_sandbox_detect import SandboxReport

        hint = SandboxReport(verdict=SandboxVerdict.SANDBOXED).remediation_hint
        assert "App Sandbox" in hint
        assert "subprocess" in hint  # subprocess restriction explanation

    def test_unknown_has_diagnostic_pointer(self) -> None:
        from sovyx.voice.health._macos_sandbox_detect import SandboxReport

        hint = SandboxReport(verdict=SandboxVerdict.UNKNOWN).remediation_hint
        assert "codesign --display --requirements" in hint
