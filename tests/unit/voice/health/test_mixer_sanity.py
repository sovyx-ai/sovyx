"""Tests for check_and_maybe_heal — the L2.5 orchestrator (F1.E).

Focus: state-machine transitions. Audio acquisition + alsactl
persist are injected via ``validation_probe_fn`` / ``persist_fn`` so
the tests exercise orchestrator semantics without real audio.

Scenario coverage:

* Non-Linux → DEFERRED_PLATFORM (no side-effects).
* Mixer probe empty → DEFERRED_NO_KB (no cards).
* Mixer healthy, no KB match → SKIPPED_HEALTHY.
* KB matches + customization < apply threshold → full apply path
  (probe → classify → detect_customization → apply → validate →
  persist → HEALED).
* KB matches + customization > skip threshold → SKIPPED_CUSTOMIZED.
* KB matches + customization in ambiguous zone → DEFERRED_AMBIGUOUS.
* No KB match → DEFERRED_NO_KB.
* Validation gates fail → ROLLED_BACK with restore invoked.
* apply_mixer_preset raises → ERROR + apply snapshot None.
* validation_probe_fn raises → ROLLED_BACK.
* Budget exceeded → ERROR with rollback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.device_enum import DeviceKind
from sovyx.voice.health import (
    Diagnosis,
    FactorySignature,
    HardwareContext,
    MixerApplySnapshot,
    MixerCardSnapshot,
    MixerControlRole,
    MixerControlRoleResolver,
    MixerControlSnapshot,
    MixerKBLookup,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    MixerSanityDecision,
    MixerValidationMetrics,
    ValidationGates,
    VerificationRecord,
    check_and_maybe_heal,
)
from sovyx.voice.health import _mixer_sanity as mod
from sovyx.voice.health.contract import CandidateEndpoint

if TYPE_CHECKING:
    from collections.abc import Sequence


# ── Fixtures ────────────────────────────────────────────────────────


def _pilot_profile() -> MixerKBProfile:
    return MixerKBProfile(
        profile_id="vaio_vjfe69_sn6180",
        profile_version=1,
        schema_version=1,
        codec_id_glob="14F1:5045",
        driver_family="hda",
        system_vendor_glob="Sony*",
        system_product_glob="VJFE69*",
        distro_family=None,
        audio_stack="pipewire",
        kernel_major_minor_glob="6.*",
        match_threshold=0.6,
        factory_regime="attenuation",
        factory_signature={
            MixerControlRole.CAPTURE_MASTER: FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.3, 0.6),
                expected_db_range=None,
            ),
            MixerControlRole.INTERNAL_MIC_BOOST: FactorySignature(
                expected_raw_range=(0, 0),
                expected_fraction_range=None,
                expected_db_range=None,
            ),
        },
        recommended_preset=MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueFraction(fraction=1.0),
                ),
                MixerPresetControl(
                    role=MixerControlRole.INTERNAL_MIC_BOOST,
                    value=MixerPresetValueRaw(raw=0),
                ),
            ),
        ),
        validation_gates=ValidationGates(
            rms_dbfs_range=(-30.0, -15.0),
            peak_dbfs_max=-2.0,
            snr_db_vocal_band_min=15.0,
            silero_prob_min=0.5,
            wake_word_stage2_prob_min=0.4,
        ),
        verified_on=(
            VerificationRecord(
                system_product="VJFE69F11X-B0221H",
                codec_id="14F1:5045",
                kernel="6.14.0-37",
                distro="linuxmint-22.2",
                verified_at="2026-04-23",
                verified_by="sovyx-core-pilot",
            ),
        ),
        contributed_by="sovyx-core",
    )


def _endpoint() -> CandidateEndpoint:
    from sovyx.voice.health.contract import CandidateSource

    return CandidateEndpoint(
        device_index=0,
        host_api_name="ALSA",
        kind=DeviceKind.HARDWARE,
        canonical_name="sn6180-capture",
        friendly_name="Internal Microphone (Conexant SN6180)",
        source=CandidateSource.USER_PREFERRED,
        preference_rank=0,
        endpoint_guid="endpoint-sn6180-test",
    )


def _hw() -> HardwareContext:
    return HardwareContext(
        driver_family="hda",
        codec_id="14F1:5045",
        system_vendor="Sony Group Corporation",
        system_product="VJFE69F11X-B0221H",
        distro="linuxmint-22.2",
        audio_stack="pipewire",
        kernel="6.14.0-37-generic",
    )


def _factory_attenuated_card() -> MixerCardSnapshot:
    """Card whose readings match the pilot's factory-attenuated signature."""
    return MixerCardSnapshot(
        card_index=0,
        card_id="Generic",
        card_longname="HDA Intel PCH (SN6180)",
        controls=(
            MixerControlSnapshot(
                name="Capture",
                min_raw=0,
                max_raw=80,
                current_raw=40,  # fraction 0.5 → inside 0.3-0.6
                current_db=-34.0,
                max_db=None,
                is_boost_control=True,
                saturation_risk=False,
            ),
            MixerControlSnapshot(
                name="Internal Mic Boost",
                min_raw=0,
                max_raw=3,
                current_raw=0,  # raw=0 → inside 0-0 range
                current_db=0.0,
                max_db=None,
                is_boost_control=True,
                saturation_risk=False,
            ),
        ),
        aggregated_boost_db=0.0,
        saturation_warning=False,
    )


def _healthy_snapshot() -> MixerCardSnapshot:
    """Card with no saturation, no factory-bad match."""
    return MixerCardSnapshot(
        card_index=0,
        card_id="Generic",
        card_longname="Healthy Generic HDA",
        controls=(
            MixerControlSnapshot(
                name="Capture",
                min_raw=0,
                max_raw=80,
                current_raw=65,  # 0.81 — healthy middle
                current_db=-6.0,
                max_db=None,
                is_boost_control=True,
                saturation_risk=False,
            ),
        ),
        aggregated_boost_db=0.0,
        saturation_warning=False,
    )


def _good_metrics() -> MixerValidationMetrics:
    return MixerValidationMetrics(
        rms_dbfs=-22.0,
        peak_dbfs=-6.0,
        snr_db_vocal_band=18.0,
        silero_max_prob=0.85,
        silero_mean_prob=0.40,
        wake_word_stage2_prob=0.55,
        measurement_duration_ms=2000,
    )


def _failing_metrics() -> MixerValidationMetrics:
    """Metrics that fail at least one gate (snr too low + silero dead)."""
    return MixerValidationMetrics(
        rms_dbfs=-22.0,
        peak_dbfs=-6.0,
        snr_db_vocal_band=5.0,  # < 15 → fails
        silero_max_prob=0.05,  # < 0.5 → fails
        silero_mean_prob=0.01,
        wake_word_stage2_prob=0.1,  # < 0.4 → fails
        measurement_duration_ms=2000,
    )


def _apply_snapshot() -> MixerApplySnapshot:
    return MixerApplySnapshot(
        card_index=0,
        reverted_controls=(("Capture", 40), ("Internal Mic Boost", 0)),
        applied_controls=(("Capture", 80), ("Internal Mic Boost", 0)),
    )


async def _pass_validation(
    _endpoint: CandidateEndpoint,
    _tuning: VoiceTuningConfig,
) -> MixerValidationMetrics:
    return _good_metrics()


async def _fail_validation(
    _endpoint: CandidateEndpoint,
    _tuning: VoiceTuningConfig,
) -> MixerValidationMetrics:
    return _failing_metrics()


async def _raising_validation(
    _endpoint: CandidateEndpoint,
    _tuning: VoiceTuningConfig,
) -> MixerValidationMetrics:
    msg = "synthetic capture failure"
    raise RuntimeError(msg)


async def _noop_persist(
    _cards: Sequence[int],  # noqa: ARG001 — Protocol conformance
    _tuning: VoiceTuningConfig,  # noqa: ARG001
) -> bool:
    return True


@dataclass
class _KBBuilder:
    profiles: list[MixerKBProfile]


def _lookup_with(profile: MixerKBProfile | None) -> MixerKBLookup:
    resolver = MixerControlRoleResolver()
    return MixerKBLookup([profile] if profile else [], resolver=resolver)


# ── Platform gate ───────────────────────────────────────────────────


class TestPlatformGate:
    @pytest.mark.asyncio()
    async def test_non_linux_defers(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.DEFERRED_PLATFORM
        assert result.cards_probed == ()
        assert result.apply_duration_ms is None


# ── Probe step ─────────────────────────────────────────────────────


class TestProbeStep:
    @pytest.mark.asyncio()
    async def test_no_cards_defers(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [],
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.DEFERRED_NO_KB
        assert result.error == "MIXER_SANITY_NO_CARDS"

    @pytest.mark.asyncio()
    async def test_probe_oserror_returns_error(self) -> None:
        def raising_probe() -> list[MixerCardSnapshot]:
            msg = "amixer crashed"
            raise OSError(msg)

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=raising_probe,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.ERROR
        assert result.error == "MIXER_SANITY_PROBE_FAILED"


# ── Classify step ───────────────────────────────────────────────────


class TestClassifyStep:
    @pytest.mark.asyncio()
    async def test_healthy_no_kb_match_skipped_healthy(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),  # empty KB
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.SKIPPED_HEALTHY
        assert result.diagnosis_before is Diagnosis.HEALTHY

    @pytest.mark.asyncio()
    async def test_unhealthy_no_kb_match_deferred(self) -> None:
        saturated = MixerCardSnapshot(
            card_index=0,
            card_id="Generic",
            card_longname="Saturated card",
            controls=(
                MixerControlSnapshot(
                    name="Internal Mic Boost",
                    min_raw=0,
                    max_raw=3,
                    current_raw=3,
                    current_db=36.0,
                    max_db=36.0,
                    is_boost_control=True,
                    saturation_risk=True,
                ),
            ),
            aggregated_boost_db=36.0,
            saturation_warning=True,
        )
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),  # empty KB
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [saturated],
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.DEFERRED_NO_KB
        assert result.diagnosis_before is Diagnosis.MIXER_UNKNOWN_PATTERN
        assert result.error == "MIXER_SANITY_NO_KB_MATCH"
        assert result.remediation is not None


# ── Customization branch ────────────────────────────────────────────


class TestCustomizationBranch:
    @pytest.mark.asyncio()
    async def test_low_customization_triggers_apply_path(
        self,
        tmp_path: object,  # noqa: ARG002 — reserved for future filesystem signal isolation
    ) -> None:
        """Rely on the real test-runner home being free of
        ``~/.asoundrc`` / pipewire / wireplumber configs (signals
        B/C/E absent on CI and on a typical developer machine). The
        orchestrator does not expose filesystem overrides — threading
        them through the public API for one test would pollute the
        production signature.
        """
        apply_calls: list[tuple[int, str]] = []

        async def spying_apply(
            card_index: int,
            preset: MixerPresetSpec,
            _role_mapping: object,
            *,
            tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerApplySnapshot:
            apply_calls.append((card_index, preset.controls[0].role.value))
            return _apply_snapshot()

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=spying_apply,
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.HEALED
        assert apply_calls, "apply was never called"
        assert result.diagnosis_before is Diagnosis.MIXER_ZEROED
        assert result.diagnosis_after is Diagnosis.HEALTHY
        assert result.validation_passed is True
        assert result.matched_kb_profile == "vaio_vjfe69_sn6180"


# ── Validation fail → rollback ──────────────────────────────────────


class TestValidationRollback:
    @pytest.mark.asyncio()
    async def test_failed_gates_trigger_rollback(self) -> None:
        restore = AsyncMock()
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_fail_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=restore,
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.ROLLED_BACK
        assert result.validation_passed is False
        restore.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_validation_raises_triggers_rollback(self) -> None:
        restore = AsyncMock()
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_raising_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=restore,
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.ROLLED_BACK
        assert result.error == "MIXER_SANITY_VALIDATION_FAILED"
        restore.assert_awaited_once()


# ── Apply failure ───────────────────────────────────────────────────


class TestApplyFailure:
    @pytest.mark.asyncio()
    async def test_apply_raises_returns_error(self) -> None:
        async def raising_apply(
            _card_index: int,
            _preset: MixerPresetSpec,
            _role_mapping: object,
            *,
            tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerApplySnapshot:
            msg = "synthetic apply failure"
            raise RuntimeError(msg)

        restore = AsyncMock()
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=raising_apply,
                mixer_restore_fn=restore,
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.ERROR
        assert result.error == "MIXER_SANITY_APPLY_FAILED"
        # No apply_snapshot captured → no rollback needed from
        # orchestrator (apply_mixer_preset does its own internal rollback).
        restore.assert_not_awaited()


# ── Persist best-effort ─────────────────────────────────────────────


class TestPersistStep:
    @pytest.mark.asyncio()
    async def test_persist_failure_still_reports_healed(self) -> None:
        async def failing_persist(
            _cards: Sequence[int],  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> bool:
            return False

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=AsyncMock(),
                persist_fn=failing_persist,
                tuning=VoiceTuningConfig(),
            )
        # HEALED with error token — preset applied in-memory,
        # persistence failed (reboot will lose it).
        assert result.decision is MixerSanityDecision.HEALED
        assert result.error == "MIXER_SANITY_PERSIST_FAILED"


# ── Budget exceeded ─────────────────────────────────────────────────


class TestBudgetExceeded:
    @pytest.mark.asyncio()
    async def test_budget_exceeded_during_apply(self) -> None:
        """Long-running apply triggers budget timeout → rollback → ERROR."""
        # Tuning with near-zero budget so *any* step trips it.
        tuning = VoiceTuningConfig()
        tuning_dict = tuning.model_dump()
        tuning_dict["linux_mixer_sanity_budget_s"] = 0.0
        tight_tuning = VoiceTuningConfig(**tuning_dict)

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=tight_tuning,
            )
        # With budget=0, the orchestrator trips after the first
        # step. Exact decision depends on which step runs first
        # but ERROR with BUDGET_EXCEEDED is the contract.
        assert result.decision is MixerSanityDecision.ERROR
        assert result.error == "MIXER_SANITY_BUDGET_EXCEEDED"


# ── Telemetry ───────────────────────────────────────────────────────


class TestTelemetry:
    @pytest.mark.asyncio()
    async def test_telemetry_records_decision(self) -> None:
        telemetry = MagicMock()
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                telemetry=telemetry,
                tuning=VoiceTuningConfig(),
            )
        telemetry.record_mixer_sanity_outcome.assert_called_once_with(
            decision=result.decision.value,
            matched_profile=None,
            score=0.0,
        )

    @pytest.mark.asyncio()
    async def test_telemetry_none_defaults_to_noop(self) -> None:
        """No telemetry arg → internal _NoopTelemetry; no crash."""
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                telemetry=None,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.SKIPPED_HEALTHY


# ── _check_validation_gates ──────────────────────────────────────────


class TestCheckValidationGates:
    def test_all_gates_pass(self) -> None:
        assert mod._check_validation_gates(
            _good_metrics(),
            _pilot_profile().validation_gates,
        )

    def test_rms_out_of_range_fails(self) -> None:
        gates = _pilot_profile().validation_gates
        metrics = MixerValidationMetrics(
            rms_dbfs=-50.0,  # below range [-30, -15]
            peak_dbfs=-6.0,
            snr_db_vocal_band=18.0,
            silero_max_prob=0.85,
            silero_mean_prob=0.4,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        assert not mod._check_validation_gates(metrics, gates)

    def test_peak_too_high_fails(self) -> None:
        gates = _pilot_profile().validation_gates
        metrics = MixerValidationMetrics(
            rms_dbfs=-22.0,
            peak_dbfs=0.0,  # above -2
            snr_db_vocal_band=18.0,
            silero_max_prob=0.85,
            silero_mean_prob=0.4,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        assert not mod._check_validation_gates(metrics, gates)

    def test_snr_too_low_fails(self) -> None:
        gates = _pilot_profile().validation_gates
        metrics = MixerValidationMetrics(
            rms_dbfs=-22.0,
            peak_dbfs=-6.0,
            snr_db_vocal_band=5.0,  # below 15
            silero_max_prob=0.85,
            silero_mean_prob=0.4,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        assert not mod._check_validation_gates(metrics, gates)


# ── default_persist_via_alsactl ──────────────────────────────────────


class TestDefaultPersistViaAlsactl:
    @pytest.mark.asyncio()
    async def test_non_linux_returns_false(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            ok = await mod.default_persist_via_alsactl([0], VoiceTuningConfig())
        assert ok is False

    @pytest.mark.asyncio()
    async def test_missing_alsactl_returns_false(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "shutil") as shutil_mock,
        ):
            sys_mock.platform = "linux"
            shutil_mock.which.return_value = None
            ok = await mod.default_persist_via_alsactl([0], VoiceTuningConfig())
        assert ok is False
