"""Unit tests for sovyx.voice.calibration.rules.R40_macos_tcc_denied.

The rule fires when:
* fingerprint.distro_id == "macos"
* triage_result.winner.hid == "H5" with confidence >= 0.7
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R40_macos_tcc_denied import rule
from sovyx.voice.diagnostics import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)


def _fingerprint(*, distro_id: str = "macos") -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id=distro_id,
        distro_id_like="darwin" if distro_id == "macos" else "debian",
        kernel_release="24.0.0",
        kernel_major_minor="24.0",
        cpu_model="Apple M3",
        cpu_cores=8,
        ram_mb=16384,
        has_gpu=True,
        gpu_vram_mb=8192,
        audio_stack="coreaudio",
        pipewire_version=None,
        pulseaudio_version=None,
        alsa_lib_version=None,
        codec_id=None,
        driver_family=None,
        system_vendor="Apple",
        system_product="MacBook Air",
        capture_card_count=1,
        capture_devices=("Built-in Microphone",),
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
        rms_dbfs_per_capture=(-120.0,),  # silence
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=-120.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=None,
        mixer_capture_pct=None,
        mixer_boost_pct=None,
        mixer_internal_mic_boost_pct=None,
        mixer_attenuation_regime=None,
        echo_correlation_db=None,
        triage_winner_hid="H5",
        triage_winner_confidence=0.95,
    )


def _triage(
    *,
    hid: HypothesisId | None = HypothesisId.H5_MIC_PERMISSION_DENIED,
    confidence: float = 0.95,
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
        toolkit="macos",
        tarball_root=Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-05T18:00:00Z",
        os_descriptor="macos",
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


class TestRuleIdentity:
    def test_rule_id_priority(self) -> None:
        assert rule.rule_id == "R40_macos_tcc_denied"
        assert rule.priority == 70


class TestApplies:
    def test_fires_on_macos_h5(self) -> None:
        assert rule.applies(_ctx()) is True

    def test_does_not_fire_on_linux(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(distro_id="linuxmint"))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_h5_below_threshold(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.5))
        assert rule.applies(ctx) is False

    def test_does_not_fire_on_unrelated_winner(self) -> None:
        ctx = _ctx(triage_result=_triage(hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED))
        assert rule.applies(ctx) is False


class TestEvaluate:
    def test_emits_advise_high_confidence(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert evaluation.decisions[0].operation == "advise"
        assert evaluation.decisions[0].confidence == CalibrationConfidence.HIGH

    def test_advice_mentions_privacy_security(self) -> None:
        evaluation = rule.evaluate(_ctx())
        value = evaluation.decisions[0].value
        assert "Privacy" in value
        assert "Microphone" in value
