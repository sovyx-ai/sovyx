"""Unit tests for sovyx.voice.calibration.rules.R30_linux_destructive_filter.

The rule fires when:
* fingerprint.audio_stack in {"pipewire", "pulseaudio"}
* fingerprint.pulse_modules_destructive is non-empty
* triage_result.winner.hid == "H4" with confidence >= 0.7

Emits an ADVISE pointing the operator at `pactl unload-module` +
the persistent ~/.config/pulse/default.pa edit.
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R30_linux_destructive_filter import rule
from sovyx.voice.diagnostics import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)


def _fingerprint(
    *,
    audio_stack: str = "pulseaudio",
    destructive: tuple[str, ...] = ("module-echo-cancel",),
) -> HardwareFingerprint:
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
        audio_stack=audio_stack,
        pipewire_version="1.0.5" if audio_stack == "pipewire" else None,
        pulseaudio_version="16.1" if audio_stack == "pulseaudio" else None,
        alsa_lib_version="1.2.10",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=destructive,
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-80.0,),
        vad_speech_probability_max=0.005,
        vad_speech_probability_p99=0.005,
        noise_floor_dbfs_estimate=-90.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=70,
        mixer_boost_pct=30,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="healthy",
        echo_correlation_db=None,
        triage_winner_hid="H4",
        triage_winner_confidence=0.85,
    )


def _triage(
    *,
    hid: HypothesisId | None = HypothesisId.H4_LINUX_DESTRUCTIVE_FILTER,
    confidence: float = 0.85,
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


class TestRuleIdentity:
    def test_rule_id_and_version(self) -> None:
        assert rule.rule_id == "R30_linux_destructive_filter"
        assert rule.rule_version >= 1

    def test_priority_below_r20_above_r60(self) -> None:
        assert rule.priority == 75


class TestApplies:
    def test_fires_on_h4_with_destructive_module(self) -> None:
        assert rule.applies(_ctx()) is True

    def test_fires_on_pipewire_with_destructive(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(audio_stack="pipewire"))
        assert rule.applies(ctx) is True

    def test_does_not_fire_when_no_destructive_modules(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(destructive=()))
        assert rule.applies(ctx) is False

    def test_does_not_fire_on_alsa_only(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(audio_stack="alsa-only"))
        assert rule.applies(ctx) is False

    def test_does_not_fire_when_h4_below_threshold(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.69))
        assert rule.applies(ctx) is False

    def test_does_not_fire_on_unrelated_winner(self) -> None:
        ctx = _ctx(triage_result=_triage(hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED))
        assert rule.applies(ctx) is False


class TestEvaluate:
    def test_emits_single_advise(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert len(evaluation.decisions) == 1
        decision = evaluation.decisions[0]
        assert decision.operation == "advise"
        assert decision.confidence == CalibrationConfidence.HIGH

    def test_advice_mentions_pactl_and_persist(self) -> None:
        evaluation = rule.evaluate(_ctx())
        value = evaluation.decisions[0].value
        assert "pactl" in value
        assert "default.pa" in value

    def test_rationale_lists_modules(self) -> None:
        evaluation = rule.evaluate(
            _ctx(fingerprint=_fingerprint(destructive=("module-echo-cancel",)))
        )
        assert "module-echo-cancel" in evaluation.decisions[0].rationale
