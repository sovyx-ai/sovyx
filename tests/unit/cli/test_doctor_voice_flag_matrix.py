"""Flag-matrix coverage for ``sovyx doctor voice`` mutex pairs.

v0.38.0 / W3.C1 + F2-M05 (audit §3.H) closure. Pre-fix the
``sovyx doctor voice`` flag-interaction logic was tested only by a
handful of single-flag invocations in ``test_doctor.py`` /
``test_doctor_calibrate.py``. The mutex pairs (`--fix` × `--full-diag`,
`--show` × `--rollback`, etc.) had no dedicated coverage, so a
regression in the ``raise typer.BadParameter(...)`` cluster would
ship green and only surface when an operator typed a real conflicting
combination. This file pins:

* The 5 mutually-exclusive pairs the audit listed.
* A handful of valid combinations to guard against false-positive
  rejections.
* Standalone unit tests for F-703 (``sovyx voice
  generate-signing-key``), F-705 (``sovyx doctor voice_capture_apo``),
  F-706/F-707 (``_surface_preflight_warnings``) per audit §1.E gap
  summary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Mutex flag pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "needle"),
    [
        # Direct mutex pairs hard-wired into the typer.BadParameter branch.
        ("--fix", "--full-diag", "mutually exclusive"),
        ("--fix", "--calibrate", "mutually exclusive"),
        ("--full-diag", "--calibrate", "mutually exclusive"),
        # Read-only inspect mode mutex set: any 2 of these MUST conflict.
        ("--show", "--rollback", "mutually exclusive"),
        ("--show", "--evaluate-rules", "mutually exclusive"),
        ("--show", "--inspect-migration", "mutually exclusive"),
        ("--rollback", "--evaluate-rules", "mutually exclusive"),
        ("--rollback", "--inspect-migration", "mutually exclusive"),
        ("--evaluate-rules", "--inspect-migration", "mutually exclusive"),
    ],
)
def test_doctor_voice_rejects_mutex_flag_pair(
    first: str,
    second: str,
    needle: str,
) -> None:
    """Each mutex pair MUST exit non-zero with a ``mutually exclusive`` error."""
    args = ["doctor", "voice", first, second]
    # All read-only inspect modes require --calibrate alongside; add it
    # so the mutex check fires AFTER the read-only requirement check.
    if first.lstrip("-") in {
        "show",
        "rollback",
        "evaluate-rules",
        "inspect-migration",
    } and second.lstrip("-") in {
        "show",
        "rollback",
        "evaluate-rules",
        "inspect-migration",
    }:
        args.append("--calibrate")
    result = runner.invoke(app, args)
    assert result.exit_code != 0, f"expected non-zero exit for {args!r}, got 0"
    combined = result.output + (result.stderr or "")
    assert needle.lower() in combined.lower(), (
        f"expected {needle!r} in error output for {args!r}; got: {combined!r}"
    )


@pytest.mark.parametrize(
    "flag",
    ["--show", "--rollback", "--inspect-migration"],
)
def test_doctor_voice_inspect_modes_require_calibrate(flag: str) -> None:
    """Read-only inspect flags without --calibrate exit non-zero."""
    result = runner.invoke(app, ["doctor", "voice", flag])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "--calibrate" in combined, f"error must reference --calibrate; got: {combined!r}"


# ---------------------------------------------------------------------------
# Valid (non-conflicting) combinations — guard against false rejections
# ---------------------------------------------------------------------------


def test_doctor_voice_help_succeeds() -> None:
    """``sovyx doctor voice --help`` is the canonical happy path."""
    result = runner.invoke(app, ["doctor", "voice", "--help"])
    assert result.exit_code == 0
    assert "voice" in result.output.lower()


def test_doctor_voice_calibrate_with_show_is_valid() -> None:
    """--calibrate + --show is the documented "read last profile" pattern.

    --show is one of the read-only inspect modes; it requires
    --calibrate but is NOT mutex with it. We don't actually want to
    execute calibration here — patch the dispatcher so the command
    early-exits before touching the real bash diag.
    """
    with patch(
        "sovyx.cli.commands.doctor._run_voice_doctor",
        return_value=MagicMock(),
    ):
        result = runner.invoke(
            app,
            ["doctor", "voice", "--calibrate", "--show"],
        )
    # Either the patched dispatcher returns cleanly (exit 0) or it
    # falls through to a downstream early-exit; the load-bearing
    # assertion is "not flagged as mutex".
    combined = result.output + (result.stderr or "")
    assert "mutually exclusive" not in combined.lower()


def test_doctor_voice_calibrate_with_evaluate_rules_is_valid() -> None:
    """--calibrate + --evaluate-rules is the dry-run preview pattern."""
    with patch(
        "sovyx.cli.commands.doctor._run_voice_doctor",
        return_value=MagicMock(),
    ):
        result = runner.invoke(
            app,
            ["doctor", "voice", "--calibrate", "--evaluate-rules"],
        )
    combined = result.output + (result.stderr or "")
    assert "mutually exclusive" not in combined.lower()


def test_doctor_voice_fix_with_dry_run_is_valid() -> None:
    """--fix + --dry-run is documented (audit + plan-but-not-mutate)."""
    with patch(
        "sovyx.cli.commands.doctor._run_voice_doctor",
        return_value=MagicMock(),
    ):
        result = runner.invoke(
            app,
            ["doctor", "voice", "--fix", "--dry-run", "--yes"],
        )
    combined = result.output + (result.stderr or "")
    assert "mutually exclusive" not in combined.lower()


# ---------------------------------------------------------------------------
# F-703 — sovyx voice generate-signing-key
# ---------------------------------------------------------------------------


class TestVoiceGenerateSigningKey:
    """Standalone unit tests for the signing-key generator subcommand."""

    def test_help_renders_and_exits_zero(self) -> None:
        result = runner.invoke(app, ["voice", "generate-signing-key", "--help"])
        assert result.exit_code == 0
        assert "Generate" in result.output or "signing" in result.output.lower()

    def test_default_invocation_exits_zero_when_key_missing(self, tmp_path: Path) -> None:
        """Happy path — no existing key, generator persists a fresh one."""
        from sovyx.voice.calibration import _key_generation as kg

        out_priv = tmp_path / "calibration.signing-key.priv"
        with patch.object(kg, "generate_signing_key") as gen:
            gen.return_value = kg.GeneratedSigningKey(
                private_key_path=out_priv,
                public_key_path=out_priv.with_suffix(".pub"),
                public_key_pem="-----BEGIN PUBLIC KEY-----\nstub\n-----END PUBLIC KEY-----\n",
                fingerprint_short="ab12cd34",
            )
            result = runner.invoke(
                app,
                [
                    "voice",
                    "generate-signing-key",
                    "--output",
                    str(out_priv),
                    "--mind-id",
                    "default",
                ],
            )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# F-705 — sovyx doctor voice_capture_apo
# ---------------------------------------------------------------------------


class TestDoctorVoiceCaptureApo:
    """Standalone unit tests for the voice_capture_apo subcommand."""

    def test_help_renders_and_exits_zero(self) -> None:
        result = runner.invoke(app, ["doctor", "voice_capture_apo", "--help"])
        assert result.exit_code == 0
        assert "apo" in result.output.lower()

    def test_pass_status_exits_zero(self) -> None:
        """``DiagnosticStatus.PASS`` → exit 0 per docstring contract."""
        from sovyx.upgrade.doctor import DiagnosticResult, DiagnosticStatus

        fake_result = DiagnosticResult(
            check="voice_capture_apo",
            status=DiagnosticStatus.PASS,
            message="No Voice Clarity APO active.",
            fix_suggestion=None,
            details=None,
        )
        with patch(
            "sovyx.upgrade.doctor._check_voice_capture_apo",
            return_value=fake_result,
        ):
            result = runner.invoke(app, ["doctor", "voice_capture_apo"])
        assert result.exit_code == 0

    def test_warn_status_exits_one(self) -> None:
        """``DiagnosticStatus.WARN`` → exit 1 per docstring contract."""
        from sovyx.upgrade.doctor import DiagnosticResult, DiagnosticStatus

        fake_result = DiagnosticResult(
            check="voice_capture_apo",
            status=DiagnosticStatus.WARN,
            message="Voice Clarity APO active without bypass.",
            fix_suggestion="set SOVYX_VOICE_CAPTURE_WASAPI_EXCLUSIVE=1",
            details={"endpoints": []},
        )
        with patch(
            "sovyx.upgrade.doctor._check_voice_capture_apo",
            return_value=fake_result,
        ):
            result = runner.invoke(app, ["doctor", "voice_capture_apo"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# F-706 / F-707 — _surface_preflight_warnings
# ---------------------------------------------------------------------------


class TestSurfacePreflightWarnings:
    """Standalone tests for the boot-preflight warning surface."""

    def test_no_warnings_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty warnings list → no console output."""
        from sovyx.cli import main as cli_main

        with (
            patch.object(
                cli_main,
                "_surface_preflight_warnings",
                wraps=cli_main._surface_preflight_warnings,
            ),
            patch(
                "sovyx.voice.health.read_preflight_warnings_file",
                return_value=[],
            ),
        ):
            cli_main._surface_preflight_warnings()
        captured = capsys.readouterr()
        # Empty warnings → no per-warning lines printed (Console may
        # buffer to stderr-like sinks; both streams must stay quiet).
        assert "Voice preflight warning" not in captured.out
        assert "Voice preflight warning" not in captured.err

    def test_warnings_render_one_line_per_entry(
        self,
    ) -> None:
        """Each warning becomes one yellow line + the remediation hint."""
        from sovyx.cli import main as cli_main

        with (
            patch(
                "sovyx.voice.health.read_preflight_warnings_file",
                return_value=[
                    {
                        "code": "linux_mixer_saturated",
                        "hint": "Run sovyx doctor voice --fix --yes.",
                    },
                ],
            ),
            patch.object(cli_main.console, "print") as mock_print,
        ):
            cli_main._surface_preflight_warnings()

        # Three calls per warning (header + hint + remediation footer).
        assert mock_print.call_count >= 3
        joined = " ".join(str(call) for call in mock_print.call_args_list)
        assert "linux_mixer_saturated" in joined

    def test_io_hiccup_is_swallowed_silently(self) -> None:
        """Reading the warnings file MUST NOT raise — preflight is best-effort."""
        from sovyx.cli import main as cli_main

        with patch(
            "sovyx.voice.health.read_preflight_warnings_file",
            side_effect=OSError("disk gone"),
        ):
            # Must not raise.
            cli_main._surface_preflight_warnings()
