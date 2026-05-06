"""Unit tests for sovyx.voice.calibration.rules.R50_hardware_gap.

Fires when:
* fingerprint.capture_card_count == 0
* triage_result.winner.hid == "H9" with confidence >= 0.7

Per-OS guidance is rendered in the advice value.
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R50_hardware_gap import rule
from sovyx.voice.diagnostics import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)


def _fingerprint(
    *,
    capture_card_count: int = 0,
    distro_id: str = "linuxmint",
) -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id=distro_id,
        distro_id_like="debian" if distro_id != "macos" else "darwin",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=8,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="1.2.10",
        codec_id=None,
        driver_family=None,
        system_vendor="Generic",
        system_product="x",
        capture_card_count=capture_card_count,
        capture_devices=() if capture_card_count == 0 else ("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=None,
        mixer_capture_pct=None,
        mixer_boost_pct=None,
        mixer_internal_mic_boost_pct=None,
        mixer_attenuation_regime=None,
        echo_correlation_db=None,
        triage_winner_hid="H9",
        triage_winner_confidence=0.9,
    )


def _triage(
    *,
    hid: HypothesisId | None = HypothesisId.H9_HARDWARE_GAP,
    confidence: float = 0.9,
) -> TriageResult:
    hypotheses: tuple[HypothesisVerdict, ...] = ()
    if hid is not None:
        hypotheses = (
            HypothesisVerdict(
                hid=hid,
                title="x",
                confidence=confidence,
                evidence_for=(),
                evidence_against=(),
                recommended_action=None,
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
        hypotheses=hypotheses,
    )


def _ctx(
    *,
    fingerprint: HardwareFingerprint | None = None,
    triage_result: TriageResult | None = None,
) -> RuleContext:
    return RuleContext(
        fingerprint=fingerprint if fingerprint is not None else _fingerprint(),
        measurements=_measurements(),
        triage_result=triage_result if triage_result is not None else _triage(),
        prior_decisions=(),
    )


class TestApplies:
    def test_fires_when_no_capture_card(self) -> None:
        assert rule.applies(_ctx()) is True

    def test_does_not_fire_when_capture_card_present(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(capture_card_count=1))
        assert rule.applies(ctx) is False

    def test_does_not_fire_on_unrelated_winner(self) -> None:
        ctx = _ctx(triage_result=_triage(hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_h9_below_threshold(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.5))
        assert rule.applies(ctx) is False


class TestEvaluate:
    def test_emits_advise(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert evaluation.decisions[0].operation == "advise"
        assert evaluation.decisions[0].confidence == CalibrationConfidence.HIGH

    def test_linux_advice_mentions_arecord(self) -> None:
        evaluation = rule.evaluate(_ctx(fingerprint=_fingerprint(distro_id="linuxmint")))
        assert "arecord" in evaluation.decisions[0].value

    def test_macos_advice_mentions_system_settings(self) -> None:
        evaluation = rule.evaluate(_ctx(fingerprint=_fingerprint(distro_id="macos")))
        assert "Bluetooth" in evaluation.decisions[0].value

    def test_windows_advice_mentions_settings_sound(self) -> None:
        evaluation = rule.evaluate(_ctx(fingerprint=_fingerprint(distro_id="windows")))
        assert "Settings" in evaluation.decisions[0].value
        assert "Sound" in evaluation.decisions[0].value


class TestPriority:
    def test_priority_below_r10_r20_r30_r40(self) -> None:
        assert rule.priority == 65
