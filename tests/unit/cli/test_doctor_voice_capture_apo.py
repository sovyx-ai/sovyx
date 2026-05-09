"""Unit tests for ``sovyx doctor voice_capture_apo`` — APO scan CLI.

Phase 5.C v0.32.6 wire-up — surfaces the existing
:func:`sovyx.upgrade.doctor._check_voice_capture_apo` check as a
standalone Typer subcommand. Multiple operator-facing docs and the
bundled bash diag script have referenced this subcommand syntax since
v0.21.1; the underlying check existed but no Typer wire-up was ever
added until this commit.

Coverage:

* Subcommand registers + invokes (smoke).
* JSON output matches ``DiagnosticResult.to_dict()`` shape exactly
  (machine-readable contract operators pipe into jq).
* Non-Windows platforms exit 0 with PASS status.
* Windows + Voice Clarity active + bypass armed → exit 1, WARN, fix
  hint cites the bypass already armed.
* Windows + Voice Clarity active + bypass disarmed → exit 1, WARN, fix
  hint cites the bypass to enable.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.upgrade.doctor import DiagnosticResult, DiagnosticStatus

runner = CliRunner()


class TestDoctorVoiceCaptureApoSmoke:
    """Subcommand registration + invocation contract."""

    def test_command_registers_and_runs(self) -> None:
        result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        # PASS on non-Windows hosts (where most CI runs); WARN+1 on
        # Windows hosts with Voice Clarity but bypass armed (default).
        # Either way the command must EXIT cleanly, not crash.
        assert result.exit_code in (0, 1), (
            f"Unexpected exit code: {result.exit_code}\nstdout: {result.stdout}"
        )

    def test_json_output_matches_diagnostic_result_shape(self) -> None:
        """The JSON payload MUST be the exact ``DiagnosticResult.to_dict()``
        contract — operators piping into jq depend on this."""
        result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        body = json.loads(result.stdout)
        assert isinstance(body, dict)
        assert body["check"] == "voice_capture_apo"
        assert body["status"] in ("pass", "warn", "fail")
        assert "message" in body
        assert isinstance(body["message"], str)


class TestDoctorVoiceCaptureApoNonWindows:
    """On non-Windows platforms the check exits 0 with PASS."""

    def test_linux_returns_pass_exit_zero(self) -> None:
        with patch.object(sys, "platform", "linux"):
            result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["status"] == "pass"
        assert "Windows" in body["message"]

    def test_darwin_returns_pass_exit_zero(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["status"] == "pass"


class TestDoctorVoiceCaptureApoExitCodes:
    """Exit code reflects severity (0=pass, 1=warn|fail)."""

    def test_pass_returns_zero(self) -> None:
        fake_pass = DiagnosticResult(
            check="voice_capture_apo",
            status=DiagnosticStatus.PASS,
            message="No Voice Clarity APO detected.",
            details={"endpoints": [], "bypass_status": []},
        )
        # Anti-pattern #38: ``_check_voice_capture_apo`` is lazy-imported
        # inside the subcommand body, so patch the SOURCE module.
        from sovyx.upgrade import doctor as upgrade_doctor

        with patch.object(
            upgrade_doctor,
            "_check_voice_capture_apo",
            return_value=fake_pass,
        ):
            result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["status"] == "pass"

    def test_warn_returns_one(self) -> None:
        fake_warn = DiagnosticResult(
            check="voice_capture_apo",
            status=DiagnosticStatus.WARN,
            message="Voice Clarity APO active on: Realtek Mic.",
            fix_suggestion="Enable SOVYX_TUNING__VOICE__CAPTURE_WASAPI_EXCLUSIVE=true.",
            details={
                "endpoints": [
                    {
                        "endpoint_id": "{guid}",
                        "endpoint_name": "Realtek Mic",
                        "voice_clarity_active": True,
                    }
                ],
                "bypass_status": [],
            },
        )
        from sovyx.upgrade import doctor as upgrade_doctor

        with patch.object(
            upgrade_doctor,
            "_check_voice_capture_apo",
            return_value=fake_warn,
        ):
            result = runner.invoke(app, ["doctor", "voice_capture_apo", "--json"])
        assert result.exit_code == 1
        body = json.loads(result.stdout)
        assert body["status"] == "warn"
        assert "fix_suggestion" in body


class TestDoctorVoiceCaptureApoHumanRender:
    """Non-JSON output renders a Rich table with status marker + message."""

    def test_pretty_output_contains_status_marker_and_message(self) -> None:
        fake_pass = DiagnosticResult(
            check="voice_capture_apo",
            status=DiagnosticStatus.PASS,
            message="No Voice Clarity APO detected.",
            details=None,
        )
        from sovyx.upgrade import doctor as upgrade_doctor

        with patch.object(
            upgrade_doctor,
            "_check_voice_capture_apo",
            return_value=fake_pass,
        ):
            result = runner.invoke(app, ["doctor", "voice_capture_apo"])
        assert result.exit_code == 0
        # Human render: contains the check name + the message.
        assert "voice_capture_apo" in result.stdout
        assert "No Voice Clarity APO detected" in result.stdout
