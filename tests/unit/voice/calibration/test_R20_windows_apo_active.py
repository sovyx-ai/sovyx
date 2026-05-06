"""Unit tests for sovyx.voice.calibration.rules.R20_windows_apo_active.

The rule fires when:
* fingerprint.apo_active is True (Windows Voice Clarity detected)
* triage_result.winner.hid == "H2" with confidence >= 0.7

It emits a single ADVISE decision telling the operator to enable
WASAPI exclusive capture mode.
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R20_windows_apo_active import rule
from sovyx.voice.diagnostics import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)

# ====================================================================
# Fixtures
# ====================================================================


def _fingerprint(
    *,
    apo_active: bool = True,
    apo_name: str | None = "VocaEffectPack",
) -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="windows",
        distro_id_like="windows",
        kernel_release="10.0.26200",
        kernel_major_minor="10.0",
        cpu_model="Intel",
        cpu_cores=8,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="wasapi",
        pipewire_version=None,
        pulseaudio_version=None,
        alsa_lib_version=None,
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Lenovo",
        system_product="ThinkPad X1",
        capture_card_count=1,
        capture_devices=("Microphone",),
        apo_active=apo_active,
        apo_name=apo_name,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0,),
        vad_speech_probability_max=0.005,  # APO destroys VAD signal
        vad_speech_probability_p99=0.005,
        noise_floor_dbfs_estimate=-55.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=None,
        mixer_capture_pct=None,
        mixer_boost_pct=None,
        mixer_internal_mic_boost_pct=None,
        mixer_attenuation_regime=None,
        echo_correlation_db=None,
        triage_winner_hid="H2",
        triage_winner_confidence=0.92,
    )


def _triage(
    *,
    hid: HypothesisId | None = HypothesisId.H2_VOICE_CLARITY_APO,
    confidence: float = 0.92,
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
        toolkit="windows",
        tarball_root=Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-05T18:00:00Z",
        os_descriptor="windows",
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


# ====================================================================
# Identity + priority
# ====================================================================


class TestRuleIdentity:
    def test_rule_id_and_version(self) -> None:
        assert rule.rule_id == "R20_windows_apo_active"
        assert rule.rule_version >= 1

    def test_priority_below_r10(self) -> None:
        # R10 (mixer attenuated) is priority 95; R20 must yield to it
        # when both fire on the same input.
        assert rule.priority == 80

    def test_description_truthy(self) -> None:
        assert rule.description


# ====================================================================
# applies() -- positive
# ====================================================================


class TestApplies:
    def test_fires_on_h2_winner_with_apo_active(self) -> None:
        assert rule.applies(_ctx()) is True

    def test_fires_at_threshold_confidence(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.7))
        assert rule.applies(ctx) is True

    def test_does_not_fire_when_apo_inactive(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(apo_active=False))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_h2_below_threshold(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.69))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_winner_is_not_h2(self) -> None:
        ctx = _ctx(triage_result=_triage(hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_no_triage(self) -> None:
        ctx = _ctx(triage_result=None)
        # explicit None means no triage available
        ctx_no_triage = RuleContext(
            fingerprint=_fingerprint(),
            measurements=_measurements(),
            triage_result=None,
            prior_decisions=(),
        )
        assert rule.applies(ctx_no_triage) is False
        assert ctx is not None  # quiet ARG002

    def test_does_not_fire_when_no_winner(self) -> None:
        ctx = _ctx(triage_result=_triage(hid=None))
        assert rule.applies(ctx) is False


# ====================================================================
# evaluate()
# ====================================================================


class TestEvaluate:
    def test_emits_single_advise_decision(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert len(evaluation.decisions) == 1
        decision = evaluation.decisions[0]
        assert decision.operation == "advise"
        assert decision.confidence == CalibrationConfidence.HIGH
        assert decision.rule_id == "R20_windows_apo_active"

    def test_advice_recommends_wasapi_exclusive(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert "WASAPI exclusive" in evaluation.decisions[0].value

    def test_rationale_mentions_apo_name(self) -> None:
        evaluation = rule.evaluate(_ctx(fingerprint=_fingerprint(apo_name="VocaEffectPack")))
        assert "VocaEffectPack" in evaluation.decisions[0].rationale

    def test_matched_conditions_record_apo_and_triage(self) -> None:
        evaluation = rule.evaluate(_ctx())
        joined = "\n".join(evaluation.matched_conditions)
        assert "apo_active" in joined
        assert "'H2'" in joined
