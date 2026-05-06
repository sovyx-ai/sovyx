"""Tests for voice.calibration.wizard.* structured telemetry events.

T3.8 of MISSION-voice-self-calibrating-system-2026-05-05.md (v0.30.18 C1).
Verifies each lifecycle event emits at the right moment + carries
the closed-enum cardinality fields the operator-facing dashboards
will subscribe to:

* voice.calibration.wizard.job_started   -- once at run() top
* voice.calibration.wizard.stage_transition -- once per state mutation
* voice.calibration.wizard.terminal      -- once at terminal state
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_capturing_logger() -> MagicMock:
    """Build a MagicMock logger that records every call for inspection.

    Structlog's stdlib bridge doesn't surface the bound fields on the
    LogRecord in a way pytest.caplog can read cleanly, so we patch the
    orchestrator's module-level ``logger`` object directly + assert on
    the recorded calls (event name = first positional arg; bound fields
    = kwargs).
    """
    return MagicMock()


def _events(logger: MagicMock) -> list[str]:
    """Return the event names from every logger.info call on the mock."""
    out: list[str] = []
    for call in logger.info.call_args_list:
        if call.args:
            out.append(str(call.args[0]))
    return out


def _last_kwargs_for(logger: MagicMock, event_name: str) -> dict[str, Any]:
    """Return the kwargs of the most-recent ``logger.info(event_name, **kw)`` call."""
    for call in reversed(logger.info.call_args_list):
        if call.args and call.args[0] == event_name:
            return dict(call.kwargs)
    return {}


from sovyx.voice.calibration import (
    ApplyResult,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    WizardOrchestrator,
    WizardStatus,
)
from sovyx.voice.calibration import _wizard_orchestrator as wo
from sovyx.voice.diagnostics import (
    AlertsSummary,
    DiagPrerequisiteError,
    DiagRunResult,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-06T18:00:00Z",
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
        captured_at_utc="2026-05-06T18:01:00Z",
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


def _triage() -> TriageResult:
    return TriageResult(
        schema_version=1,
        toolkit="linux",
        tarball_root=Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-06T18:00:00Z",
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
        hypotheses=(
            HypothesisVerdict(
                hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
                title="x",
                confidence=0.95,
                evidence_for=(),
                evidence_against=(),
                recommended_action="sovyx doctor voice --fix --yes",
            ),
        ),
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
        generated_by_engine_version="0.30.18",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-06T18:02:00Z",
        signature=None,
    )


@pytest.mark.asyncio()
class TestWizardTelemetryEvents:
    async def test_successful_run_emits_full_lifecycle(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        profile_path = tmp_path / "default" / "calibration.json"
        capturing = _make_capturing_logger()
        with (
            patch.object(wo, "logger", capturing),
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                return_value=DiagRunResult(
                    tarball_path=Path("/tmp/x.tar.gz"),
                    duration_s=600.0,
                    exit_code=0,
                ),
            ),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=ApplyResult(
                    profile_path=profile_path,
                    applied_decisions=(),
                    skipped_decisions=(),
                    advised_actions=("sovyx doctor voice --fix --yes",),
                    dry_run=False,
                ),
            ),
        ):
            await orch.run(job_id="testjob", mind_id="default")

        events = _events(capturing)
        assert "voice.calibration.wizard.job_started" in events
        # 6 stage transitions: PENDING + PROBING + SLOW_PATH_DIAG +
        # SLOW_PATH_CALIBRATE + SLOW_PATH_APPLY + DONE.
        assert events.count("voice.calibration.wizard.stage_transition") == 6
        assert "voice.calibration.wizard.terminal" in events

    async def test_terminal_event_carries_winner_hid(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        profile_path = tmp_path / "default" / "calibration.json"
        capturing = _make_capturing_logger()
        with (
            patch.object(wo, "logger", capturing),
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                return_value=DiagRunResult(
                    tarball_path=Path("/tmp/x.tar.gz"),
                    duration_s=600.0,
                    exit_code=0,
                ),
            ),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(wo.CalibrationEngine, "evaluate", return_value=_r10_profile()),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=ApplyResult(
                    profile_path=profile_path,
                    applied_decisions=(),
                    skipped_decisions=(),
                    advised_actions=(),
                    dry_run=False,
                ),
            ),
        ):
            await orch.run(job_id="testjob", mind_id="default")

        kwargs = _last_kwargs_for(capturing, "voice.calibration.wizard.terminal")
        assert kwargs.get("status") == "done"
        assert kwargs.get("triage_winner_hid") == "H10"

    async def test_fallback_terminal_event_carries_reason(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        capturing = _make_capturing_logger()
        with (
            patch.object(wo, "logger", capturing),
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                side_effect=DiagPrerequisiteError("no bash"),
            ),
        ):
            result = await orch.run(job_id="testjob", mind_id="default")

        assert result.status == WizardStatus.FALLBACK
        kwargs = _last_kwargs_for(capturing, "voice.calibration.wizard.terminal")
        assert kwargs.get("status") == "fallback"
        assert kwargs.get("fallback_reason") == "diag_prerequisite_unmet"
