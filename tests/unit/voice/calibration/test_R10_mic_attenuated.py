"""Unit tests for sovyx.voice.calibration.rules.R10_mic_attenuated.

The rule fires when:
* Linux audio stack (pipewire/pulseaudio/alsa-only)
* mixer_attenuation_regime == "attenuated"
* triage_result.winner.hid == "H10" with confidence >= 0.7

It emits a single ADVISE decision pointing the operator at
``sovyx doctor voice --fix --yes``.
"""

from __future__ import annotations

from sovyx.voice.calibration import (
    CalibrationConfidence,
    CalibrationDecision,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R10_mic_attenuated import rule
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


def _fingerprint(*, audio_stack: str = "pipewire") -> HardwareFingerprint:
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
        pulse_modules_destructive=(),
    )


def _measurements(
    *,
    regime: str | None = "attenuated",
    capture_pct: int | None = 40,
    boost_pct: int | None = 0,
) -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-90.0,),
        vad_speech_probability_max=0.001,
        vad_speech_probability_p99=0.001,
        noise_floor_dbfs_estimate=-95.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=capture_pct,
        mixer_boost_pct=boost_pct,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime=regime,
        echo_correlation_db=None,
        triage_winner_hid="H10",
        triage_winner_confidence=0.95,
    )


def _triage(
    *,
    hid: HypothesisId | None = HypothesisId.H10_LINUX_MIXER_ATTENUATED,
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
    from pathlib import Path

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


_DEFAULT_TRIAGE = object()  # Sentinel distinguishing "default" from explicit None.


def _ctx(
    *,
    fingerprint: HardwareFingerprint | None = None,
    measurements: MeasurementSnapshot | None = None,
    triage_result: TriageResult | None | object = _DEFAULT_TRIAGE,
) -> RuleContext:
    if triage_result is _DEFAULT_TRIAGE:
        resolved_triage: TriageResult | None = _triage()
    else:
        resolved_triage = triage_result  # type: ignore[assignment]
    return RuleContext(
        fingerprint=fingerprint if fingerprint is not None else _fingerprint(),
        measurements=measurements if measurements is not None else _measurements(),
        triage_result=resolved_triage,
        prior_decisions=(),
    )


# ====================================================================
# applies()
# ====================================================================


class TestApplies:
    """Precondition gates."""

    def test_canonical_case_applies(self) -> None:
        # Sony VAIO + pipewire + attenuated + H10 winner -> fires.
        assert rule.applies(_ctx()) is True

    def test_pulseaudio_stack_applies(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(audio_stack="pulseaudio"))
        assert rule.applies(ctx) is True

    def test_alsa_only_stack_applies(self) -> None:
        ctx = _ctx(fingerprint=_fingerprint(audio_stack="alsa-only"))
        assert rule.applies(ctx) is True

    def test_non_linux_audio_stack_does_not_apply(self) -> None:
        # The rule is Linux-specific; macOS / Windows audio stacks
        # don't go through the amixer signature path.
        ctx = _ctx(fingerprint=_fingerprint(audio_stack="coreaudio"))
        assert rule.applies(ctx) is False

    def test_healthy_regime_does_not_apply(self) -> None:
        ctx = _ctx(measurements=_measurements(regime="healthy"))
        assert rule.applies(ctx) is False

    def test_saturated_regime_does_not_apply(self) -> None:
        # Saturated is a different mode, handled by the existing
        # --fix path, not the calibration rule.
        ctx = _ctx(measurements=_measurements(regime="saturated"))
        assert rule.applies(ctx) is False

    def test_no_triage_result_does_not_apply(self) -> None:
        ctx = _ctx(triage_result=None)
        assert rule.applies(ctx) is False

    def test_triage_with_no_winner_does_not_apply(self) -> None:
        # Empty hypotheses => no winner.
        ctx = _ctx(triage_result=_triage(hid=None))
        assert rule.applies(ctx) is False

    def test_triage_winner_below_threshold_does_not_apply(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.65))
        assert rule.applies(ctx) is False

    def test_triage_winner_at_threshold_applies(self) -> None:
        ctx = _ctx(triage_result=_triage(confidence=0.7))
        assert rule.applies(ctx) is True

    def test_different_hypothesis_winner_does_not_apply(self) -> None:
        # H1 winner with 0.95 confidence is NOT R10's trigger.
        ctx = _ctx(triage_result=_triage(hid=HypothesisId.H1_MIC_DESTROYED))
        assert rule.applies(ctx) is False


# ====================================================================
# evaluate()
# ====================================================================


class TestEvaluate:
    """Decision shape + matched conditions."""

    def test_emits_one_set_decision(self) -> None:
        # P1 (v0.30.29): R10 promoted from advise to SET targeting
        # LinuxMixerApply with intent boost_up. The applier dispatches
        # directly; operator no longer needs `sovyx doctor voice --fix`.
        evaluation = rule.evaluate(_ctx())
        assert len(evaluation.decisions) == 1
        d = evaluation.decisions[0]
        assert isinstance(d, CalibrationDecision)
        assert d.operation == "set"
        assert d.target == "mixer.preset.applied"
        assert d.target_class == "LinuxMixerApply"
        assert d.value == "boost_up"
        assert d.rule_id == "R10_mic_attenuated"
        assert d.rule_version == 2
        assert d.confidence == CalibrationConfidence.HIGH

    def test_rationale_includes_mixer_state(self) -> None:
        evaluation = rule.evaluate(_ctx())
        rationale = evaluation.decisions[0].rationale
        assert "capture=40%" in rationale
        assert "boost=0%" in rationale

    def test_matched_conditions_describe_gates(self) -> None:
        evaluation = rule.evaluate(_ctx())
        # 3 gates: audio_stack + mixer_attenuation_regime + triage winner.
        assert len(evaluation.matched_conditions) == 3
        joined = " | ".join(evaluation.matched_conditions)
        assert "audio_stack" in joined
        assert "mixer_attenuation_regime" in joined
        assert "H10" in joined


# ====================================================================
# Metadata
# ====================================================================


class TestRuleMetadata:
    """Static rule attributes."""

    def test_rule_id_is_stable(self) -> None:
        assert rule.rule_id == "R10_mic_attenuated"

    def test_priority_is_high_band(self) -> None:
        # 95 is the spec'd value; if the rule's priority changes the
        # docstring rationale must be updated too. Pin to enforce.
        assert rule.priority == 95

    def test_rule_version_p1_promotion(self) -> None:
        # P1 (v0.30.29) promoted R10 from ADVISE to SET; rule_version
        # bumped 1 -> 2 so the persistence layer can detect drift between
        # profile-time and runtime rule sets.
        assert rule.rule_version == 2

    def test_description_mentions_remediation(self) -> None:
        assert (
            "doctor voice --fix" in rule.description.lower() or "fix" in rule.description.lower()
        )
