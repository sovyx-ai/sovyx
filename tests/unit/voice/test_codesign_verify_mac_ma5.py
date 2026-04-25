"""Tests for the MA5 macOS code-signing entitlement verifier."""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice._codesign_verify_mac import (
    EntitlementReport,
    EntitlementVerdict,
    current_executable_path,
    verify_microphone_entitlement,
)

_MIC_ENT_KEY = "com.apple.security.device.audio-input"


def _fake_run(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raise_exc: type[BaseException] | None = None,
) -> Any:
    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        if raise_exc is not None:
            raise raise_exc("simulated failure")
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    return _factory


# ── Cross-platform branches ───────────────────────────────────────


class TestNonDarwin:
    def test_linux_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = verify_microphone_entitlement()
        assert report.verdict is EntitlementVerdict.UNKNOWN
        assert any("non-darwin" in n for n in report.notes)

    def test_windows_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "win32"):
            report = verify_microphone_entitlement()
        assert report.verdict is EntitlementVerdict.UNKNOWN


# ── Probe failures ────────────────────────────────────────────────


class TestProbeFailures:
    def test_codesign_missing(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value=None),
        ):
            report = verify_microphone_entitlement(executable="/usr/bin/python3")
        assert report.verdict is EntitlementVerdict.UNKNOWN
        assert any("not found" in n for n in report.notes)

    def test_codesign_timeout(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cs", 3)),
        ):
            report = verify_microphone_entitlement(executable="/usr/bin/python3")
        assert report.verdict is EntitlementVerdict.UNKNOWN
        assert any("timed out" in n for n in report.notes)

    def test_codesign_oserror(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch("subprocess.run", side_effect=OSError("boom")),
        ):
            report = verify_microphone_entitlement(executable="/usr/bin/python3")
        assert report.verdict is EntitlementVerdict.UNKNOWN
        assert any("spawn failed" in n for n in report.notes)


# ── Verdict logic (mocked codesign) ───────────────────────────────


class TestVerdictLogic:
    def test_unsigned_binary_returns_unsigned(self) -> None:
        # codesign returns rc=1 with stderr "not signed at all".
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    returncode=1,
                    stderr="/usr/local/bin/python3: code object is not signed at all",
                ),
            ),
        ):
            report = verify_microphone_entitlement(executable="/usr/local/bin/python3")
        assert report.verdict is EntitlementVerdict.UNSIGNED
        assert any("typical for python" in n for n in report.notes)

    def test_signed_with_mic_entitlement_returns_present(self) -> None:
        signed_with_mic = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>{_MIC_ENT_KEY}</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
</dict>
</plist>
"""
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout=signed_with_mic, returncode=0),
            ),
        ):
            report = verify_microphone_entitlement(executable="/Sovyx.app/Contents/MacOS/sovyx")
        assert report.verdict is EntitlementVerdict.PRESENT
        assert _MIC_ENT_KEY in report.raw_codesign_stdout

    def test_signed_without_mic_entitlement_returns_absent(self) -> None:
        signed_without = """\
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
</dict>
</plist>
"""
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout=signed_without, returncode=0),
            ),
        ):
            report = verify_microphone_entitlement(executable="/Sovyx.app/Contents/MacOS/sovyx")
        assert report.verdict is EntitlementVerdict.ABSENT
        assert "missing entitlement" in report.notes[0]

    def test_signed_nonzero_unrecognised_error_returns_unknown(self) -> None:
        # codesign returns non-zero with some other stderr (not "not signed").
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(returncode=2, stderr="some weird error"),
            ),
        ):
            report = verify_microphone_entitlement(executable="/usr/bin/python3")
        assert report.verdict is EntitlementVerdict.UNKNOWN
        assert any("exited 2" in n for n in report.notes)

    def test_raw_stdout_truncated_to_4kb(self) -> None:
        # 5 KB stdout → truncated at 4096.
        big = "x" * 5000
        with (
            patch.object(sys, "platform", "darwin"),
            patch("shutil.which", return_value="/usr/bin/codesign"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout=big, returncode=0),
            ),
        ):
            report = verify_microphone_entitlement(executable="/usr/bin/python3")
        assert len(report.raw_codesign_stdout) == 4096  # noqa: PLR2004


# ── Helper functions ──────────────────────────────────────────────


class TestCurrentExecutablePath:
    def test_returns_sys_executable(self) -> None:
        # current_executable_path simply returns sys.executable —
        # we just verify it's a non-empty string that matches.
        assert current_executable_path() == sys.executable
        assert current_executable_path()  # non-empty


# ── Remediation hints (operator-actionable) ──────────────────────


class TestRemediationHints:
    def test_present_hint_empty(self) -> None:
        report = EntitlementReport(verdict=EntitlementVerdict.PRESENT)
        assert report.remediation_hint == ""

    def test_absent_hint_explains_rebuild(self) -> None:
        report = EntitlementReport(verdict=EntitlementVerdict.ABSENT)
        hint = report.remediation_hint
        assert "rebuilding Sovyx" in hint
        assert _MIC_ENT_KEY in hint
        assert "notarising" in hint

    def test_unsigned_hint_explains_typical_python(self) -> None:
        report = EntitlementReport(verdict=EntitlementVerdict.UNSIGNED)
        hint = report.remediation_hint
        assert "Homebrew" in hint or "pyenv" in hint
        assert "Not a Sovyx defect" in hint

    def test_unknown_hint_offers_fallback_check(self) -> None:
        report = EntitlementReport(verdict=EntitlementVerdict.UNKNOWN)
        hint = report.remediation_hint
        assert "could not be determined" in hint


# ── Report contract ──────────────────────────────────────────────


class TestReportContract:
    def test_verdict_enum_values_stable(self) -> None:
        assert EntitlementVerdict.PRESENT.value == "present"
        assert EntitlementVerdict.ABSENT.value == "absent"
        assert EntitlementVerdict.UNSIGNED.value == "unsigned"
        assert EntitlementVerdict.UNKNOWN.value == "unknown"


pytestmark = pytest.mark.timeout(10)
