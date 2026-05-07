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

import re
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

# Rich colour + wrap normalisation lives in tests/conftest.py
# (NO_COLOR=1 + COLUMNS=240 set at session start). CliRunner inherits
# it; output is plain ASCII, no ANSI escapes, no 80-col wrap.
# ``_strip_ansi`` is kept as a NO-OP shim for the few callers that
# already accept defensive normalisation, so we don't churn 8+ test
# bodies for zero behaviour change.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Defensive ANSI strip; conftest sets NO_COLOR=1 so this is now
    a no-op on any well-behaved CliRunner output. Kept so existing
    callers don't need a churn-edit + so a future Rich variant that
    ignores NO_COLOR won't silently break the assertions."""
    return _ANSI_RE.sub("", text)


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
# rc.6 (Agent 2 A.5): --signing-key fail-fast on missing path. Operator
# typo (e.g. ``--signing-key /tmp/nope``) MUST be caught at flag-parse
# time so the operator doesn't waste 8-12 min of diag runtime then
# silently land an unsigned profile.
# ====================================================================


class TestSigningKeyFailFast:
    """``--signing-key /tmp/nonexistent`` rejects at flag-parse, not deep
    in `_persistence.py:319` after the 8-12 min diag runs."""

    def test_signing_key_missing_path_rejected_before_diag(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no_such_key.pem"
        result = runner.invoke(
            app,
            [
                "doctor",
                "voice",
                "--calibrate",
                "--non-interactive",
                "--signing-key",
                str(nonexistent),
            ],
        )
        assert result.exit_code != 0
        combined = _strip_ansi(result.output) + (
            _strip_ansi(result.stderr) if result.stderr_bytes else ""
        )
        # Strip whitespace + box-drawing wrapping that Rich introduces in
        # error panels — the path may span multiple lines.
        combined_compact = "".join(combined.split())
        # Operator-readable error: cite the bad path + suggest the fix.
        assert "--signing-keypathdoesnotexist" in combined_compact
        # The path filename ("no_such_key.pem") must appear (the full path
        # may wrap across lines but the leaf filename always lands on one).
        assert "no_such_key.pem" in combined_compact
        assert "generate_calibration_signing_key.py" in combined_compact

    def test_signing_key_malformed_pem_rejected_at_flag_parse(self, tmp_path: Path) -> None:
        """rc.7 (Agent 2 NEW.1): garbage-bytes file passes ``is_file()``
        but fails PEM parsing. Pre-rc.7 the operator wasted 8-12 min
        of diag runtime then landed unsigned. Now the deeper validation
        fires at flag-parse with a Click BadParameter.
        """
        garbage = tmp_path / "garbage_key.pem"
        garbage.write_bytes(b"not a valid PEM key, just random bytes\n")
        result = runner.invoke(
            app,
            [
                "doctor",
                "voice",
                "--calibrate",
                "--non-interactive",
                "--signing-key",
                str(garbage),
            ],
        )
        assert result.exit_code != 0
        combined = _strip_ansi(result.output) + (
            _strip_ansi(result.stderr) if result.stderr_bytes else ""
        )
        compact = "".join(combined.split())
        assert "--signing-keyvalidationfailed" in compact
        assert "garbage_key.pem" in compact
        assert "generate_calibration_signing_key.py" in compact

    def test_signing_key_rsa_not_ed25519_rejected_at_flag_parse(self, tmp_path: Path) -> None:
        """rc.7 (Agent 2 NEW.1): RSA private key (or any non-Ed25519
        algorithm) is rejected at flag-parse with a clear message.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rsa_pem = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        wrong_algo = tmp_path / "rsa_key.pem"
        wrong_algo.write_bytes(rsa_pem)

        result = runner.invoke(
            app,
            [
                "doctor",
                "voice",
                "--calibrate",
                "--non-interactive",
                "--signing-key",
                str(wrong_algo),
            ],
        )
        assert result.exit_code != 0
        combined = _strip_ansi(result.output) + (
            _strip_ansi(result.stderr) if result.stderr_bytes else ""
        )
        compact = "".join(combined.split())
        assert "--signing-keyvalidationfailed" in compact
        assert "Ed25519" in combined  # cite the expected algorithm

    def test_signing_key_valid_ed25519_passes_validation(self, tmp_path: Path) -> None:
        """rc.7 (Agent 2 NEW.1) — A valid Ed25519 PEM passes the deeper
        validation; the downstream pipeline (TTY gate / non-interactive
        check / prerequisite check) takes over.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        ed_key = ed25519.Ed25519PrivateKey.generate()
        ed_pem = ed_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        existing = tmp_path / "real_ed25519_key.pem"
        existing.write_bytes(ed_pem)

        result = runner.invoke(
            app,
            [
                "doctor",
                "voice",
                "--calibrate",
                "--signing-key",
                str(existing),
            ],
        )
        # Validation PASSED — downstream TTY gate or prereq check fires.
        combined = _strip_ansi(result.output) + (
            _strip_ansi(result.stderr) if result.stderr_bytes else ""
        )
        compact = "".join(combined.split())
        assert "--signing-keypathdoesnotexist" not in compact
        assert "--signing-keyvalidationfailed" not in compact


# ====================================================================
# rc.7 (Agent 2 NEW.2/NEW.3): _render_calibration_verdict surfaces
# signed/unsigned status from `apply_result.signed`, NOT from
# `profile.signature` (which is always None on frozen profiles).
# Pre-rc.7 the renderer read profile.signature and showed
# "Profile is unsigned" on EVERY clean --calibrate run, even when
# --signing-key worked. This regression class pins the rc.7 contract.
# ====================================================================


class TestRenderCalibrationVerdictSignedStatus:
    """``_render_calibration_verdict`` surfaces signing status from
    ``apply_result.signed`` — the disk-side truth — instead of
    ``profile.signature`` which is always None on the in-memory profile.

    rc.10 (Agent 2 fix #2) refined the semantics: the unsigned banner
    fires ONLY when ``signed_intent=True AND signed=False`` (operator
    passed --signing-key but signing failed). The default path
    (signed_intent=False, signed=False) renders SILENTLY so
    non-technical operators don't see scary "STRICT rejects; pass
    --signing-key" hints that punt them at a dev-only flag.
    """

    @staticmethod
    def _capture_render(
        signed_status: bool | None,
        *,
        signed_intent: bool | None = None,
        dry_run: bool = False,
    ) -> str:
        """Invoke ``_render_calibration_verdict`` with a synthetic
        ApplyResult carrying the given signed status + intent; capture
        rendered text via Rich's Console.capture context manager.
        """
        from rich.console import Console

        from sovyx.cli.commands import doctor as doctor_module

        # Build minimal apply_result + profile.
        apply_result = ApplyResult(
            profile_path=Path("/tmp/calibration.json"),
            applied_decisions=(),
            skipped_decisions=(_r10_advise_decision(),),
            advised_actions=(),
            dry_run=dry_run,
            signed=signed_status,
            signed_intent=signed_intent,
        )
        profile = _r10_profile()

        # Replace the doctor module's console with a capturable one.
        captured_console = Console(record=True, force_terminal=False)
        original_console = doctor_module.console
        doctor_module.console = captured_console
        try:
            doctor_module._render_calibration_verdict(profile, apply_result, explain=False)
        finally:
            doctor_module.console = original_console
        return _strip_ansi(captured_console.export_text())

    def test_signed_true_renders_green_checkmark_banner(self) -> None:
        """``apply_result.signed = True`` → green ✓ + STRICT mode hint."""
        rendered = self._capture_render(signed_status=True, signed_intent=True)
        assert "✓" in rendered
        assert "signed" in rendered
        assert "Ed25519" in rendered
        assert "STRICT" in rendered

    def test_signed_false_with_intent_renders_failure_warning(self) -> None:
        """rc.10: ``signed=False AND signed_intent=True`` → operator
        passed --signing-key but signing failed mid-write. The yellow
        warning fires + cites the structured log event for forensics.
        """
        rendered = self._capture_render(signed_status=False, signed_intent=True)
        assert "could not be signed" in rendered
        assert "[!]" in rendered
        assert "signing_failed" in rendered
        # Critical: must NOT show the "✓ signed" banner.
        assert "✓" not in rendered

    def test_signed_false_default_path_renders_silently(self) -> None:
        """rc.10 (Agent 2 fix #2) — DEFAULT PATH: ``signed=False AND
        signed_intent=False`` (operator ran --calibrate without
        --signing-key, the canonical non-technical user flow). The
        renderer must NOT show the "Profile is unsigned" banner that
        punts users at the dev-only --signing-key flag. This is the
        single most important UX fix in rc.10.
        """
        rendered = self._capture_render(signed_status=False, signed_intent=False)
        # Critical: NEITHER the unsigned hint NOR the signing-failure
        # warning should fire on the default path.
        assert "Profile is unsigned" not in rendered
        assert "--signing-key on next" not in rendered
        assert "could not be signed" not in rendered
        assert "STRICT mode rejects" not in rendered
        # The green ✓ banner also must not fire (we didn't sign).
        assert "Profile is signed" not in rendered

    def test_signed_none_dry_run_renders_no_signing_banner(self) -> None:
        """``apply_result.signed = None`` (dry-run path) → renderer
        renders neither banner. Persistence didn't happen so there's
        nothing to verify.
        """
        rendered = self._capture_render(signed_status=None, dry_run=True)
        assert "✓ Profile is" not in rendered
        assert "Profile is unsigned" not in rendered
        assert "could not be signed" not in rendered


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
        clean = _strip_ansi(result.output)
        assert "require --calibrate" in clean or "--show" in clean

    def test_rollback_without_calibrate_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--rollback"])
        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "require --calibrate" in clean or "--rollback" in clean

    def test_show_with_rollback_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--calibrate", "--show", "--rollback"])
        assert result.exit_code != 0
        assert "mutually exclusive" in _strip_ansi(result.output)


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
        """The --surgical flag is documented in --help output.

        rc.10 (Agent 2 fix #4): the help text was rewritten in
        operator-action language — the dev-internal layer codes
        (`A,C,D,E,J`) were removed because non-technical operators
        don't know what they mean. The help now describes the
        operator-facing trade-off ("fast mode ~30s vs 8-12 min full").
        """
        result = runner.invoke(app, ["doctor", "voice", "--help"])
        assert result.exit_code == 0
        clean = _strip_ansi(result.output)
        assert "--surgical" in clean
        # Help describes the operator-facing trade-off, not dev-internal
        # layer codes. Match against the rewritten help text fragments
        # that survive Rich's table-column wrapping.
        assert "fast mode" in clean.lower() or "30s" in clean

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


# ════════════════════════════════════════════════════════════════════
# rc.4 (Agent 3 #11) — `--evaluate-rules` behavior coverage. Pre-rc.4
# only the help-text registration was tested; the actual dry-eval flow
# (capture_fingerprint + capture_measurements + engine.evaluate +
# render-only with no apply / no diag / no triage) had ZERO behavior
# coverage. A regression that wired a real apply() instead of dry-eval,
# or that broke capture_measurements(triage_result=None), or that
# accidentally triggered run_full_diag would land green pre-rc.4.
# ════════════════════════════════════════════════════════════════════


class TestEvaluateRulesBehavior:
    """``sovyx doctor voice --calibrate --evaluate-rules`` runs render-only.

    Mission §0 promise 11 (``--evaluate-rules`` CLI mode): "render rule
    trace without running diag/applying". Tests assert the contract:

    * Calls ``capture_fingerprint`` exactly once.
    * Calls ``capture_measurements`` exactly once with
      ``triage_result=None`` and ``diag_tarball_root=None``.
    * Calls ``CalibrationEngine.evaluate`` exactly once with the
      captured fingerprint + measurements + ``triage_result=None``.
    * Does NOT invoke ``run_full_diag`` or ``triage_tarball``.
    * Does NOT invoke ``CalibrationApplier.apply`` (real or dry).
    * Returns exit code 0 on success.
    * Renders ``[dry-eval, no apply]`` marker so the operator sees the
      mode.
    """

    def test_evaluate_rules_runs_dry_eval_without_diag_or_apply(self) -> None:
        # ``_run_voice_calibrate_evaluate_rules`` does a LAZY import:
        # ``from sovyx.voice.calibration import capture_fingerprint,
        # CalibrationEngine`` and
        # ``from sovyx.voice.calibration._measurer import capture_measurements``.
        # The lazy import resolves attributes on the SOURCE modules at
        # call-time, so patching the doctor-module attribute would not
        # affect the lazy binding. Patch the source modules directly.
        # rc.6 (Agent 2 A.3): the function is now Linux-gated; patch
        # sys.platform on Windows test hosts so the Linux-only path runs.
        from sovyx.cli.commands import doctor as doctor_module

        with (
            patch.object(doctor_module.sys, "platform", "linux"),
            patch(
                "sovyx.voice.calibration.capture_fingerprint",
                return_value=_fingerprint(),
            ) as fingerprint_mock,
            patch(
                "sovyx.voice.calibration._measurer.capture_measurements",
                return_value=_measurements(),
            ) as measurements_mock,
            patch(
                "sovyx.voice.calibration.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ) as evaluate_mock,
            # Belt-and-suspenders: assert these are NEVER called.
            patch("sovyx.cli.commands.doctor.run_full_diag") as run_diag_mock,
            patch("sovyx.cli.commands.doctor.triage_tarball") as triage_mock,
            patch("sovyx.cli.commands.doctor.CalibrationApplier.apply") as apply_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--non-interactive",
                    "--evaluate-rules",
                ],
            )

        assert result.exit_code == 0, (
            f"unexpected exit {result.exit_code} from --evaluate-rules; output: {result.output!r}"
        )
        # Must have rendered the dry-eval banner so the operator knows
        # nothing was applied.
        clean = _strip_ansi(result.output)
        assert "dry-eval" in clean.lower(), (
            f"--evaluate-rules must render the dry-eval marker; output: {clean!r}"
        )

        # The 3 expected calls.
        assert fingerprint_mock.call_count == 1
        assert measurements_mock.call_count == 1
        # capture_measurements MUST receive triage_result=None +
        # diag_tarball_root=None (the dry-eval contract).
        meas_kwargs = measurements_mock.call_args.kwargs
        assert meas_kwargs.get("triage_result") is None
        assert meas_kwargs.get("diag_tarball_root") is None
        assert evaluate_mock.call_count == 1, (
            "engine.evaluate must be invoked exactly once on --evaluate-rules"
        )
        eng_kwargs = evaluate_mock.call_args.kwargs
        assert eng_kwargs.get("triage_result") is None, (
            f"engine.evaluate must be called with triage_result=None on "
            f"--evaluate-rules; got {eng_kwargs!r}"
        )

        # The 3 forbidden calls (diag + triage + apply).
        assert run_diag_mock.call_count == 0, "--evaluate-rules must NOT invoke run_full_diag"
        assert triage_mock.call_count == 0, "--evaluate-rules must NOT invoke triage_tarball"
        assert apply_mock.call_count == 0, (
            "--evaluate-rules must NOT invoke CalibrationApplier.apply (even dry-run)"
        )

    def test_evaluate_rules_with_explain_flag_invokes_explain_renderer(self) -> None:
        """``--explain`` should propagate into the verdict renderer."""
        from sovyx.cli.commands import doctor as doctor_module

        with (
            patch.object(doctor_module.sys, "platform", "linux"),
            patch(
                "sovyx.voice.calibration.capture_fingerprint",
                return_value=_fingerprint(),
            ),
            patch(
                "sovyx.voice.calibration._measurer.capture_measurements",
                return_value=_measurements(),
            ),
            patch(
                "sovyx.voice.calibration.CalibrationEngine.evaluate",
                return_value=_r10_profile(),
            ),
            patch("sovyx.cli.commands.doctor.run_full_diag"),
            patch("sovyx.cli.commands.doctor.triage_tarball"),
            patch("sovyx.cli.commands.doctor.CalibrationApplier.apply"),
        ):
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--non-interactive",
                    "--evaluate-rules",
                    "--explain",
                ],
            )

        assert result.exit_code == 0
        clean = _strip_ansi(result.output)
        # rc.5 (Agent 3 #1): pre-rc.5 this asserted ``"R10" in clean`` —
        # but ``rule_id`` always renders in the decisions table at
        # ``doctor.py:979``, regardless of ``--explain``. A regression
        # that wired ``explain=True`` to a no-op would have landed
        # green. The genuine load-bearing strings are the explain-only
        # block at ``doctor.py:994-1003``: the "Rule trace" header
        # (string at line 995) AND the per-condition ``"matched:"``
        # prefix (string at line 1004). Both fire only when
        # ``explain=True and profile.provenance``.
        assert "Rule trace" in clean, (
            f"--explain must render the explain-only block header 'Rule trace'; output: {clean!r}"
        )
        assert "matched:" in clean, (
            f"--explain must render per-condition 'matched:' lines from "
            f"profile.provenance; output: {clean!r}"
        )

    def test_evaluate_rules_non_linux_returns_unsupported(self) -> None:
        """rc.6 (Agent 2 A.3): non-Linux hosts get a friendly message
        with EXIT_DOCTOR_UNSUPPORTED (5), NOT a Python exception from
        the Linux-only fingerprint/amixer probes.
        """
        from sovyx.cli.commands import doctor as doctor_module

        with patch.object(doctor_module.sys, "platform", "win32"):
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--non-interactive",
                    "--evaluate-rules",
                ],
            )

        # EXIT_DOCTOR_UNSUPPORTED = 5 per cli/commands/doctor.py:104.
        assert result.exit_code == 5, (
            f"non-Linux must yield EXIT_DOCTOR_UNSUPPORTED=5; "
            f"got {result.exit_code} with output {result.output!r}"
        )
        clean = _strip_ansi(result.output)
        assert "Linux-only" in clean
        # Operator-actionable hint: point at the cross-platform alternative.
        assert "sovyx doctor voice" in clean

    def test_evaluate_rules_fingerprint_failure_returns_generic_failure(self) -> None:
        """Fingerprint capture failure surfaces EXIT_DOCTOR_GENERIC_FAILURE
        (5) without crashing or invoking downstream stages.
        """
        from sovyx.cli.commands import doctor as doctor_module

        with (
            patch.object(doctor_module.sys, "platform", "linux"),
            patch(
                "sovyx.voice.calibration.capture_fingerprint",
                side_effect=RuntimeError("synthetic dmidecode failure"),
            ),
            patch("sovyx.voice.calibration._measurer.capture_measurements") as measurements_mock,
            patch("sovyx.voice.calibration.CalibrationEngine.evaluate") as evaluate_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--non-interactive",
                    "--evaluate-rules",
                ],
            )

        # EXIT_DOCTOR_GENERIC_FAILURE = 1 per cli/commands/doctor.py:92.
        assert result.exit_code == 1, (
            f"fingerprint failure must yield EXIT_DOCTOR_GENERIC_FAILURE=1; "
            f"got {result.exit_code} with output {result.output!r}"
        )
        clean = _strip_ansi(result.output)
        assert "Fingerprint capture failed" in clean
        # Downstream stages MUST be skipped on early failure.
        assert measurements_mock.call_count == 0
        assert evaluate_mock.call_count == 0
