"""Unit tests for sovyx.voice.calibration.engine.CalibrationEngine.

Coverage:

* Engine instantiation: rules sorted by priority desc + tie-break by id
* Engine.evaluate: returns frozen CalibrationProfile with the right
  fields + correct mind_id
* Determinism: same inputs (with fixed profile_id + timestamp) ->
  byte-identical output across re-runs
* Conflict resolution: lower-priority rules emitting "set" on a
  target a higher-priority rule already claimed are skipped silently
  (no override) -- the higher-priority rule wins
* "advise" + "preserve" decisions are NOT subject to conflict
  resolution (multiple advisories pass through)
* Engine version + rule_set_version land in the produced profile
* Empty rule set produces a profile with empty decisions + provenance
* iter_rules() discovers R10 from the rules subpackage
"""

from __future__ import annotations

from sovyx.voice.calibration import (
    CALIBRATION_PROFILE_SCHEMA_VERSION,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationEngine,
    CalibrationProfile,
    CalibrationRule,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
    RuleEvaluation,
    iter_rules,
)

# ====================================================================
# Fixtures (synthetic fingerprint + measurements)
# ====================================================================


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8.0-50-generic",
        kernel_major_minor="6.8",
        cpu_model="Intel Core i5-1240P",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="1.2.10",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VJFE69F11X-B0221H",
        capture_card_count=1,
        capture_devices=("Internal Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements_healthy() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0, -26.0, -25.5),
        vad_speech_probability_max=0.95,
        vad_speech_probability_p99=0.92,
        noise_floor_dbfs_estimate=-55.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=75,
        mixer_boost_pct=50,
        mixer_internal_mic_boost_pct=25,
        mixer_attenuation_regime="healthy",
        echo_correlation_db=-45.0,
        triage_winner_hid=None,
        triage_winner_confidence=None,
    )


# ====================================================================
# Synthetic rules for engine logic isolation
# ====================================================================


class _FixedDecisionRule:
    """Rule that always fires + emits a fixed decision tuple.

    Used to exercise engine machinery without depending on real
    rule preconditions. The ``decisions`` tuple is bound at __init__
    so each test can shape exactly what the engine sees.
    """

    def __init__(
        self,
        *,
        rule_id: str,
        priority: int,
        decisions: tuple[CalibrationDecision, ...],
        applies_result: bool = True,
    ) -> None:
        self.rule_id = rule_id
        self.rule_version = 1
        self.priority = priority
        self.description = f"test rule {rule_id}"
        self._decisions = decisions
        self._applies = applies_result

    def applies(self, ctx: RuleContext) -> bool:  # noqa: ARG002
        return self._applies

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:  # noqa: ARG002
        return RuleEvaluation(
            decisions=self._decisions,
            matched_conditions=(f"{self.rule_id} applied=True",),
        )


def _set_decision(*, target: str, value: str, rule_id: str = "R_test") -> CalibrationDecision:
    return CalibrationDecision(
        target=target,
        target_class="MindConfig.voice",
        operation="set",
        value=value,
        rationale="synthetic",
        rule_id=rule_id,
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


def _advise_decision(
    *, target: str = "advice.action", rule_id: str = "R_test"
) -> CalibrationDecision:
    return CalibrationDecision(
        target=target,
        target_class="TuningAdvice",
        operation="advise",
        value="run something",
        rationale="synthetic",
        rule_id=rule_id,
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


# ====================================================================
# Construction + ordering
# ====================================================================


class TestEngineConstruction:
    """Engine sorts rules by priority desc + tie-break by rule_id."""

    def test_priority_desc_ordering(self) -> None:
        low = _FixedDecisionRule(
            rule_id="R_LOW", priority=10, decisions=(_set_decision(target="x", value="low"),)
        )
        high = _FixedDecisionRule(
            rule_id="R_HIGH", priority=90, decisions=(_set_decision(target="y", value="high"),)
        )
        engine = CalibrationEngine(
            rules=(low, high),
            engine_version="0.30.15",
            rule_set_version=1,
        )
        assert tuple(r.rule_id for r in engine.rules) == ("R_HIGH", "R_LOW")

    def test_tie_breaker_by_rule_id_alphabetical(self) -> None:
        a = _FixedDecisionRule(rule_id="R_A", priority=50, decisions=())
        b = _FixedDecisionRule(rule_id="R_B", priority=50, decisions=())
        engine = CalibrationEngine(rules=(b, a), engine_version="0.30.15", rule_set_version=1)
        assert tuple(r.rule_id for r in engine.rules) == ("R_A", "R_B")

    def test_engine_version_default_falls_back_to_unknown(self) -> None:
        # When the package isn't installed (editable mode w/o metadata),
        # engine_version should default to "unknown" rather than raise.
        # We can't easily simulate the missing-package case in unit
        # tests, so we just check that the default path returns a
        # non-empty string (either the real version or "unknown").
        engine = CalibrationEngine(rules=(), rule_set_version=1)
        assert engine.engine_version  # truthy

    def test_engine_version_override(self) -> None:
        engine = CalibrationEngine(rules=(), engine_version="9.9.9", rule_set_version=42)
        assert engine.engine_version == "9.9.9"
        assert engine.rule_set_version == 42


# ====================================================================
# Evaluate: produced CalibrationProfile shape
# ====================================================================


class TestEvaluateShape:
    """Engine.evaluate returns a frozen, complete CalibrationProfile."""

    def test_returns_calibration_profile(self) -> None:
        engine = CalibrationEngine(rules=(), engine_version="0.30.15", rule_set_version=1)
        result = engine.evaluate(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
            profile_id="11111111-2222-3333-4444-555555555555",
            generated_at_utc="2026-05-05T18:02:00Z",
        )
        assert isinstance(result, CalibrationProfile)
        assert result.mind_id == "default"
        assert result.profile_id == "11111111-2222-3333-4444-555555555555"
        assert result.generated_at_utc == "2026-05-05T18:02:00Z"
        assert result.generated_by_engine_version == "0.30.15"
        assert result.generated_by_rule_set_version == 1
        assert result.schema_version == CALIBRATION_PROFILE_SCHEMA_VERSION
        assert result.signature is None  # Signed at persistence boundary

    def test_empty_rule_set_produces_no_decisions_or_provenance(self) -> None:
        engine = CalibrationEngine(rules=(), engine_version="0.30.15", rule_set_version=1)
        result = engine.evaluate(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
        )
        assert result.decisions == ()
        assert result.provenance == ()


# ====================================================================
# Determinism
# ====================================================================


class TestDeterminism:
    """Same inputs (with fixed UUID + timestamp) -> byte-identical output."""

    def test_two_runs_produce_identical_profile(self) -> None:
        rule = _FixedDecisionRule(
            rule_id="R_test",
            priority=50,
            decisions=(_set_decision(target="t", value="v"),),
        )
        engine = CalibrationEngine(rules=(rule,), engine_version="0.30.15", rule_set_version=1)
        kwargs = dict(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
            profile_id="11111111-2222-3333-4444-555555555555",
            generated_at_utc="2026-05-05T18:02:00Z",
        )
        a = engine.evaluate(**kwargs)
        b = engine.evaluate(**kwargs)
        assert a == b


# ====================================================================
# Conflict resolution
# ====================================================================


class TestConflictResolution:
    """Higher-priority rule wins for conflicting "set" target."""

    def test_higher_priority_wins_set_conflict(self) -> None:
        high_winner = _FixedDecisionRule(
            rule_id="R_HIGH",
            priority=90,
            decisions=(_set_decision(target="t", value="high_value", rule_id="R_HIGH"),),
        )
        low_loser = _FixedDecisionRule(
            rule_id="R_LOW",
            priority=10,
            decisions=(_set_decision(target="t", value="low_value", rule_id="R_LOW"),),
        )
        engine = CalibrationEngine(
            rules=(low_loser, high_winner),
            engine_version="0.30.15",
            rule_set_version=1,
        )
        result = engine.evaluate(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
        )
        # Only the high-priority decision survives; low-priority's
        # conflicting "set" is dropped.
        assert len(result.decisions) == 1
        assert result.decisions[0].value == "high_value"
        assert result.decisions[0].rule_id == "R_HIGH"
        # Provenance only records the rule that actually contributed
        # decisions (the low-priority rule's all-conflicting firing
        # is suppressed to keep --explain noise-free).
        assert len(result.provenance) == 1
        assert result.provenance[0].rule_id == "R_HIGH"

    def test_advise_decisions_do_not_conflict(self) -> None:
        a = _FixedDecisionRule(
            rule_id="R_A",
            priority=50,
            decisions=(_advise_decision(target="advice.action", rule_id="R_A"),),
        )
        b = _FixedDecisionRule(
            rule_id="R_B",
            priority=40,
            decisions=(_advise_decision(target="advice.action", rule_id="R_B"),),
        )
        engine = CalibrationEngine(rules=(a, b), engine_version="0.30.15", rule_set_version=1)
        result = engine.evaluate(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
        )
        # Both advisories pass through.
        assert len(result.decisions) == 2
        assert {d.rule_id for d in result.decisions} == {"R_A", "R_B"}

    def test_set_does_not_conflict_with_advise_on_same_target(self) -> None:
        # "set" and "advise" use different operations; the engine only
        # de-dupes "set" on (target, target_class).
        setter = _FixedDecisionRule(
            rule_id="R_SET",
            priority=60,
            decisions=(_set_decision(target="t", value="v"),),
        )
        adviser = _FixedDecisionRule(
            rule_id="R_ADVISE",
            priority=50,
            decisions=(_advise_decision(target="t", rule_id="R_ADVISE"),),
        )
        engine = CalibrationEngine(
            rules=(setter, adviser), engine_version="0.30.15", rule_set_version=1
        )
        result = engine.evaluate(
            mind_id="default",
            fingerprint=_fingerprint(),
            measurements=_measurements_healthy(),
        )
        assert len(result.decisions) == 2


# ====================================================================
# Rule discovery (iter_rules) picks up R10
# ====================================================================


class TestRuleDiscovery:
    """iter_rules() finds R10_mic_attenuated."""

    def test_r10_is_discovered(self) -> None:
        rule_ids = {r.rule_id for r in iter_rules()}
        assert "R10_mic_attenuated" in rule_ids

    def test_discovered_rules_satisfy_protocol(self) -> None:
        for r in iter_rules():
            assert isinstance(r, CalibrationRule)
            assert r.rule_id  # truthy
            assert r.rule_version >= 1
            assert 1 <= r.priority <= 100
            assert r.description  # truthy

    def test_default_engine_includes_r10(self) -> None:
        # CalibrationEngine() with no rules= override should discover.
        engine = CalibrationEngine(engine_version="0.30.15", rule_set_version=1)
        rule_ids = {r.rule_id for r in engine.rules}
        assert "R10_mic_attenuated" in rule_ids
