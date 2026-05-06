"""Unit tests for sovyx.voice.calibration._applier.CalibrationApplier.

Coverage matrix (post-P1 v0.30.29):

* :class:`ApplyResult` shape — applied/skipped/confirm_required partition + frozen
* dry_run + non-dry-run persistence semantics
* advised_actions extracted from advise decisions
* Confidence-band gating (D7): HIGH auto / MEDIUM confirm-required without flag,
  auto with allow_medium / LOW advise / EXPERIMENTAL skip
* Handler registry: register pair + lookup + unknown target_class raises ApplyError
* :class:`_PreApplySnapshot` — captured pre-apply state + per-decision tokens
* LIFO rollback: 3-decision-third-fails scenario reverts decisions in reverse order
* Rollback step itself raising → logged but does not mask original ApplyError
* Telemetry: apply_started + apply_succeeded + apply_failed_with_rollback +
  rollback_step_failed
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from sovyx.voice.calibration._applier import (
    _TARGET_CLASS_HANDLERS,
    _classify_decision_disposition,
    _DecisionDisposition,
    _PreApplySnapshot,
    register_target_class_pair,
)

# ════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════


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


def _set_high(
    *,
    target: str = "mind.voice.voice_input_device_name",
    target_class: str = "MindConfig.voice",
    value: Any = "Internal Mic",
) -> CalibrationDecision:
    return CalibrationDecision(
        target=target,
        target_class=target_class,
        operation="set",
        value=value,
        rationale="synthetic",
        rule_id="R_synthetic",
        rule_version=1,
        confidence=CalibrationConfidence.HIGH,
    )


def _set_medium() -> CalibrationDecision:
    return CalibrationDecision(
        target="mind.voice.vad_threshold",
        target_class="MindConfig.voice",
        operation="set",
        value=0.4,
        rationale="medium-confidence vad tune",
        rule_id="R_medium",
        rule_version=1,
        confidence=CalibrationConfidence.MEDIUM,
    )


def _set_low() -> CalibrationDecision:
    return CalibrationDecision(
        target="mind.voice.energy_threshold",
        target_class="MindConfig.voice",
        operation="set",
        value=0.05,
        rationale="low-confidence",
        rule_id="R_low",
        rule_version=1,
        confidence=CalibrationConfidence.LOW,
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
        generated_by_engine_version="0.30.29",
        generated_by_rule_set_version=11,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=None,
    )


# ════════════════════════════════════════════════════════════════════
# Capturing logger pattern (xdist-safe; per-test fresh instance)
# ════════════════════════════════════════════════════════════════════


def _capture_logger() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    from sovyx.voice.calibration import _applier as applier_module

    events: list[tuple[str, dict[str, Any]]] = []

    class _Capturing:
        def info(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    original = applier_module.logger
    applier_module.logger = _Capturing()  # type: ignore[assignment]
    return events, original


def _restore_logger(original: Any) -> None:
    from sovyx.voice.calibration import _applier as applier_module

    applier_module.logger = original  # type: ignore[assignment]


# ════════════════════════════════════════════════════════════════════
# advise-only profile (R10 v0.30.28 path; remains valid post-P1)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio()
class TestAdviseOnlyProfile:
    async def test_apply_advise_only_persists_profile(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = await applier.apply(profile)

        assert isinstance(result, ApplyResult)
        assert result.dry_run is False
        assert result.applied_decisions == ()
        assert len(result.skipped_decisions) == 1
        assert result.advised_actions == ("sovyx doctor voice --fix --yes",)

    async def test_apply_persists_to_canonical_path(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = await applier.apply(profile)
        expected = profile_path(data_dir=tmp_path, mind_id="default")
        assert result.profile_path == expected
        assert expected.is_file()

    async def test_dry_run_skips_persistence(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = await applier.apply(profile, dry_run=True)
        assert result.dry_run is True
        expected = profile_path(data_dir=tmp_path, mind_id="default")
        assert result.profile_path == expected
        assert not expected.exists()

    async def test_dry_run_still_returns_advice(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = await applier.apply(profile, dry_run=True)
        assert result.advised_actions == ("sovyx doctor voice --fix --yes",)


# ════════════════════════════════════════════════════════════════════
# Confidence-band gating (D7)
# ════════════════════════════════════════════════════════════════════


class TestConfidenceBandClassification:
    """Pure-function classification of decision disposition."""

    def test_high_set_auto_apply(self) -> None:
        d = _set_high()
        assert (
            _classify_decision_disposition(d, allow_medium=False)
            == _DecisionDisposition.AUTO_APPLY
        )

    def test_medium_set_confirm_required_without_flag(self) -> None:
        d = _set_medium()
        assert (
            _classify_decision_disposition(d, allow_medium=False)
            == _DecisionDisposition.CONFIRM_REQUIRED
        )

    def test_medium_set_auto_apply_with_allow_medium(self) -> None:
        d = _set_medium()
        assert (
            _classify_decision_disposition(d, allow_medium=True) == _DecisionDisposition.AUTO_APPLY
        )

    def test_low_set_advise_only(self) -> None:
        d = _set_low()
        assert (
            _classify_decision_disposition(d, allow_medium=True)
            == _DecisionDisposition.ADVISE_ONLY
        )

    def test_experimental_set_skipped(self) -> None:
        d = _experimental_set()
        assert _classify_decision_disposition(d, allow_medium=True) == _DecisionDisposition.SKIP

    def test_advise_operation_advise_only(self) -> None:
        d = _advise()
        assert (
            _classify_decision_disposition(d, allow_medium=True)
            == _DecisionDisposition.ADVISE_ONLY
        )


@pytest.mark.asyncio()
class TestConfidenceBandPartition:
    """The applier groups decisions into auto / confirm / skipped buckets."""

    async def test_medium_decision_lands_in_confirm_required_without_flag(
        self, tmp_path: Path
    ) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_set_medium(),))
        result = await applier.apply(profile, allow_medium=False)
        assert result.applied_decisions == ()
        assert len(result.confirm_required_decisions) == 1
        assert result.confirm_required_decisions[0].confidence == CalibrationConfidence.MEDIUM

    async def test_low_decision_lands_in_skipped(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_set_low(),))
        result = await applier.apply(profile)
        assert result.applied_decisions == ()
        assert result.confirm_required_decisions == ()
        assert len(result.skipped_decisions) == 1

    async def test_experimental_decision_lands_in_skipped(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_experimental_set(),))
        result = await applier.apply(profile)
        assert result.applied_decisions == ()
        assert result.confirm_required_decisions == ()
        assert len(result.skipped_decisions) == 1


# ════════════════════════════════════════════════════════════════════
# Handler registry
# ════════════════════════════════════════════════════════════════════


class TestRegistryFoundation:
    def test_built_in_handlers_registered_on_import(self) -> None:
        assert "LinuxMixerApply" in _TARGET_CLASS_HANDLERS
        assert "MindConfig.voice" in _TARGET_CLASS_HANDLERS

    def test_pair_lookup_returns_apply_and_revert(self) -> None:
        apply_fn, revert_fn = _TARGET_CLASS_HANDLERS["MindConfig.voice"]
        assert apply_fn is not None
        assert revert_fn is not None

    def test_register_pair_overwrites_existing(self) -> None:
        async def _noop_apply(*args: Any, **kwargs: Any) -> str:
            return "tok"

        async def _noop_revert(*args: Any, **kwargs: Any) -> None:
            return None

        # Save originals so we can restore.
        orig_apply, orig_revert = _TARGET_CLASS_HANDLERS["MindConfig.voice"]
        try:
            register_target_class_pair("MindConfig.voice", apply=_noop_apply, revert=_noop_revert)
            apply_fn, revert_fn = _TARGET_CLASS_HANDLERS["MindConfig.voice"]
            assert apply_fn is _noop_apply
            assert revert_fn is _noop_revert
        finally:
            register_target_class_pair("MindConfig.voice", apply=orig_apply, revert=orig_revert)


@pytest.mark.asyncio()
class TestUnknownTargetClass:
    async def test_unknown_target_class_raises_apply_error(self, tmp_path: Path) -> None:
        decision = _set_high(target_class="DoesNotExist")
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(decision,))
        with pytest.raises(ApplyError) as exc_info:
            await applier.apply(profile)
        assert exc_info.value.decision.target_class == "DoesNotExist"
        assert "no handler registered" in str(exc_info.value)


# ════════════════════════════════════════════════════════════════════
# LIFO rollback
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio()
class TestLifoRollback:
    """Three-decision third-fails: apply LIFO-reverts decisions 0 and 1."""

    async def test_third_decision_failure_triggers_lifo_rollback(self, tmp_path: Path) -> None:
        # Build a fake target_class with deterministic apply + revert
        # that we can introspect.
        applied_calls: list[int] = []
        reverted_calls: list[int] = []

        async def _apply_test(decision: CalibrationDecision, snapshot: Any, applier: Any) -> int:
            idx = int(decision.value)  # type: ignore[arg-type]
            if idx == 2:
                raise ApplyError(
                    f"synthetic third-fails at idx={idx}",
                    decision=decision,
                    decision_index=idx,
                )
            applied_calls.append(idx)
            return idx

        async def _revert_test(token: int, snapshot: Any, applier: Any) -> None:
            reverted_calls.append(token)

        register_target_class_pair("TestRollback", apply=_apply_test, revert=_revert_test)
        try:
            applier = CalibrationApplier(data_dir=tmp_path)
            decisions = (
                _set_high(target_class="TestRollback", value=0, target="t.0"),
                _set_high(target_class="TestRollback", value=1, target="t.1"),
                _set_high(target_class="TestRollback", value=2, target="t.2"),
            )
            profile = _profile(decisions=decisions)
            with pytest.raises(ApplyError):
                await applier.apply(profile)

            # Apply chain: 0, 1 succeeded; 2 failed.
            assert applied_calls == [0, 1]
            # LIFO rollback: 1 reverted before 0.
            assert reverted_calls == [1, 0]
        finally:
            del _TARGET_CLASS_HANDLERS["TestRollback"]

    async def test_rollback_step_failure_does_not_mask_original_error(
        self, tmp_path: Path
    ) -> None:
        async def _apply_fragile(
            decision: CalibrationDecision, snapshot: Any, applier: Any
        ) -> int:
            idx = int(decision.value)  # type: ignore[arg-type]
            if idx == 1:
                raise ApplyError(
                    f"second-fails at idx={idx}",
                    decision=decision,
                    decision_index=idx,
                )
            return idx

        async def _revert_explodes(token: int, snapshot: Any, applier: Any) -> None:
            raise RuntimeError(f"revert exploded for token={token}")

        register_target_class_pair(
            "TestFragileRevert", apply=_apply_fragile, revert=_revert_explodes
        )
        try:
            applier = CalibrationApplier(data_dir=tmp_path)
            decisions = (
                _set_high(target_class="TestFragileRevert", value=0, target="x.0"),
                _set_high(target_class="TestFragileRevert", value=1, target="x.1"),
            )
            profile = _profile(decisions=decisions)
            with pytest.raises(ApplyError) as exc_info:
                await applier.apply(profile)
            # Original ApplyError surfaces — NOT the rollback's RuntimeError.
            assert "second-fails" in str(exc_info.value)
            assert exc_info.value.decision_index == 1
        finally:
            del _TARGET_CLASS_HANDLERS["TestFragileRevert"]

    async def test_empty_applicable_no_op(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=(_advise(),))
        result = await applier.apply(profile)
        assert result.rolled_back is False


# ════════════════════════════════════════════════════════════════════
# Snapshot dataclass
# ════════════════════════════════════════════════════════════════════


class TestPreApplySnapshot:
    def test_default_construction(self) -> None:
        s = _PreApplySnapshot()
        assert s.mind_config_before == {}
        assert s.mixer_snapshots == {}
        assert s.decision_results == []

    def test_appendable_decision_results(self) -> None:
        s = _PreApplySnapshot()
        s.decision_results.append((0, True, "tok"))
        assert s.decision_results == [(0, True, "tok")]


# ════════════════════════════════════════════════════════════════════
# Telemetry events
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio()
class TestApplierTelemetry:
    async def test_apply_started_emitted_with_hashes(self, tmp_path: Path) -> None:
        events, original = _capture_logger()
        try:
            applier = CalibrationApplier(data_dir=tmp_path)
            profile = _profile(decisions=(_advise(),))
            await applier.apply(profile)
        finally:
            _restore_logger(original)

        started = next(e for e in events if e[0] == "voice.calibration.applier.apply_started")
        assert "profile_id_hash" in started[1]
        assert "mind_id_hash" in started[1]
        assert started[1]["mind_id_hash"] != "default"
        assert isinstance(started[1]["profile_id_hash"], str)
        assert len(started[1]["profile_id_hash"]) == 16

    async def test_apply_succeeded_includes_decisions_applied_field(self, tmp_path: Path) -> None:
        events, original = _capture_logger()
        try:
            applier = CalibrationApplier(data_dir=tmp_path)
            profile = _profile(decisions=(_advise(),))
            await applier.apply(profile)
        finally:
            _restore_logger(original)

        succeeded = next(e for e in events if e[0] == "voice.calibration.applier.apply_succeeded")
        assert "decisions_applied" in succeeded[1]
        assert "applicable_count" in succeeded[1]
        assert succeeded[1]["decisions_applied"] == succeeded[1]["applicable_count"]

    async def test_apply_failed_with_rollback_telemetry(self, tmp_path: Path) -> None:
        async def _apply_test(decision: CalibrationDecision, snapshot: Any, applier: Any) -> int:
            idx = int(decision.value)  # type: ignore[arg-type]
            if idx == 1:
                raise ApplyError(f"synthetic fail at {idx}", decision=decision, decision_index=idx)
            return idx

        async def _revert_test(token: int, snapshot: Any, applier: Any) -> None:
            return None

        register_target_class_pair("TestTelemetry", apply=_apply_test, revert=_revert_test)
        try:
            events, original = _capture_logger()
            try:
                applier = CalibrationApplier(data_dir=tmp_path)
                decisions = (
                    _set_high(target_class="TestTelemetry", value=0, target="y.0"),
                    _set_high(target_class="TestTelemetry", value=1, target="y.1"),
                )
                profile = _profile(decisions=decisions)
                with pytest.raises(ApplyError):
                    await applier.apply(profile)
            finally:
                _restore_logger(original)

            rollback_event = next(
                e for e in events if e[0] == "voice.calibration.applier.apply_failed_with_rollback"
            )
            assert rollback_event[1]["decisions_rolled_back"] == 1
            assert "rollback_duration_s" in rollback_event[1]
            assert isinstance(rollback_event[1]["rollback_duration_s"], (int, float))
        finally:
            del _TARGET_CLASS_HANDLERS["TestTelemetry"]

    async def test_rollback_step_failed_telemetry(self, tmp_path: Path) -> None:
        async def _apply_test(decision: CalibrationDecision, snapshot: Any, applier: Any) -> int:
            idx = int(decision.value)  # type: ignore[arg-type]
            if idx == 1:
                raise ApplyError("synthetic", decision=decision, decision_index=idx)
            return idx

        async def _revert_explodes(token: int, snapshot: Any, applier: Any) -> None:
            raise RuntimeError("revert blew up")

        register_target_class_pair("TestRevertFails", apply=_apply_test, revert=_revert_explodes)
        try:
            events, original = _capture_logger()
            try:
                applier = CalibrationApplier(data_dir=tmp_path)
                decisions = (
                    _set_high(target_class="TestRevertFails", value=0, target="z.0"),
                    _set_high(target_class="TestRevertFails", value=1, target="z.1"),
                )
                profile = _profile(decisions=decisions)
                with pytest.raises(ApplyError):
                    await applier.apply(profile)
            finally:
                _restore_logger(original)

            step_failed = next(
                e for e in events if e[0] == "voice.calibration.applier.rollback_step_failed"
            )
            assert step_failed[1]["target_class"] == "TestRevertFails"
            assert step_failed[1]["exception_type"] == "RuntimeError"
        finally:
            del _TARGET_CLASS_HANDLERS["TestRevertFails"]


# ════════════════════════════════════════════════════════════════════
# Empty profile + invariants
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio()
class TestEmptyProfile:
    async def test_apply_empty_profile(self, tmp_path: Path) -> None:
        applier = CalibrationApplier(data_dir=tmp_path)
        profile = _profile(decisions=())
        result = await applier.apply(profile)
        assert result.applied_decisions == ()
        assert result.skipped_decisions == ()
        assert result.advised_actions == ()
        assert result.profile_path.is_file()


class TestApplyResultInvariants:
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


# ════════════════════════════════════════════════════════════════════
# QA-FIX-1 (v0.31.0-rc.2) — ApplyError(decision=None) regression test
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio()
class TestMindYamlHelperRaiseRegressions:
    """Regression: helper failures must surface a typed ApplyError with
    the actual decision, not ``decision=None``.

    Pre-rc.2 the ``_mutate_mind_yaml_voice_field`` helper raised
    ``ApplyError(decision=None)`` directly; the outer except in
    ``CalibrationApplier.apply`` then crashed with ``AttributeError:
    'NoneType' object has no attribute 'target'`` when logging the
    failure event, masking the original error. Each path here exercises
    one of the helper's three error sites + asserts the surfaced
    ApplyError carries the test decision.
    """

    async def test_missing_yaml_file_surfaces_decision(self, tmp_path: Path) -> None:
        # mind_yaml_path is set but the file doesn't exist.
        applier = CalibrationApplier(data_dir=tmp_path, mind_yaml_path=tmp_path / "nope.yaml")
        decision = _set_high(target="mind.voice.vad_threshold")
        profile = _profile(decisions=(decision,))
        with pytest.raises(ApplyError) as exc_info:
            await applier.apply(profile)
        # The bug pre-rc.2: `exc.decision` was None and the outer
        # except's `exc.decision.target` log raised AttributeError
        # which masked the original ApplyError.
        assert exc_info.value.decision is not None
        assert exc_info.value.decision.target == "mind.voice.vad_threshold"
        assert "mind.yaml not found" in str(exc_info.value)

    async def test_malformed_yaml_surfaces_decision(self, tmp_path: Path) -> None:
        bad_yaml = tmp_path / "mind.yaml"
        bad_yaml.write_text("this: is: not: valid: yaml: [\n", encoding="utf-8")
        applier = CalibrationApplier(data_dir=tmp_path, mind_yaml_path=bad_yaml)
        decision = _set_high(target="mind.voice.vad_threshold")
        profile = _profile(decisions=(decision,))
        with pytest.raises(ApplyError) as exc_info:
            await applier.apply(profile)
        assert exc_info.value.decision is not None
        assert exc_info.value.decision.target == "mind.voice.vad_threshold"
        assert "unreadable or malformed" in str(exc_info.value)

    async def test_outer_except_logs_without_attribute_error(self, tmp_path: Path) -> None:
        """The pre-rc.2 bug: outer except crashed on `exc.decision.target`.

        Capture the applier's telemetry logger and verify the
        ``apply_failed`` event fires cleanly (with target/target_class/
        operation fields) instead of crashing inside the except block.
        """
        applier = CalibrationApplier(data_dir=tmp_path, mind_yaml_path=tmp_path / "missing.yaml")
        decision = _set_high(target="mind.voice.energy_threshold")
        profile = _profile(decisions=(decision,))

        events, original = _capture_logger()
        try:
            with pytest.raises(ApplyError):
                await applier.apply(profile)
        finally:
            _restore_logger(original)

        failed = next(
            (e for e in events if e[0] == "voice.calibration.applier.apply_failed"),
            None,
        )
        assert failed is not None, "apply_failed telemetry must fire"
        assert failed[1]["target"] == "mind.voice.energy_threshold"
        assert failed[1]["target_class"] == "MindConfig.voice"
        assert failed[1]["operation"] == "set"
        assert failed[1]["failure_reason"] == "set_dispatch_failed"
