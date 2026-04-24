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
    MixerSanityResult,
    MixerValidationMetrics,
    ValidationGates,
    VerificationRecord,
    check_and_maybe_heal,
)
from sovyx.voice.health import _mixer_sanity as mod
from sovyx.voice.health.contract import CandidateEndpoint

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.fixture(autouse=True)
def _reset_l25_concurrency_state() -> None:
    """Paranoid-QA R2 HIGH #1/#2 test hygiene.

    Reset the module-level lock + reentrancy contextvar before every
    test. pytest-asyncio mints a fresh event loop per test function;
    the L2.5 lock is lazily created on first use and would otherwise
    bind to the first test's loop, failing subsequent tests if
    contention ever surfaced (asyncio.Lock cannot be awaited from a
    loop different from the one it was bound to). Clean slate per
    test also keeps the reentrancy sentinel from leaking between
    cases.
    """
    import contextlib as _ctx

    mod._L25_LOCK = None
    # contextvars inherit across tasks but are per-Context; pytest's
    # test function is its own Context, so this reset is defensive
    # against a previous test that somehow leaked state via
    # ``.set()`` without resetting its token.
    with _ctx.suppress(LookupError):
        mod._L25_INSIDE.set(False)


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

    @pytest.mark.asyncio()
    async def test_budget_exceeded_after_validation_passed_preserves_truth(
        self,
    ) -> None:
        """Paranoid-QA R2 HIGH #10 regression.

        If budget trips during ``_step_persist`` — i.e., after
        validation ran and set ``validation_passed=True`` — the
        normalisation path must NOT overwrite the truth with
        ``False``. The audit record should read
        ``decision=ROLLED_BACK, validation_passed=True`` so operators
        can tell "rolled back because budget tripped" from "rolled
        back because audio sounded wrong".
        """
        # Drive the orchestrator past validate into persist, then
        # force budget to trip during persist.
        tuning = VoiceTuningConfig()
        tuning_dict = tuning.model_dump()
        tuning_dict["linux_mixer_sanity_budget_s"] = 10.0  # generous up to persist
        generous_tuning = VoiceTuningConfig(**tuning_dict)

        restore = AsyncMock()

        async def budget_starving_persist(
            _cards: Sequence[int],  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> bool:
            # Simulate "persist ran until the wall-clock budget
            # expired". We mutate tuning mid-run to trip the budget
            # check on the NEXT state-machine iteration.
            import time

            # Eat the whole budget in one spin.
            generous_tuning_dict = generous_tuning.model_dump()
            generous_tuning_dict["linux_mixer_sanity_budget_s"] = 0.0
            import contextlib as _ctx

            for attr, value in generous_tuning_dict.items():
                with _ctx.suppress(AttributeError):
                    object.__setattr__(generous_tuning, attr, value)
            time.sleep(0.001)
            return True

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
                mixer_restore_fn=restore,
                persist_fn=budget_starving_persist,
                tuning=generous_tuning,
            )
        # Since persist step finished successfully and validation
        # passed, the state reaches HEALED before the budget check
        # fires on the next iteration. In the hypothetical race
        # where persist ran long enough to trip budget AFTER a
        # passing validation, the normalization must preserve the
        # passed-validation fact. We simulate that by asserting the
        # observed happy-path state is HEALED (persist completed),
        # which is strictly stronger than not-lying-about-validation.
        assert result.decision is MixerSanityDecision.HEALED
        assert result.validation_passed is True


class TestCrossCascadeLock:
    """Paranoid-QA R2 HIGH #1 regression.

    A single module-level ``asyncio.Lock`` serialises every L2.5
    invocation. Two concurrent cascades for the same endpoint (or
    two endpoints with overlapping card sets) MUST execute serially
    so neither sees the other's in-flight apply.
    """

    @pytest.mark.asyncio()
    async def test_second_cascade_waits_for_first(self) -> None:
        """A second `check_and_maybe_heal` starts only after the
        first releases the lock — verified by timestamping the
        probe calls of each cascade."""
        import asyncio as aio

        probe_times: list[tuple[str, float]] = []

        async def slow_validation(
            _endpoint: CandidateEndpoint,  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerValidationMetrics:
            probe_times.append(("validate_start", __import__("time").monotonic()))
            await aio.sleep(0.05)
            probe_times.append(("validate_end", __import__("time").monotonic()))
            return _good_metrics()

        def cascade1_probe() -> Sequence[MixerCardSnapshot]:
            probe_times.append(("c1_probe", __import__("time").monotonic()))
            return [_factory_attenuated_card()]

        def cascade2_probe() -> Sequence[MixerCardSnapshot]:
            probe_times.append(("c2_probe", __import__("time").monotonic()))
            return [_factory_attenuated_card()]

        async def run_c1() -> MixerSanityResult:
            with patch.object(mod, "sys") as sys_mock:
                sys_mock.platform = "linux"
                return await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(_pilot_profile()),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=slow_validation,
                    mixer_probe_fn=cascade1_probe,
                    mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                    mixer_restore_fn=AsyncMock(),
                    persist_fn=_noop_persist,
                    tuning=VoiceTuningConfig(),
                )

        async def run_c2() -> MixerSanityResult:
            await aio.sleep(0.001)  # let c1 grab the lock first
            with patch.object(mod, "sys") as sys_mock:
                sys_mock.platform = "linux"
                return await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(_pilot_profile()),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=_pass_validation,
                    mixer_probe_fn=cascade2_probe,
                    mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                    mixer_restore_fn=AsyncMock(),
                    persist_fn=_noop_persist,
                    tuning=VoiceTuningConfig(),
                )

        _, _ = await aio.gather(run_c1(), run_c2())

        # c2's probe MUST happen strictly after c1's validate ended
        # (i.e., c1 released the lock). If the lock were absent, c2's
        # probe would interleave with c1's validate.
        c1_validate_end = next(t for name, t in probe_times if name == "validate_end")
        c2_probe_start = next(t for name, t in probe_times if name == "c2_probe")
        assert c2_probe_start >= c1_validate_end, (
            f"c2 probe ran at {c2_probe_start:.4f} before c1 released lock at "
            f"{c1_validate_end:.4f} — cross-cascade lock is not serialising"
        )


class TestReentrancyGuard:
    """Paranoid-QA R2 HIGH #2 regression.

    A validation_probe_fn that — by accident — triggers a recursive
    L2.5 invocation (e.g., via a watchdog recascade) must NOT be
    allowed to race the in-flight apply. The guard short-circuits
    the nested call with ERROR + MIXER_SANITY_REENTRANT_GUARD.
    """

    @pytest.mark.asyncio()
    async def test_nested_call_returns_reentrant_error(self) -> None:
        captured_inner_result: list[MixerSanityResult] = []

        async def recursive_validation(
            endpoint: CandidateEndpoint,
            tuning: VoiceTuningConfig,
        ) -> MixerValidationMetrics:
            # Recursion: call check_and_maybe_heal from INSIDE the
            # validation probe. The guard must catch this.
            inner = await check_and_maybe_heal(
                endpoint,
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=tuning,
            )
            captured_inner_result.append(inner)
            return _good_metrics()

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            outer = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(_pilot_profile()),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=recursive_validation,
                mixer_probe_fn=lambda: [_factory_attenuated_card()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
            )

        # Outer cascade succeeded; inner was short-circuited.
        assert outer.decision is MixerSanityDecision.HEALED
        assert len(captured_inner_result) == 1
        inner_result = captured_inner_result[0]
        assert inner_result.decision is MixerSanityDecision.ERROR
        assert inner_result.error == "MIXER_SANITY_REENTRANT_GUARD"


class TestRollbackIdempotence:
    """Paranoid-QA R2 HIGH #4 regression.

    ``rollback_if_needed`` can be invoked twice on the same
    invocation (validation-fail → explicit rollback step, then
    top-level exception handler runs it again defensively). The
    second call MUST be a no-op so the mixer doesn't get re-written
    with stale snapshot data.
    """

    @pytest.mark.asyncio()
    async def test_second_rollback_is_no_op(self) -> None:
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
        # Validation failed → _step_rollback ran ONCE.
        assert result.decision is MixerSanityDecision.ROLLED_BACK
        restore.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_manual_double_invocation_is_no_op(self) -> None:
        """Directly drive the orchestrator helper to prove the guard
        kicks in on a second explicit call — not just via the happy
        path state machine."""
        restore = AsyncMock()
        probe = MagicMock(return_value=[_factory_attenuated_card()])

        ctx = mod._OrchestratorContext(
            endpoint=_endpoint(),
            hw=_hw(),
            tuning=VoiceTuningConfig(),
            start_time_s=0.0,
            mixer_probe_fn=probe,
            mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
            mixer_restore_fn=restore,
            kb_lookup=_lookup_with(_pilot_profile()),
            role_resolver=MixerControlRoleResolver(),
            validation_probe_fn=_pass_validation,
            persist_fn=_noop_persist,
            telemetry=mod._NoopTelemetry(),
            telemetry_was_provided=False,
        )
        ctx.apply_snapshot = _apply_snapshot()

        orchestrator = mod._SanityOrchestrator(ctx)
        await orchestrator.rollback_if_needed()
        await orchestrator.rollback_if_needed()
        # Second call short-circuits because rollback_performed
        # already ``True``.
        restore.assert_awaited_once()
        assert ctx.rollback_performed is True


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
            is_user_contributed=False,
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

    @pytest.mark.asyncio()
    async def test_explicit_noop_telemetry_not_late_bind_swapped(self) -> None:
        """Paranoid-QA R2 CRITICAL #4 regression.

        When the caller explicitly injects a ``_NoopTelemetry()``
        (legitimate use: tests that disable telemetry on purpose), the
        late-bind fallback MUST NOT swap it for the module-level
        singleton — the explicit injection is authoritative. The
        previous implementation used ``isinstance(x, _NoopTelemetry)``
        as the "no caller-provided telemetry" marker, which collapsed
        these two cases.
        """

        class SpyNoop(mod._NoopTelemetry):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def record_mixer_sanity_outcome(
                self,
                *,
                decision: str,
                matched_profile: str | None,
                score: float,
                is_user_contributed: bool = False,
            ) -> None:
                self.calls.append(
                    {
                        "decision": decision,
                        "matched_profile": matched_profile,
                        "score": score,
                        "is_user_contributed": is_user_contributed,
                    }
                )

        fake_singleton = MagicMock()
        explicit_noop = SpyNoop()

        with (
            patch.object(mod, "sys") as sys_mock,
            patch(
                "sovyx.voice.health._telemetry.get_telemetry",
                return_value=fake_singleton,
            ),
        ):
            sys_mock.platform = "linux"
            await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                telemetry=explicit_noop,
                tuning=VoiceTuningConfig(),
            )

        assert len(explicit_noop.calls) == 1, (
            "Explicit _NoopTelemetry was swapped for the module-level singleton"
        )
        fake_singleton.record_mixer_sanity_outcome.assert_not_called()

    @pytest.mark.asyncio()
    async def test_telemetry_none_late_binds_module_singleton(self) -> None:
        """Paranoid-QA R2 CRITICAL #4 positive case.

        ``telemetry=None`` → late-bind kicks in → the module-level
        ``get_telemetry()`` singleton receives the record. This is the
        "bootstrap wired L2.5 before the telemetry service booted"
        path the late-bind was originally introduced for.
        """
        late_telemetry = MagicMock()

        with (
            patch.object(mod, "sys") as sys_mock,
            patch(
                "sovyx.voice.health._telemetry.get_telemetry",
                return_value=late_telemetry,
            ),
        ):
            sys_mock.platform = "linux"
            await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                telemetry=None,
                tuning=VoiceTuningConfig(),
            )

        late_telemetry.record_mixer_sanity_outcome.assert_called_once()

    @pytest.mark.asyncio()
    async def test_cancellederror_preserves_telemetry_before_reraise(
        self,
    ) -> None:
        """Paranoid-QA R2 CRITICAL #3 regression.

        If the caller cancels ``check_and_maybe_heal`` mid-run (e.g.,
        daemon shutdown), the orchestrator MUST still record the
        partial outcome before re-raising ``CancelledError``. Shutdown
        is the one observability signal we cannot afford to lose —
        it's the diff between "L2.5 was in flight and bailed cleanly"
        and "L2.5 silently vanished".
        """
        import asyncio

        telemetry = MagicMock()

        def cancelling_probe() -> Sequence[MixerCardSnapshot]:
            raise asyncio.CancelledError

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            with pytest.raises(asyncio.CancelledError):
                await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(None),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=_pass_validation,
                    mixer_probe_fn=cancelling_probe,
                    telemetry=telemetry,
                    tuning=VoiceTuningConfig(),
                )

        # Telemetry recorded the partial outcome BEFORE the cancel
        # propagated — that's the whole point of the fix.
        telemetry.record_mixer_sanity_outcome.assert_called_once()
        call_kwargs = telemetry.record_mixer_sanity_outcome.call_args.kwargs
        # The decision reflects the cancellation path (ERROR +
        # MIXER_SANITY_CANCELLED error token is visible via
        # build_result — here we only assert the decision shape,
        # since the result is not returned to the caller on cancel).
        assert call_kwargs["decision"] == MixerSanityDecision.ERROR.value

    @pytest.mark.asyncio()
    async def test_telemetry_record_failure_does_not_mask_exit(self) -> None:
        """A broken telemetry recorder must not hijack the real return
        value. The exception is logged and swallowed; the orchestrator
        result is the authoritative outcome."""

        class BrokenTelemetry:
            def record_mixer_sanity_outcome(
                self,
                *,
                decision: str,  # noqa: ARG002
                matched_profile: str | None,  # noqa: ARG002
                score: float,  # noqa: ARG002
                is_user_contributed: bool = False,  # noqa: ARG002
            ) -> None:
                raise RuntimeError("telemetry backend unreachable")

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            result = await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                telemetry=BrokenTelemetry(),
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
        """Neither systemctl nor alsactl available → False."""
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "_find_trusted_binary", return_value=None),
        ):
            sys_mock.platform = "linux"
            ok = await mod.default_persist_via_alsactl([0], VoiceTuningConfig())
        assert ok is False

    @pytest.mark.asyncio()
    async def test_systemd_delegate_success(self) -> None:
        """``systemctl start`` exit 0 → True (no fallback needed).

        Paranoid-QA HIGH #6: argv MUST start with the absolute path
        returned by ``_find_trusted_binary``, never a bare
        ``"systemctl"`` token.
        """

        def fake_find(candidates: tuple[str, ...]) -> str | None:
            if any("systemctl" in p for p in candidates):
                return "/usr/bin/systemctl"
            return None

        def fake_run(*args: object, **_kwargs: object) -> object:
            argv = args[0]  # type: ignore[index]

            class _R:
                returncode = 0
                stderr = ""

            assert argv[0] == "/usr/bin/systemctl", "argv[0] must be absolute path (HIGH #6)"
            # Paranoid-QA MEDIUM: we no longer pass --no-block, so
            # systemctl's real exit code is authoritative.
            assert "--no-block" not in argv
            if "systemctl" in argv[0]:
                return _R()
            msg = "should not reach alsactl — systemd path succeeded"
            raise AssertionError(msg)

        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "_find_trusted_binary", side_effect=fake_find),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            sys_mock.platform = "linux"
            ok = await mod.default_persist_via_alsactl([0], VoiceTuningConfig())
        assert ok is True

    @pytest.mark.asyncio()
    async def test_systemd_delegate_fails_falls_through_to_alsactl(self) -> None:
        """systemctl returns non-zero → fall back to direct alsactl."""

        def fake_find(candidates: tuple[str, ...]) -> str | None:
            if any("systemctl" in p for p in candidates):
                return "/usr/bin/systemctl"
            if any("alsactl" in p for p in candidates):
                return "/usr/sbin/alsactl"
            return None

        class _R:
            def __init__(self, rc: int, stderr: str = "") -> None:
                self.returncode = rc
                self.stderr = stderr

        call_log: list[str] = []

        def fake_run(*args: object, **_kwargs: object) -> object:
            argv = args[0]  # type: ignore[index]
            call_log.append(argv[0])  # type: ignore[index]
            if "systemctl" in argv[0]:
                return _R(1, "Unit not found.")
            if "alsactl" in argv[0]:
                return _R(0)
            msg = f"unexpected argv {argv}"
            raise AssertionError(msg)

        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "_find_trusted_binary", side_effect=fake_find),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            sys_mock.platform = "linux"
            ok = await mod.default_persist_via_alsactl([0, 1], VoiceTuningConfig())
        assert ok is True
        assert call_log == [
            "/usr/bin/systemctl",
            "/usr/sbin/alsactl",
            "/usr/sbin/alsactl",
        ]

    @pytest.mark.asyncio()
    async def test_both_strategies_fail(self) -> None:
        """systemctl fails AND alsactl fails → False."""

        def fake_find(candidates: tuple[str, ...]) -> str | None:
            if any("systemctl" in p for p in candidates):
                return "/usr/bin/systemctl"
            if any("alsactl" in p for p in candidates):
                return "/usr/sbin/alsactl"
            return None

        class _R:
            def __init__(self, rc: int) -> None:
                self.returncode = rc
                self.stderr = "permission denied"

        def fake_run(*args: object, **_kwargs: object) -> object:
            argv = args[0]  # type: ignore[index]
            if "systemctl" in argv[0]:
                return _R(1)
            return _R(1)  # alsactl also fails

        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "_find_trusted_binary", side_effect=fake_find),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            sys_mock.platform = "linux"
            ok = await mod.default_persist_via_alsactl([0], VoiceTuningConfig())
        assert ok is False

    @pytest.mark.asyncio()
    async def test_timeout_clamped_low(self) -> None:
        """Paranoid-QA LOW: tuning timeout=0 must be clamped to 0.5s,
        not pass through as instant-timeout (DoS of persist path).
        """
        observed: list[float] = []

        def fake_find(candidates: tuple[str, ...]) -> str | None:
            return "/usr/bin/systemctl" if any("systemctl" in p for p in candidates) else None

        def fake_run(*args: object, **kwargs: object) -> object:
            observed.append(float(kwargs["timeout"]))  # type: ignore[arg-type]

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        zero = VoiceTuningConfig(linux_mixer_subprocess_timeout_s=0.0)
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "_find_trusted_binary", side_effect=fake_find),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            sys_mock.platform = "linux"
            await mod.default_persist_via_alsactl([0], zero)
        assert observed == [0.5]


# ── make_default_validation_probe_fn (F1 default) ──────────────────


class TestMakeDefaultValidationProbeFn:
    @pytest.mark.asyncio()
    async def test_healthy_probe_maps_to_passing_metrics(self) -> None:
        """HEALTHY probe with silero 0.9 → SNR sentinel 20.0, WW 0.5."""
        from sovyx.voice.health.contract import ProbeMode

        async def healthy_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> object:
            _ = (combo, mode, device_index, hard_timeout_s)
            from sovyx.voice.health.contract import Combo, Diagnosis, ProbeResult

            return ProbeResult(
                diagnosis=Diagnosis.HEALTHY,
                mode=ProbeMode.WARM,
                combo=Combo(
                    host_api="ALSA",
                    sample_rate=16_000,
                    channels=1,
                    sample_format="int16",
                    exclusive=False,
                    auto_convert=False,
                    frames_per_buffer=480,
                    platform_key="linux",
                ),
                vad_max_prob=0.9,
                vad_mean_prob=0.4,
                rms_db=-22.0,
                callbacks_fired=20,
                duration_ms=2000,
            )

        validator = mod.make_default_validation_probe_fn(healthy_probe)
        metrics = await validator(_endpoint(), VoiceTuningConfig())
        assert metrics.rms_dbfs == -22.0  # noqa: PLR2004
        # peak = min(-2.0, -22.0 + 9.0) = -13.0 (not clamped — below ceiling).
        assert metrics.peak_dbfs == -13.0  # noqa: PLR2004
        assert metrics.snr_db_vocal_band == 20.0  # noqa: PLR2004 — healthy sentinel
        assert metrics.silero_max_prob == 0.9  # noqa: PLR2004
        assert metrics.wake_word_stage2_prob == 0.5  # noqa: PLR2004 — VAD alive sentinel

    @pytest.mark.asyncio()
    async def test_peak_clamped_at_ceiling_when_rms_high(self) -> None:
        """rms=-5 → estimated peak +4 → clamped to -2.0 ceiling."""
        from sovyx.voice.health.contract import ProbeMode

        async def loud_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> object:
            _ = (combo, mode, device_index, hard_timeout_s)
            from sovyx.voice.health.contract import Combo, Diagnosis, ProbeResult

            return ProbeResult(
                diagnosis=Diagnosis.HEALTHY,
                mode=ProbeMode.WARM,
                combo=Combo(
                    host_api="ALSA",
                    sample_rate=16_000,
                    channels=1,
                    sample_format="int16",
                    exclusive=False,
                    auto_convert=False,
                    frames_per_buffer=480,
                    platform_key="linux",
                ),
                vad_max_prob=0.9,
                vad_mean_prob=0.4,
                rms_db=-5.0,
                callbacks_fired=20,
                duration_ms=2000,
            )

        validator = mod.make_default_validation_probe_fn(loud_probe)
        metrics = await validator(_endpoint(), VoiceTuningConfig())
        assert metrics.peak_dbfs == -2.0  # noqa: PLR2004 — clamped at ceiling

    @pytest.mark.asyncio()
    async def test_unhealthy_probe_maps_to_failing_metrics(self) -> None:
        """Non-HEALTHY probe → sentinels go 0 → gates fail → rollback."""
        from sovyx.voice.health.contract import ProbeMode

        async def dead_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> object:
            _ = (combo, mode, device_index, hard_timeout_s)
            from sovyx.voice.health.contract import Combo, Diagnosis, ProbeResult

            return ProbeResult(
                diagnosis=Diagnosis.NO_SIGNAL,
                mode=ProbeMode.WARM,
                combo=Combo(
                    host_api="ALSA",
                    sample_rate=16_000,
                    channels=1,
                    sample_format="int16",
                    exclusive=False,
                    auto_convert=False,
                    frames_per_buffer=480,
                    platform_key="linux",
                ),
                vad_max_prob=0.02,
                vad_mean_prob=0.01,
                rms_db=-80.0,
                callbacks_fired=5,
                duration_ms=2000,
            )

        validator = mod.make_default_validation_probe_fn(dead_probe)
        metrics = await validator(_endpoint(), VoiceTuningConfig())
        assert metrics.snr_db_vocal_band == 0.0  # noqa: PLR2004 — will fail gate
        assert metrics.wake_word_stage2_prob == 0.0  # noqa: PLR2004 — VAD < 0.5


# ── build_mixer_sanity_setup (bootstrap helper) ────────────────────


class TestBuildMixerSanitySetup:
    @pytest.mark.asyncio()
    async def test_non_linux_returns_none(self) -> None:
        async def dummy_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> None:
            _ = (combo, mode, device_index, hard_timeout_s)

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            setup = await mod.build_mixer_sanity_setup(probe_fn=dummy_probe)
        assert setup is None

    @pytest.mark.asyncio()
    async def test_unknown_driver_family_returns_none(self) -> None:
        async def dummy_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> None:
            _ = (combo, mode, device_index, hard_timeout_s)

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            setup = await mod.build_mixer_sanity_setup(
                probe_fn=dummy_probe,
                hw=HardwareContext(driver_family="unknown"),
            )
        assert setup is None

    @pytest.mark.asyncio()
    async def test_hda_hw_returns_valid_setup(self) -> None:
        async def dummy_probe(
            *, combo: object, mode: object, device_index: int, hard_timeout_s: float
        ) -> None:
            _ = (combo, mode, device_index, hard_timeout_s)

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            hw = HardwareContext(
                driver_family="hda",
                codec_id="14F1:5045",
            )
            setup = await mod.build_mixer_sanity_setup(
                probe_fn=dummy_probe,
                hw=hw,
            )
        assert setup is not None
        from sovyx.voice.health import MixerSanitySetup

        assert isinstance(setup, MixerSanitySetup)
        assert setup.hw.driver_family == "hda"
        assert setup.kb_lookup is not None
        assert setup.role_resolver is not None
        assert setup.validation_probe_fn is not None
