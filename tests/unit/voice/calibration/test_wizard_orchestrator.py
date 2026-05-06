"""Unit tests for sovyx.voice.calibration._wizard_orchestrator.

Coverage:
* WizardJobState dataclass round-trip + frozen invariants
* WizardStatus.is_terminal partition
* WizardProgressTracker append + read_all + latest
* WizardOrchestrator.run pipeline (mocked external deps):
    - successful path -> DONE state with profile_path populated
    - cancellation between stages -> CANCELLED snapshot
    - DiagPrerequisiteError -> FALLBACK with reason
    - DiagRunError -> FALLBACK with reason
    - ApplyError -> FAILED with summary
    - Unhandled exception in any stage -> FAILED
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.voice.calibration import (
    ApplyError,
    ApplyResult,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProgressEvent,
    WizardJobState,
    WizardOrchestrator,
    WizardProgressTracker,
    WizardStatus,
)
from sovyx.voice.calibration import _wizard_orchestrator as wo
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

# ====================================================================
# Fixtures
# ====================================================================


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
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
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
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
    hyps: tuple[HypothesisVerdict, ...] = ()
    if with_winner:
        hyps = (
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
        tarball_root=Path("/tmp/diag"),
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
        hypotheses=hyps,
    )


def _r10_profile() -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="sovyx doctor voice --fix --yes",
                rationale="r10",
                rule_id="R10_mic_attenuated",
                rule_version=1,
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        provenance=(),
        generated_by_engine_version="0.30.16",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=None,
    )


def _apply_result(profile_path: Path) -> ApplyResult:
    return ApplyResult(
        profile_path=profile_path,
        applied_decisions=(),
        skipped_decisions=(),
        advised_actions=("sovyx doctor voice --fix --yes",),
        dry_run=False,
    )


def _diag_result() -> DiagRunResult:
    return DiagRunResult(
        tarball_path=Path("/tmp/diag.tar.gz"),
        duration_s=600.0,
        exit_code=0,
    )


# ====================================================================
# WizardStatus + WizardJobState invariants
# ====================================================================


class TestWizardStatus:
    def test_terminal_states(self) -> None:
        assert WizardStatus.DONE.is_terminal
        assert WizardStatus.FAILED.is_terminal
        assert WizardStatus.CANCELLED.is_terminal
        assert WizardStatus.FALLBACK.is_terminal
        assert not WizardStatus.PENDING.is_terminal
        assert not WizardStatus.PROBING.is_terminal
        assert not WizardStatus.SLOW_PATH_DIAG.is_terminal


class TestWizardJobState:
    def test_frozen(self) -> None:
        s = WizardJobState(
            job_id="x",
            mind_id="default",
            status=WizardStatus.PENDING,
            progress=0.0,
            current_stage_message="",
            created_at_utc="2026-05-05T18:00:00Z",
            updated_at_utc="2026-05-05T18:00:00Z",
        )
        with pytest.raises(FrozenInstanceError):
            s.status = WizardStatus.DONE  # type: ignore[misc]

    def test_dict_round_trip(self) -> None:
        s = WizardJobState(
            job_id="x",
            mind_id="default",
            status=WizardStatus.SLOW_PATH_APPLY,
            progress=0.92,
            current_stage_message="msg",
            created_at_utc="2026-05-05T18:00:00Z",
            updated_at_utc="2026-05-05T18:01:00Z",
            profile_path="/tmp/x.json",
            triage_winner_hid="H10",
        )
        round_tripped = WizardJobState.from_dict(s.to_dict())
        assert round_tripped == s


# ====================================================================
# WizardProgressTracker
# ====================================================================


class TestWizardProgressTracker:
    def test_empty_returns_no_events(self, tmp_path: Path) -> None:
        tracker = WizardProgressTracker(tmp_path / "missing.jsonl")
        assert tracker.read_all() == []
        assert tracker.latest() is None

    def test_append_then_read(self, tmp_path: Path) -> None:
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        s1 = WizardJobState(
            job_id="x",
            mind_id="default",
            status=WizardStatus.PENDING,
            progress=0.0,
            current_stage_message="m",
            created_at_utc="2026-05-05T18:00:00Z",
            updated_at_utc="2026-05-05T18:00:00Z",
        )
        s2 = WizardJobState(
            job_id="x",
            mind_id="default",
            status=WizardStatus.PROBING,
            progress=0.05,
            current_stage_message="m2",
            created_at_utc="2026-05-05T18:00:00Z",
            updated_at_utc="2026-05-05T18:00:01Z",
        )
        tracker.append(s1)
        tracker.append(s2)
        events = tracker.read_all()
        assert len(events) == 2
        assert events[0] == ProgressEvent(state=s1, line_no=1)
        assert events[1] == ProgressEvent(state=s2, line_no=2)
        assert tracker.latest() == s2

    def test_skips_corrupt_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        path.write_text(
            "not-valid-json\n"
            '{"missing_required": "fields"}\n'
            '{"job_id":"x","mind_id":"d","status":"pending","progress":0.0,'
            '"current_stage_message":"m","created_at_utc":"a","updated_at_utc":"a"}\n',
            encoding="utf-8",
        )
        tracker = WizardProgressTracker(path)
        events = tracker.read_all()
        assert len(events) == 1
        assert events[0].state.job_id == "x"


# ====================================================================
# WizardOrchestrator successful pipeline
# ====================================================================


@pytest.mark.asyncio()
class TestOrchestratorSuccess:
    async def test_full_slow_path_reaches_done(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        profile_path = tmp_path / "default" / "calibration.json"

        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag", return_value=_diag_result()),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=_apply_result(profile_path),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")

        assert result.status == WizardStatus.DONE
        assert result.progress == 1.0
        assert result.profile_path == str(profile_path)
        assert result.triage_winner_hid == "H10"

    async def test_progress_jsonl_records_each_transition(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        profile_path = tmp_path / "default" / "calibration.json"

        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag", return_value=_diag_result()),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=_apply_result(profile_path),
            ),
        ):
            await orch.run(job_id="testjob", mind_id="default")

        tracker = WizardProgressTracker(orch.progress_path("testjob"))
        events = tracker.read_all()
        # Expected stages: PENDING, PROBING, SLOW_PATH_DIAG,
        # SLOW_PATH_CALIBRATE, SLOW_PATH_APPLY, DONE -- 6 events.
        statuses = [e.state.status for e in events]
        assert statuses == [
            WizardStatus.PENDING,
            WizardStatus.PROBING,
            WizardStatus.SLOW_PATH_DIAG,
            WizardStatus.SLOW_PATH_CALIBRATE,
            WizardStatus.SLOW_PATH_APPLY,
            WizardStatus.DONE,
        ]


# ====================================================================
# Cancellation
# ====================================================================


@pytest.mark.asyncio()
class TestOrchestratorCancellation:
    async def test_cancel_before_probing(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        # Pre-create the cancel file BEFORE the orchestrator runs.
        orch.job_dir("testjob").mkdir(parents=True)
        orch.cancel_path("testjob").touch()

        result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.CANCELLED

    async def test_cancel_between_diag_and_calibrate(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)

        # Touch the cancel file via the run_full_diag mock so the next
        # cancellation poll (before SLOW_PATH_CALIBRATE) catches it.
        def diag_then_cancel(**_kwargs: object) -> DiagRunResult:
            orch.cancel_path("testjob").touch()
            return _diag_result()

        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag", side_effect=diag_then_cancel),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")

        assert result.status == WizardStatus.CANCELLED


# ====================================================================
# Failure modes -> FALLBACK + FAILED
# ====================================================================


@pytest.mark.asyncio()
class TestOrchestratorFailures:
    async def test_diag_prerequisite_unmet_emits_fallback(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                side_effect=DiagPrerequisiteError("no bash"),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.FALLBACK
        assert result.fallback_reason == "diag_prerequisite_unmet"
        assert "no bash" in (result.error_summary or "")

    async def test_diag_run_failed_emits_fallback(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                side_effect=DiagRunError("selftest aborted", exit_code=3),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.FALLBACK
        assert result.fallback_reason == "diag_run_failed"

    async def test_apply_error_emits_failed(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        unsupported_set = CalibrationDecision(
            target="mind.voice.x",
            target_class="MindConfig.voice",
            operation="set",
            value="x",
            rationale="r",
            rule_id="R_synth",
            rule_version=1,
            confidence=CalibrationConfidence.HIGH,
        )
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag", return_value=_diag_result()),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                side_effect=ApplyError("not supported", decision=unsupported_set),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.FAILED
        assert "not supported" in (result.error_summary or "")

    async def test_unhandled_exception_emits_failed(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        with patch.object(
            wo,
            "capture_fingerprint",
            side_effect=RuntimeError("synth bug"),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.FAILED
        assert "synth bug" in (result.error_summary or "")
