"""Unit tests for ``sovyx doctor voice --full-diag``.

The flag wires the in-process diag runner + triage analyzer behind
the existing ``sovyx doctor voice`` command. Tests mock both the
runner (to avoid the 8-12 min interactive bash invocation) and the
triage (to avoid building real diag tarballs) so the CLI orchestration
logic is exercised in milliseconds. End-to-end runs land in operator
Linux environments via the actual ``--full-diag`` invocation.

Coverage:
* mutex enforcement (--full-diag + --fix -> BadParameter)
* TTY gate (non-TTY stdin without --non-interactive -> USER_ABORTED)
* prerequisite failure (Linux-only / no-bash) -> UNSUPPORTED
* diag-script failure (runner raises DiagRunError) -> GENERIC_FAILURE
* success path: runner -> triage -> rendered verdict
* hypothesis-winner branch: surfaces recommended_action
* no-winner branch: surfaces "voice subsystem appears healthy" message
* extra_args plumbing: --non-interactive flows through to bash
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.voice.diagnostics import (
    AlertsSummary,
    DiagPrerequisiteError,
    DiagRunError,
    DiagRunResult,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)

runner = CliRunner()


def _make_diag_result(tmp_path: Path) -> DiagRunResult:
    """Synthetic DiagRunResult pointing at a real tarball path (need not exist
    for the purposes of mocked-triage tests; the path is just plumbed)."""
    return DiagRunResult(
        tarball_path=tmp_path / "sovyx-diag-host" / "sovyx-voice-diag_x.tar.gz",
        duration_s=42.0,
        exit_code=0,
    )


def _make_triage_result(
    *,
    hypotheses: tuple[HypothesisVerdict, ...] = (),
    tarball_root: Path | None = None,
) -> TriageResult:
    return TriageResult(
        schema_version=1,
        toolkit="linux",
        tarball_root=tarball_root or Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3.0",
        host="test-host",
        captured_at_utc="2026-05-05T16:00:00Z",
        os_descriptor="linux",
        status="complete",
        exit_code="0",
        selftest_status="pass",
        steps={},
        skip_captures=False,
        schema_validation=SchemaValidation(
            ok=True, missing_required=(), missing_recommended=(), warnings=()
        ),
        alerts=AlertsSummary(error_count=0, warn_count=0, info_count=0, error_messages=()),
        hypotheses=hypotheses,
    )


def _make_h10_winner() -> HypothesisVerdict:
    return HypothesisVerdict(
        hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
        title="Linux ALSA mixer attenuated -- capture+boost below Silero VAD floor",
        confidence=0.95,
        evidence_for=("alert: linux_mixer_saturated detected (attenuated regime)",),
        evidence_against=(),
        recommended_action="Run `sovyx doctor voice --fix --yes` to lift the attenuated mixer controls.",
    )


# ====================================================================
# Mutex enforcement
# ====================================================================


class TestMutexFullDiagFix:
    """--full-diag and --fix cannot coexist."""

    def test_full_diag_with_fix_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--full-diag", "--fix"])
        assert result.exit_code != 0
        # Typer / click renders BadParameter messages to stderr (mixed_stderr=True
        # combines them into output); accept either source.
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "mutually exclusive" in combined


# ====================================================================
# TTY gate
# ====================================================================


class TestTTYGate:
    """Non-TTY stdin requires --non-interactive."""

    def test_non_tty_without_non_interactive_returns_user_aborted(self) -> None:
        # CliRunner.invoke runs without a TTY by default (stdin is StringIO),
        # so isatty() returns False. Without --non-interactive, the command
        # should refuse and return EXIT_DOCTOR_USER_ABORTED (4).
        result = runner.invoke(app, ["doctor", "voice", "--full-diag"])
        assert result.exit_code == 4
        assert "interactive TTY" in result.output


# ====================================================================
# Prerequisite failure
# ====================================================================


class TestPrerequisiteFailure:
    """DiagPrerequisiteError -> EXIT_DOCTOR_UNSUPPORTED (5)."""

    def test_non_linux_returns_unsupported(self) -> None:
        with patch(
            "sovyx.cli.commands.doctor.run_full_diag",
            side_effect=DiagPrerequisiteError(
                "voice diag toolkit is Linux-only; current platform is 'darwin'."
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 5
        assert "Linux-only" in result.output
        # Operator hint to the cross-platform fallback.
        assert "sovyx doctor voice" in result.output

    def test_missing_bash_returns_unsupported(self) -> None:
        with patch(
            "sovyx.cli.commands.doctor.run_full_diag",
            side_effect=DiagPrerequisiteError("bash is not installed or not in PATH."),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 5
        assert "bash is not installed" in result.output


# ====================================================================
# Diag-script failure
# ====================================================================


class TestDiagFailure:
    """DiagRunError -> EXIT_DOCTOR_GENERIC_FAILURE (1)."""

    def test_diag_run_error_returns_generic_failure(self) -> None:
        with patch(
            "sovyx.cli.commands.doctor.run_full_diag",
            side_effect=DiagRunError("selftest aborted", exit_code=3),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 1
        assert "Voice diag failed" in result.output
        assert "selftest aborted" in result.output

    def test_partial_output_dir_surfaced(self, tmp_path: Path) -> None:
        partial = tmp_path / "sovyx-diag-partial"
        partial.mkdir()
        with patch(
            "sovyx.cli.commands.doctor.run_full_diag",
            side_effect=DiagRunError("selftest aborted", exit_code=3, partial_output_dir=partial),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 1
        # Path may wrap across lines in narrow terminals; assert on the
        # operator-visible label + the directory's leaf name (unique to
        # this fixture and stable regardless of width).
        assert "Partial output preserved" in result.output
        assert "sovyx-diag-partial" in result.output


# ====================================================================
# Success path
# ====================================================================


class TestSuccessPath:
    """Successful runner+triage renders verdict and exits 0."""

    def test_success_with_winner_renders_recommended_action(self, tmp_path: Path) -> None:
        diag = _make_diag_result(tmp_path)
        triage = _make_triage_result(
            hypotheses=(_make_h10_winner(),),
            tarball_root=tmp_path,
        )
        with (
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=triage,
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 0
        assert "Diag completed" in result.output
        assert "42." in result.output  # duration_s rendering
        # Hypothesis winner block.
        assert "Highest-confidence hypothesis" in result.output
        assert "H10" in result.output
        # Recommended action is surfaced for the operator.
        assert "sovyx doctor voice --fix --yes" in result.output

    def test_success_no_winner_renders_healthy_message(self, tmp_path: Path) -> None:
        diag = _make_diag_result(tmp_path)
        triage = _make_triage_result(hypotheses=(), tarball_root=tmp_path)
        with (
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=triage,
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 0
        assert "No high-confidence hypothesis" in result.output


# ====================================================================
# extra_args plumbing
# ====================================================================


class TestExtraArgsPlumbing:
    """--non-interactive flows through to the bash diag as extra_args."""

    def test_non_interactive_passes_flag_to_runner(self, tmp_path: Path) -> None:
        diag = _make_diag_result(tmp_path)
        triage = _make_triage_result(tarball_root=tmp_path)

        captured_kwargs: dict[str, object] = {}

        def capture_run(**kwargs: object) -> DiagRunResult:
            captured_kwargs.update(kwargs)
            return diag

        with (
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                side_effect=capture_run,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=triage,
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--full-diag", "--non-interactive"],
            )
        assert result.exit_code == 0
        assert captured_kwargs.get("extra_args") == ("--non-interactive",)
