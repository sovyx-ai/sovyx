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
from typing import Any
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
            patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
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
            patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
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
            patch.object(wo, "run_full_diag_async", side_effect=diag_then_cancel),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")

        assert result.status == WizardStatus.CANCELLED


# ====================================================================
# Failure modes -> FALLBACK + FAILED
# ====================================================================


@pytest.mark.asyncio()
class TestOrchestratorFastPath:
    """KB cache hit takes the FAST_PATH branch + skips diag entirely."""

    async def test_cache_hit_takes_fast_path(self, tmp_path: Path) -> None:
        # Pre-seed the cache with a profile for the canonical fingerprint.
        from sovyx.voice.calibration._kb_cache import store_profile

        cached = _r10_profile()
        store_profile(cached, data_dir=tmp_path)

        orch = WizardOrchestrator(data_dir=tmp_path)
        applied_path = tmp_path / "default" / "calibration.json"

        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            # run_full_diag MUST NOT be called on the fast path.
            patch.object(wo, "run_full_diag_async") as run_full_diag_mock,
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=ApplyResult(
                    profile_path=applied_path,
                    applied_decisions=(),
                    skipped_decisions=(),
                    advised_actions=(),
                    dry_run=False,
                ),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")

        assert result.status == WizardStatus.DONE
        # Diag was skipped (cache hit shortcut).
        run_full_diag_mock.assert_not_called()

    async def test_cache_miss_falls_through_to_slow_path(self, tmp_path: Path) -> None:
        # No cache pre-seeded -> slow path runs as normal.
        orch = WizardOrchestrator(data_dir=tmp_path)
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=_apply_result(tmp_path / "default" / "calibration.json"),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")
        assert result.status == WizardStatus.DONE

    async def test_slow_path_completion_populates_cache(self, tmp_path: Path) -> None:
        # Run slow path -> cache is populated for the next call.
        from sovyx.voice.calibration._kb_cache import has_match

        orch = WizardOrchestrator(data_dir=tmp_path)
        fingerprint = _fingerprint()
        with (
            patch.object(wo, "capture_fingerprint", return_value=fingerprint),
            patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=_apply_result(tmp_path / "default" / "calibration.json"),
            ),
        ):
            await orch.run(job_id="testjob", mind_id="default")

        assert (
            has_match(
                data_dir=tmp_path,
                fingerprint_hash=fingerprint.fingerprint_hash,
            )
            is True
        )


@pytest.mark.asyncio()
class TestOrchestratorFailures:
    async def test_diag_prerequisite_unmet_emits_fallback(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag_async",
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
                "run_full_diag_async",
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
            patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
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


# ====================================================================
# v0.30.24 T3.8: spec §8.3 wizard telemetry alignment
# ====================================================================


def _capture_wizard_logger() -> tuple[list[tuple[str, dict[str, Any]]], object]:
    events: list[tuple[str, dict[str, Any]]] = []

    class _Cap:
        def info(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    original = wo.logger
    wo.logger = _Cap()  # type: ignore[assignment]
    return events, original


def _restore_wizard_logger(original: object) -> None:
    wo.logger = original  # type: ignore[assignment]


class TestSpecTelemetryAlignment:
    """voice.calibration.wizard.{step_entered, path_chosen, completed, ...}."""

    async def test_slow_path_emits_step_entered_and_path_chosen_and_completed(
        self, tmp_path: Path
    ) -> None:
        events, original = _capture_wizard_logger()
        try:
            orch = WizardOrchestrator(data_dir=tmp_path)
            with (
                patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
                patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
                patch.object(wo, "triage_tarball", return_value=_triage()),
                patch.object(wo, "capture_measurements", return_value=_measurements()),
                patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
                patch.object(
                    wo.CalibrationApplier,
                    "apply",
                    return_value=_apply_result(Path("/tmp/x.json")),
                ),
            ):
                await orch.run(job_id="testjob", mind_id="default")
        finally:
            _restore_wizard_logger(original)

        names = [e[0] for e in events]
        assert "voice.calibration.wizard.step_entered" in names
        assert "voice.calibration.wizard.path_chosen" in names
        assert "voice.calibration.wizard.completed" in names

        # path_chosen lands once with path="slow" (cache miss path).
        chosen = [e for e in events if e[0] == "voice.calibration.wizard.path_chosen"]
        assert len(chosen) == 1
        assert chosen[0][1]["path"] == "slow"

        # completed has success=True + path=slow + duration_s present.
        completed = next(e for e in events if e[0] == "voice.calibration.wizard.completed")
        assert completed[1]["success"] is True
        assert completed[1]["path"] == "slow"
        assert "duration_s" in completed[1]

        # step_entered fires for "probe" + "slow_path".
        steps = {e[1]["step"] for e in events if e[0] == "voice.calibration.wizard.step_entered"}
        assert "probe" in steps
        assert "slow_path" in steps

    async def test_fast_path_emits_path_chosen_fast_and_completed(self, tmp_path: Path) -> None:
        from sovyx.voice.calibration._kb_cache import store_profile

        # Pre-seed the local KB so the orchestrator hits the fast path.
        store_profile(_r10_profile(), data_dir=tmp_path)

        events, original = _capture_wizard_logger()
        try:
            orch = WizardOrchestrator(data_dir=tmp_path)
            with (
                patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
                patch.object(
                    wo.CalibrationApplier,
                    "apply",
                    return_value=_apply_result(Path("/tmp/x.json")),
                ),
            ):
                await orch.run(job_id="testjob", mind_id="default")
        finally:
            _restore_wizard_logger(original)

        chosen = [e for e in events if e[0] == "voice.calibration.wizard.path_chosen"]
        assert len(chosen) == 1
        assert chosen[0][1]["path"] == "fast"

        steps = {e[1]["step"] for e in events if e[0] == "voice.calibration.wizard.step_entered"}
        assert "fast_path" in steps

    async def test_done_emits_step_entered_review(self, tmp_path: Path) -> None:
        """v0.30.26 spec §8.3: step_entered{step=review} fires on DONE terminal."""
        events, original = _capture_wizard_logger()
        try:
            orch = WizardOrchestrator(data_dir=tmp_path)
            with (
                patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
                patch.object(wo, "run_full_diag_async", return_value=_diag_result()),
                patch.object(wo, "triage_tarball", return_value=_triage()),
                patch.object(wo, "capture_measurements", return_value=_measurements()),
                patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
                patch.object(
                    wo.CalibrationApplier,
                    "apply",
                    return_value=_apply_result(Path("/tmp/x.json")),
                ),
            ):
                await orch.run(job_id="reviewjob", mind_id="default")
        finally:
            _restore_wizard_logger(original)

        steps = [e[1]["step"] for e in events if e[0] == "voice.calibration.wizard.step_entered"]
        # The review step fires AFTER probe + slow_path on the slow-path branch.
        assert "review" in steps, f"step_entered values: {steps}"
        # And it's the LAST step_entered emitted (DONE is the next state).
        assert steps[-1] == "review"

    async def test_fallback_emits_fallback_triggered(self, tmp_path: Path) -> None:
        events, original = _capture_wizard_logger()
        try:
            orch = WizardOrchestrator(data_dir=tmp_path)
            with (
                patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
                patch.object(
                    wo, "run_full_diag_async", side_effect=DiagPrerequisiteError("no bash")
                ),
            ):
                await orch.run(job_id="testjob", mind_id="default")
        finally:
            _restore_wizard_logger(original)

        triggered = next(
            (e for e in events if e[0] == "voice.calibration.wizard.fallback_triggered"),
            None,
        )
        assert triggered is not None
        assert triggered[1]["reason"] == "diag_prerequisite_unmet"
        # Path is reclassified to fallback even though slow was chosen first.
        assert triggered[1]["path"] == "fallback"
