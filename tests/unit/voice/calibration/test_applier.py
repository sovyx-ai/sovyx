"""Unit tests for sovyx.voice.calibration._applier.CalibrationApplier.

Coverage:
* ApplyResult shape: applied_decisions + skipped_decisions partition
* dry_run skips persistence + state mutation
* Non-dry-run persists profile via save_calibration_profile
* advised_actions extracted from advise decisions
* SET decisions raise ApplyError (v0.30.15 has no SET dispatch)
* Frozen ApplyResult invariants
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.voice.calibration import (
    ApplyError,
    ApplyResult,
    CalibrationApplier,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    profile_path,
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
        kernel_release="6.8.0-50-generic",
        kernel_major_minor="6.8",
        cpu_model="Intel",
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


def _advise() -> CalibrationDecision:
    return CalibrationDecision(
        target="advice.action",
        target_class="TuningAdvice",
        operation="advise",
        value="sovyx doctor voice --fix --yes",
        rationale="r10 mixer attenuated",
        rule_id="R10_mic_attenuated",
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


def _set(*, target: str = "mind.voice.voice_input_device_name") -> CalibrationDecision:
    return CalibrationDecision(
        target=target,
        target_class="MindConfig.voice",
        operation="set",
        value="Internal Mic",
        rationale="synthetic",
        rule_id="R_synthetic",
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


def _experimental_set() -> CalibrationDecision:
    return CalibrationDecision(
        target="tuning.voice.vad_threshold",
        target_class="TuningAdvice",
        operation="set",
        value=0.5,
        rationale="experimental",
        rule_id="R_experimental",
        rule_version=1,
        confidence=CalibrationConfidence.EXPERIMENTAL,
    )


def _profile(*, decisions: tuple[CalibrationDecision, ...]) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=decisions,
        provenance=(),
        generated_by_engine_version="0.30.15",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=None,
    )


# ====================================================================
# advise-only profile (the v0.30.15 happy path with R10)
# ====================================================================


class TestAdviseOnlyProfile:
    """R10's advise decisions: zero applied, advice surfaced for operator."""

    def test_apply_advise_only_persists_profile(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = applier.apply(profile)

        assert isinstance(result, ApplyResult)
        assert result.dry_run is False
        # advise decisions are NOT applicable -> applied is empty.
        assert result.applied_decisions == ()
        # The advise decision lands in skipped (not "set" + non-experimental).
        assert len(result.skipped_decisions) == 1
        # The advice action surfaces for operator copy-paste.
        assert result.advised_actions == ("sovyx doctor voice --fix --yes",)

    def test_apply_persists_to_canonical_path(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = applier.apply(profile)
        expected = profile_path(data_dir=tmp_path, mind_id="default")
        assert result.profile_path == expected
        assert expected.is_file()

    def test_dry_run_skips_persistence(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = applier.apply(profile, dry_run=True)
        assert result.dry_run is True
        # Path is computed but the file is NOT written.
        expected = profile_path(data_dir=tmp_path, mind_id="default")
        assert result.profile_path == expected
        assert not expected.exists()

    def test_dry_run_still_returns_advice(self, tmp_path: Path) -> None:
        # The operator should still SEE the advice in --dry-run output
        # so they know what would happen.
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = applier.apply(profile, dry_run=True)
        assert result.advised_actions == ("sovyx doctor voice --fix --yes",)


# ====================================================================
# Mixed decisions: experimental SET filtered out
# ====================================================================


class TestMixedDecisions:
    """Profile with advise + experimental-set: experimental filtered."""

    def test_experimental_set_is_skipped_not_applied(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(), _experimental_set()))
        result = applier.apply(profile)
        # Neither decision is "applicable" (advise is not SET;
        # experimental SET is filtered by applicable_decisions).
        assert result.applied_decisions == ()
        assert len(result.skipped_decisions) == 2
        # Advice still surfaces.
        assert result.advised_actions == ("sovyx doctor voice --fix --yes",)


# ====================================================================
# SET decisions raise (no dispatch in v0.30.15)
# ====================================================================


class TestSetDispatchNotImplemented:
    """v0.30.15 has no SET dispatch; raise ApplyError when one fires."""

    def test_set_decision_raises_apply_error(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_set(),))
        with pytest.raises(ApplyError) as exc_info:
            applier.apply(profile)
        assert exc_info.value.decision.target == "mind.voice.voice_input_device_name"
        assert "v0.30.15" in str(exc_info.value)

    def test_apply_error_carries_decision(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        decision = _set(target="mind.voice.wake_word_enabled")
        profile = _profile(decisions=(decision,))
        with pytest.raises(ApplyError) as exc_info:
            applier.apply(profile)
        assert exc_info.value.decision == decision

    def test_set_decision_dry_run_still_raises(self, tmp_path: Path) -> None:
        # Dry-run goes through the dispatch path so it surfaces the
        # gap early; otherwise an operator running --dry-run might
        # get false confidence that an unsupported SET is "ready".
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_set(),))
        with pytest.raises(ApplyError):
            applier.apply(profile, dry_run=True)


# ====================================================================
# Empty profile
# ====================================================================


class TestEmptyProfile:
    """Profile with no decisions: still persists, returns empty tuples."""

    def test_apply_empty_profile(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=())
        result = applier.apply(profile)
        assert result.applied_decisions == ()
        assert result.skipped_decisions == ()
        assert result.advised_actions == ()
        assert result.profile_path.is_file()


# ====================================================================
# ApplyResult invariants
# ====================================================================


class TestApplyResultInvariants:
    """ApplyResult is frozen."""

    def test_apply_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = ApplyResult(
            profile_path=Path("/tmp/x"),
            applied_decisions=(),
            skipped_decisions=(),
            advised_actions=(),
            dry_run=False,
        )
        with pytest.raises(FrozenInstanceError):
            result.dry_run = True  # type: ignore[misc]
