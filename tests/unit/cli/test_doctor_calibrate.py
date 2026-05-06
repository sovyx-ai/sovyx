"""Unit tests for ``sovyx doctor voice --calibrate``.

The flag wires the calibration engine end-to-end: capture fingerprint
+ run full diag + triage + capture measurements + evaluate engine +
apply (or dry-run) + render verdict. Tests mock every external
dependency (subprocess for fingerprint, run_full_diag, triage_tarball,
capture_measurements, applier) so the orchestration logic is exercised
in milliseconds. End-to-end runs land in operator Linux environments.

Coverage:
* Mutex enforcement (--calibrate + --fix; --calibrate + --full-diag)
* TTY gate (non-TTY without --non-interactive -> USER_ABORTED)
* Prereq failure -> UNSUPPORTED
* Diag failure -> GENERIC_FAILURE
* Triage failure -> GENERIC_FAILURE
* Apply failure -> GENERIC_FAILURE
* Successful pipeline -> renders decisions table + advised actions
* Dry-run mode + explain mode flow through to renderer
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.voice.calibration import (
    ApplyError,
    ApplyResult,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProvenanceTrace,
)
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


# ====================================================================
# Fixtures
# ====================================================================


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8.0-50-generic",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="ALSA",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=600.0,
        rms_dbfs_per_capture=(-90.0,),
        vad_speech_probability_max=0.001,
        vad_speech_probability_p99=0.001,
        noise_floor_dbfs_estimate=-95.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=5,
        mixer_boost_pct=0,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="attenuated",
        echo_correlation_db=None,
        triage_winner_hid="H10",
        triage_winner_confidence=0.95,
    )


def _triage(*, with_winner: bool = True) -> TriageResult:
    hypotheses: tuple[HypothesisVerdict, ...] = ()
    if with_winner:
        hypotheses = (
            HypothesisVerdict(
                hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
                title="x",
                confidence=0.95,
                evidence_for=(),
                evidence_against=(),
                recommended_action="sovyx doctor voice --fix --yes",
            ),
        )
    return TriageResult(
        schema_version=1,
        toolkit="linux",
        tarball_root=Path("/tmp/x"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-05T18:00:00Z",
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


def _r10_advise_decision() -> CalibrationDecision:
    return CalibrationDecision(
        target="advice.action",
        target_class="TuningAdvice",
        operation="advise",
        value="sovyx doctor voice --fix --yes",
        rationale="r10",
        rule_id="R10_mic_attenuated",
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


def _r10_provenance() -> ProvenanceTrace:
    return ProvenanceTrace(
        rule_id="R10_mic_attenuated",
        rule_version=1,
        fired_at_utc="2026-05-05T18:02:00Z",
        matched_conditions=("audio_stack == 'pipewire'", "regime == 'attenuated'"),
        produced_decisions=("advise: advice.action = 'sovyx doctor voice --fix' (high)",),
        confidence=CalibrationConfidence.HIGH,
    )


def _r10_profile() -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(_r10_advise_decision(),),
        provenance=(_r10_provenance(),),
        generated_by_engine_version="0.30.15",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=None,
    )


def _apply_result_advise(*, dry_run: bool = False) -> ApplyResult:
    return ApplyResult(
        profile_path=Path("/tmp/calibration.json"),
        applied_decisions=(),
        skipped_decisions=(_r10_advise_decision(),),
        advised_actions=("sovyx doctor voice --fix --yes",),
        dry_run=dry_run,
    )


# ====================================================================
# Mutex enforcement
# ====================================================================


class TestMutex:
    """--calibrate is mutex with --fix and --full-diag."""

    def test_calibrate_with_fix_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--fix"])
        assert result.exit_code != 0
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "mutually exclusive" in combined

    def test_calibrate_with_full_diag_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--full-diag"])
        assert result.exit_code != 0
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "mutually exclusive" in combined


# ====================================================================
# TTY gate
# ====================================================================


class TestTTYGate:
    def test_non_tty_without_non_interactive_returns_user_aborted(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--calibrate"])
        assert result.exit_code == 4  # EXIT_DOCTOR_USER_ABORTED
        assert "interactive TTY" in result.output


# ====================================================================
# Prerequisite + diag failure paths
# ====================================================================


class TestPrereqFailures:
    def test_diag_prerequisite_failure_returns_unsupported(self) -> None:
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                side_effect=DiagPrerequisiteError("Linux-only"),
            ),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--non-interactive"])
        assert result.exit_code == 5  # EXIT_DOCTOR_UNSUPPORTED
        assert "Linux-only" in result.output

    def test_diag_run_failure_returns_generic_failure(self) -> None:
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                side_effect=DiagRunError("selftest failed", exit_code=3),
            ),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--non-interactive"])
        assert result.exit_code == 1
        assert "selftest failed" in result.output

    def test_apply_error_returns_generic_failure(self) -> None:
        diag_result = DiagRunResult(
            tarball_path=Path("/tmp/diag.tar.gz"),
            duration_s=600.0,
            exit_code=0,
        )
        unsupported_set = CalibrationDecision(
            target="mind.voice.voice_input_device_name",
            target_class="MindConfig.voice",
            operation="set",
            value="Internal Mic",
            rationale="synth",
            rule_id="R_synth",
            rule_version=1,
            confidence=CalibrationConfidence.HIGH,
        )
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag_result,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=_triage(),
            ),
            patch(
                "sovyx.cli.commands.doctor.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationApplier.apply",
                side_effect=ApplyError("set not supported", decision=unsupported_set),
            ),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--non-interactive"])
        assert result.exit_code == 1
        assert "Calibration apply failed" in result.output


# ====================================================================
# Successful pipeline
# ====================================================================


class TestSuccessPath:
    def test_calibrate_renders_decisions_and_advised_actions(self) -> None:
        diag_result = DiagRunResult(
            tarball_path=Path("/tmp/diag.tar.gz"),
            duration_s=600.0,
            exit_code=0,
        )
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag_result,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=_triage(),
            ),
            patch(
                "sovyx.cli.commands.doctor.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationApplier.apply",
                return_value=_apply_result_advise(),
            ),
        ):
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--non-interactive"])
        assert result.exit_code == 0
        # The decisions table renders R10's row.
        assert "R10_mic_attenuated" in result.output
        # The advise action surfaces for operator copy-paste.
        assert "sovyx doctor voice --fix --yes" in result.output

    def test_calibrate_dry_run_skips_persistence_label(self) -> None:
        diag_result = DiagRunResult(
            tarball_path=Path("/tmp/diag.tar.gz"),
            duration_s=600.0,
            exit_code=0,
        )
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag_result,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=_triage(),
            ),
            patch(
                "sovyx.cli.commands.doctor.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationApplier.apply",
                return_value=_apply_result_advise(dry_run=True),
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--non-interactive", "--dry-run"],
            )
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        assert "would be persisted" in result.output

    def test_calibrate_explain_renders_rule_trace(self) -> None:
        diag_result = DiagRunResult(
            tarball_path=Path("/tmp/diag.tar.gz"),
            duration_s=600.0,
            exit_code=0,
        )
        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                return_value=diag_result,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=_triage(),
            ),
            patch(
                "sovyx.cli.commands.doctor.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationApplier.apply",
                return_value=_apply_result_advise(),
            ),
        ):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--non-interactive", "--explain"],
            )
        assert result.exit_code == 0
        # The rule trace block surfaces.
        assert "Rule trace" in result.output
        assert "matched:" in result.output
        assert "produced:" in result.output


# ====================================================================
# v0.30.19: --show + --rollback flags
# ====================================================================


class TestShowAndRollbackMutex:
    """--show and --rollback both require --calibrate; cannot combine."""

    def test_show_without_calibrate_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--show"])
        assert result.exit_code != 0
        assert "require --calibrate" in result.output or "--show" in result.output

    def test_rollback_without_calibrate_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--rollback"])
        assert result.exit_code != 0
        assert "require --calibrate" in result.output or "--rollback" in result.output

    def test_show_with_rollback_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--show", "--rollback"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestShow:
    """``--calibrate --show`` renders the LAST persisted profile, no diag run."""

    def test_show_renders_existing_profile(self, tmp_path: Path) -> None:
        from sovyx.voice.calibration import save_calibration_profile

        # CLI resolves data_dir as Path.home() / ".sovyx", so we patch
        # Path.home to a parent of an empty fake-home dir + save to
        # the matching .sovyx subdir so the load path lines up.
        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx"
        sovyx_data.mkdir(parents=True)

        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            save_calibration_profile(_r10_profile(), data_dir=sovyx_data)
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--show"])
        assert result.exit_code == 0
        assert "Calibration decisions" in result.output
        # The advised action surfaces (operator copy-paste affordance).
        assert "sovyx doctor voice --fix --yes" in result.output

    def test_show_when_no_profile_returns_failure(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--show"])
        assert result.exit_code != 0
        assert "Cannot load calibration profile" in result.output


class TestRollback:
    """``--calibrate --rollback`` swaps .bak -> canonical."""

    def test_rollback_swaps_bak_to_canonical(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from sovyx.voice.calibration import (
            profile_backup_path,
            save_calibration_profile,
        )

        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx"
        sovyx_data.mkdir(parents=True)

        # Two saves: first becomes the eventual .bak; second the
        # current. After rollback, the first lands back as canonical.
        first = _r10_profile()
        second = replace(first, profile_id="22222222-2222-3333-4444-555555555555")

        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            save_calibration_profile(first, data_dir=sovyx_data)
            save_calibration_profile(second, data_dir=sovyx_data)
            assert profile_backup_path(data_dir=sovyx_data, mind_id="default").is_file()

            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--rollback"])
        assert result.exit_code == 0
        assert "Restored prior profile" in result.output
        # After rollback, the .bak slot is consumed.
        assert not profile_backup_path(data_dir=sovyx_data, mind_id="default").is_file()

    def test_rollback_when_no_backup_returns_failure(self, tmp_path: Path) -> None:
        from sovyx.voice.calibration import save_calibration_profile

        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx"
        sovyx_data.mkdir(parents=True)

        # Only one save -> no .bak exists.
        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            save_calibration_profile(_r10_profile(), data_dir=sovyx_data)
            result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--rollback"])
        assert result.exit_code != 0
        assert "Rollback failed" in result.output


# ====================================================================
# v0.30.26: --surgical wires the bash --only A,C,D,E,J flag
# ====================================================================


class TestSurgicalFlag:
    """``--calibrate --surgical`` passes --only + skip flags to run_full_diag."""

    def test_surgical_extra_args_helper(self) -> None:
        """The CLI's _surgical_extra_args() builds the right flag list."""
        from sovyx.cli.commands.doctor import _surgical_extra_args

        # surgical=False + non_interactive=False -> empty
        assert _surgical_extra_args(non_interactive=False, surgical=False) == ()
        # surgical=False + non_interactive=True -> just --non-interactive
        assert _surgical_extra_args(non_interactive=True, surgical=False) == ("--non-interactive",)
        # surgical=True + non_interactive=False -> --only + skips
        result = _surgical_extra_args(non_interactive=False, surgical=True)
        assert "--only" in result
        assert "A,C,D,E,J" in result
        assert "--skip-captures" in result
        assert "--skip-guardian" in result
        assert "--skip-operator-prompts" in result
        assert "--non-interactive" not in result
        # both true -> non-interactive + --only + skips
        result_both = _surgical_extra_args(non_interactive=True, surgical=True)
        assert "--non-interactive" in result_both
        assert "--only" in result_both

    def test_surgical_flag_visible_in_help(self) -> None:
        """The --surgical flag is documented in --help output."""
        result = runner.invoke(app, ["doctor", "voice", "--help"])
        assert result.exit_code == 0
        assert "--surgical" in result.output
        assert "A,C,D,E,J" in result.output

    def test_surgical_flag_threads_through_run_full_diag(self) -> None:
        """--calibrate --surgical passes --only to run_full_diag."""
        from sovyx.voice.diagnostics import DiagRunResult

        diag_result = DiagRunResult(
            tarball_path=Path("/tmp/diag.tar.gz"),
            duration_s=30.0,
            exit_code=0,
        )

        captured: dict[str, object] = {}

        def capture_run_full_diag(**kwargs: object) -> DiagRunResult:
            captured["kwargs"] = kwargs
            return diag_result

        with (
            patch(
                "sovyx.cli.commands.doctor.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.cli.commands.doctor.run_full_diag",
                side_effect=capture_run_full_diag,
            ),
            patch(
                "sovyx.cli.commands.doctor.triage_tarball",
                return_value=_triage(),
            ),
            patch(
                "sovyx.cli.commands.doctor.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ),
            patch(
                "sovyx.cli.commands.doctor.CalibrationApplier.apply",
                return_value=_apply_result_advise(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--non-interactive",
                    "--surgical",
                ],
            )
        assert result.exit_code == 0
        extra_args = captured["kwargs"]["extra_args"]
        assert "--only" in extra_args
        assert "A,C,D,E,J" in extra_args
        # CLI explicitly passes trigger="cli"
        assert captured["kwargs"]["trigger"] == "cli"
