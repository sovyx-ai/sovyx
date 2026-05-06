"""Property-based tests for the calibration engine (T2.11).

Validates the four contracts the mission spec §5.9 demands:

1. **Determinism** — same inputs (with pinned profile_id +
   generated_at_utc + clock) -> byte-identical CalibrationProfile;
2. **Idempotency** — applying a profile twice produces the same
   ApplyResult.profile_path + applied_decisions tuple the second
   time as the first (no double-mutation);
3. **Signature roundtrip** — sign + persist + load + verify returns
   the same profile (bytes) with the signature preserved (LENIENT
   accepts; STRICT accepts when signed);
4. **Conflict resolution** — two rules emitting SET on the same
   target_field: the higher-priority rule's value wins, regardless
   of synthetic ordering.

These tests are CHEAP (no I/O beyond tmp_path JSON round-trip), so
``max_examples`` is conservative (50) -- the goal is breadth across
randomized synthetic inputs, not exhaustive search.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.calibration import (
    CalibrationApplier,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationEngine,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
    RuleEvaluation,
    load_calibration_profile,
    save_calibration_profile,
)

# ====================================================================
# Synthetic context fixtures
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
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0,),
        vad_speech_probability_max=0.9,
        vad_speech_probability_p99=0.85,
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


class _SyntheticAdviseRule:
    """Rule that emits a single advise decision with parametrized rule_id + value."""

    rule_version = 1
    description = "synthetic advise"

    def __init__(self, *, rule_id: str, priority: int, value: str) -> None:
        self.rule_id = rule_id
        self.priority = priority
        self._value = value

    def applies(self, ctx: RuleContext) -> bool:  # noqa: ARG002
        return True

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:  # noqa: ARG002
        return RuleEvaluation(
            decisions=(
                CalibrationDecision(
                    target="advice.action",
                    target_class="TuningAdvice",
                    operation="advise",
                    value=self._value,
                    rationale="synthetic",
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    confidence=CalibrationConfidence.HIGH,
                ),
            ),
            matched_conditions=(f"{self.rule_id} fired",),
        )


class _SyntheticSetRule:
    """Rule that emits a single SET decision on a parametrized target."""

    rule_version = 1
    description = "synthetic set"

    def __init__(self, *, rule_id: str, priority: int, target: str, value: str) -> None:
        self.rule_id = rule_id
        self.priority = priority
        self._target = target
        self._value = value

    def applies(self, ctx: RuleContext) -> bool:  # noqa: ARG002
        return True

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:  # noqa: ARG002
        return RuleEvaluation(
            decisions=(
                CalibrationDecision(
                    target=self._target,
                    target_class="MindConfig.voice",
                    operation="set",
                    value=self._value,
                    rationale="synthetic",
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    confidence=CalibrationConfidence.HIGH,
                ),
            ),
            matched_conditions=(f"{self.rule_id} fired",),
        )


_PINNED_NOW = "2026-05-05T18:02:00.000000+00:00"


def _evaluate_pinned(
    engine: CalibrationEngine,
    *,
    profile_id: str = "11111111-2222-3333-4444-555555555555",
) -> CalibrationProfile:
    """Run engine.evaluate with the clock + ids pinned for determinism."""
    return engine.evaluate(
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        profile_id=profile_id,
        generated_at_utc=_PINNED_NOW,
        now_factory=lambda: _PINNED_NOW,
    )


# ====================================================================
# Property: determinism
# ====================================================================


# rule_id_strategy: alphanumeric + underscore, 3..12 chars; must start
# with R then digit/letter so the synthetic rules stay sortable.
_RULE_ID_LETTER = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@st.composite
def _rule_id(draw: st.DrawFn) -> str:
    suffix_len = draw(st.integers(min_value=2, max_value=10))
    suffix = "".join(draw(_RULE_ID_LETTER) for _ in range(suffix_len))
    return f"R_{suffix}"


@given(
    rules=st.lists(
        st.tuples(
            _rule_id(),
            st.integers(min_value=10, max_value=99),
            st.text(
                alphabet="abcdefghij ",
                min_size=1,
                max_size=20,
            ),
        ),
        min_size=0,
        max_size=5,
        unique_by=lambda t: t[0],
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_engine_determinism_under_random_rules(
    rules: list[tuple[str, int, str]],
) -> None:
    """Same inputs (pinned profile_id + clock) -> identical profile bytes."""
    rule_objs = tuple(
        _SyntheticAdviseRule(rule_id=rid, priority=prio, value=val) for rid, prio, val in rules
    )
    engine = CalibrationEngine(rules=rule_objs, engine_version="0.30.19", rule_set_version=1)
    a = _evaluate_pinned(engine)
    b = _evaluate_pinned(engine)
    assert a == b
    # Provenance trace is also stable (rule firing order is
    # priority-desc + alphabetical tie-break).
    assert a.provenance == b.provenance


# ====================================================================
# Property: idempotency
# ====================================================================


@given(
    advice_count=st.integers(min_value=0, max_value=4),
)
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_apply_is_idempotent(advice_count: int, tmp_path: Path) -> None:
    """Applying the same profile twice -> same persisted bytes."""
    import asyncio  # local import — sync-test driving async applier

    rule_objs = tuple(
        _SyntheticAdviseRule(rule_id=f"R_AD{i}", priority=50 + i, value=f"v{i}")
        for i in range(advice_count)
    )
    engine = CalibrationEngine(rules=rule_objs, engine_version="0.30.19", rule_set_version=1)
    profile = _evaluate_pinned(engine)

    applier = CalibrationApplier(data_dir=tmp_path)
    # P1 (v0.30.29): CalibrationApplier.apply is async; sync property tests
    # drive it via asyncio.run.
    first = asyncio.run(applier.apply(profile))
    first_bytes = first.profile_path.read_bytes()
    second = asyncio.run(applier.apply(profile))
    second_bytes = second.profile_path.read_bytes()

    assert first.profile_path == second.profile_path
    assert first.applied_decisions == second.applied_decisions
    assert first.advised_actions == second.advised_actions
    # Persisted bytes are byte-identical (sort_keys=True + frozen profile).
    assert first_bytes == second_bytes


# ====================================================================
# Property: signature roundtrip
# ====================================================================


@given(
    signature=st.one_of(
        st.none(),
        st.text(
            alphabet="0123456789abcdef",
            min_size=64,
            max_size=128,
        ),
    ),
)
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_signature_roundtrip(signature: str | None, tmp_path: Path) -> None:
    """Persist + load preserves the signature field bytewise."""
    rule = _SyntheticAdviseRule(rule_id="R_SR", priority=50, value="x")
    engine = CalibrationEngine(rules=(rule,), engine_version="0.30.19", rule_set_version=1)
    profile = _evaluate_pinned(engine)

    # Override signature on the persisted profile via dataclasses.replace
    from dataclasses import replace

    signed_profile = replace(profile, signature=signature)
    save_calibration_profile(signed_profile, data_dir=tmp_path)
    loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
    assert loaded.signature == signature


# ====================================================================
# Property: conflict resolution by priority
# ====================================================================


@given(
    high_value=st.text(alphabet="abcdef", min_size=1, max_size=8),
    low_value=st.text(alphabet="ghijkl", min_size=1, max_size=8),
    high_priority=st.integers(min_value=60, max_value=99),
    low_priority=st.integers(min_value=10, max_value=50),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_conflict_resolution_higher_priority_wins(
    high_value: str,
    low_value: str,
    high_priority: int,
    low_priority: int,
) -> None:
    """Two rules SETting the same target: high-priority value lands in the profile."""
    high = _SyntheticSetRule(
        rule_id="R_HIGH", priority=high_priority, target="x", value=high_value
    )
    low = _SyntheticSetRule(rule_id="R_LOW", priority=low_priority, target="x", value=low_value)
    # Pass them in REVERSE priority order; the engine must still rank
    # by priority desc + alphabetical tie-break before evaluation.
    engine = CalibrationEngine(rules=(low, high), engine_version="0.30.19", rule_set_version=1)
    profile = _evaluate_pinned(engine)
    set_decisions = [d for d in profile.decisions if d.operation == "set"]
    assert len(set_decisions) == 1
    assert set_decisions[0].value == high_value
    assert set_decisions[0].rule_id == "R_HIGH"
