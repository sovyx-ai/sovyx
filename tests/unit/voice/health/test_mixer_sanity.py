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
from pathlib import Path
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
    mod._L25_LOCK = None
    mod._L25_LOCK_LOOP = None
    # Reset the reentrancy contextvar defensively. Each pytest test
    # function runs in its own Context, so this should be a no-op,
    # but it prevents any leak from a previous test that used
    # ``_L25_INSIDE.set(...)`` directly.
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
        """Paranoid-QA R2 HIGH #10 regression (rewritten R3 CRIT-4).

        The pre-R3 version of this test mutated tuning mid-persist
        and asserted the happy-path HEALED outcome — that didn't
        exercise the R2 HIGH #10 guard at all. This version drives
        the orchestrator's state machine directly: it seeds a
        context where validation already passed + an apply snapshot
        exists + the budget has tripped, then runs one iteration
        of the state machine and asserts the normalisation
        preserves ``validation_passed=True``. A breaking diff that
        reverts the R2 HIGH-10 guard back to unconditional
        ``validation_passed = False`` fails this test immediately.
        """
        # Construct a budget that has ALREADY expired by lying
        # about start_time_s (start in the distant past).
        tight = VoiceTuningConfig()
        tight_dict = tight.model_dump()
        tight_dict["linux_mixer_sanity_budget_s"] = 0.01
        tight = VoiceTuningConfig(**tight_dict)

        ctx = mod._OrchestratorContext(
            endpoint=_endpoint(),
            hw=_hw(),
            tuning=tight,
            start_time_s=__import__("time").monotonic() - 10.0,  # past
            mixer_probe_fn=lambda: [_factory_attenuated_card()],
            mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
            mixer_restore_fn=AsyncMock(),
            kb_lookup=_lookup_with(_pilot_profile()),
            role_resolver=MixerControlRoleResolver(),
            validation_probe_fn=_pass_validation,
            persist_fn=_noop_persist,
            telemetry=mod._NoopTelemetry(),
            telemetry_was_provided=False,
        )
        # Seed the state a persist-phase entry would have:
        ctx.apply_snapshot = _apply_snapshot()
        ctx.validation_passed = True  # R2 HIGH-10 scenario
        ctx.diagnosis_before = Diagnosis.MIXER_ZEROED

        orchestrator = mod._SanityOrchestrator(ctx)
        await orchestrator.run()

        # The budget-branch must:
        #  (a) surface as ROLLED_BACK (apply committed)
        #  (b) leave validation_passed=True (R2 HIGH-10 invariant)
        #  (c) carry the BUDGET_EXCEEDED error token
        assert ctx.decision is MixerSanityDecision.ROLLED_BACK
        assert ctx.validation_passed is True, (
            "budget-exceeded path overwrote validation_passed with "
            "False even though validation had already set it True "
            "— R2 HIGH-10 regression"
        )
        assert ctx.error_token == "MIXER_SANITY_BUDGET_EXCEEDED"


class TestCrossCascadeLock:
    """Paranoid-QA R2 HIGH #1 regression.

    A single module-level ``asyncio.Lock`` serialises every L2.5
    invocation. Two concurrent cascades for the same endpoint (or
    two endpoints with overlapping card sets) MUST execute serially
    so neither sees the other's in-flight apply.
    """

    @pytest.mark.asyncio()
    async def test_second_cascade_waits_for_first(self) -> None:
        """Serialisation proven by a critical-section counter — NOT
        wall-clock timestamps.

        Paranoid-QA R3 HIGH #11: the earlier version used
        ``time.monotonic()`` deltas + ``await aio.sleep(0.05)`` to
        prove c2 started after c1 ended. Wall-clock proofs fail
        both ways under load — they can pass when the lock is
        broken (scheduling coincidence) and fail when the lock
        works (timer resolution jitter). This counter-based
        version increments when a cascade enters the lock-protected
        region and asserts the counter NEVER exceeds 1 — catches
        any overlap regardless of wall-clock noise.
        """
        import asyncio as aio

        in_critical_section = 0
        max_observed = 0

        def make_probe() -> object:
            def _probe() -> Sequence[MixerCardSnapshot]:
                nonlocal in_critical_section, max_observed
                in_critical_section += 1
                max_observed = max(max_observed, in_critical_section)
                return [_factory_attenuated_card()]

            return _probe

        async def slow_validation(
            _endpoint: CandidateEndpoint,  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerValidationMetrics:
            # Long await INSIDE the critical section — if the lock
            # is missing, the other cascade will enter its probe
            # during this sleep and bump the counter above 1.
            await aio.sleep(0.05)
            return _good_metrics()

        async def fast_validation(
            _endpoint: CandidateEndpoint,  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerValidationMetrics:
            return _good_metrics()

        async def decrementing_persist(
            _cards: Sequence[int],  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> bool:
            nonlocal in_critical_section
            in_critical_section -= 1
            return True

        async def run(
            probe: object,
            validation: object,
        ) -> MixerSanityResult:
            with patch.object(mod, "sys") as sys_mock:
                sys_mock.platform = "linux"
                return await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(_pilot_profile()),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=validation,
                    mixer_probe_fn=probe,
                    mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                    mixer_restore_fn=AsyncMock(),
                    persist_fn=decrementing_persist,
                    tuning=VoiceTuningConfig(),
                )

        await aio.gather(
            run(make_probe(), slow_validation),
            run(make_probe(), fast_validation),
        )
        # If the lock is broken, both probes fire concurrently and
        # max_observed hits 2. The lock's job is to keep this at 1.
        assert max_observed == 1, (
            f"cross-cascade lock failed to serialise — observed "
            f"{max_observed} cascades in the critical section at once"
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


class TestHalfHealWalIntegration:
    """Paranoid-QA R2 HIGH #3 orchestrator-level regression.

    End-to-end: when ``half_heal_wal_path`` is provided, the
    orchestrator writes the WAL before _step_apply and clears it
    on every terminal transition.
    """

    @pytest.mark.asyncio()
    async def test_wal_written_before_apply_and_cleared_on_success(self, tmp_path: Path) -> None:
        from sovyx.voice.health._half_heal_recovery import load_wal

        wal_path = tmp_path / "wal.json"
        observed_wal_during_apply: list[bool] = []

        async def checking_apply(
            card_index: int,  # noqa: ARG001
            preset: MixerPresetSpec,  # noqa: ARG001
            role_mapping: object,  # noqa: ARG001
            *,
            tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> MixerApplySnapshot:
            # Observe WAL presence mid-apply — must exist.
            observed_wal_during_apply.append(wal_path.exists())
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
                mixer_apply_fn=checking_apply,
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
                half_heal_wal_path=wal_path,
            )
        assert result.decision is MixerSanityDecision.HEALED
        # WAL existed mid-apply:
        assert observed_wal_during_apply == [True]
        # WAL cleared on success:
        assert not wal_path.exists()
        # Post-condition: no stale WAL left behind for the next cascade.
        assert load_wal(wal_path) is None

    @pytest.mark.asyncio()
    async def test_wal_cleared_on_rollback(self, tmp_path: Path) -> None:
        wal_path = tmp_path / "wal.json"
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
                mixer_restore_fn=AsyncMock(),
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
                half_heal_wal_path=wal_path,
            )
        assert result.decision is MixerSanityDecision.ROLLED_BACK
        # WAL cleared even after rollback.
        assert not wal_path.exists()

    @pytest.mark.asyncio()
    async def test_existing_wal_triggers_recovery_before_cascade(self, tmp_path: Path) -> None:
        """If a prior cascade died mid-apply and left a WAL, the
        next invocation detects it, restores pre-apply state, and
        only then runs the state machine."""
        from sovyx.voice.health._half_heal_recovery import write_wal

        wal_path = tmp_path / "wal.json"
        write_wal(
            card_index=0,
            reverted_controls=(("Capture", 40),),
            path=wal_path,
        )

        restore = AsyncMock()
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            await check_and_maybe_heal(
                _endpoint(),
                _hw(),
                kb_lookup=_lookup_with(None),
                role_resolver=MixerControlRoleResolver(),
                validation_probe_fn=_pass_validation,
                mixer_probe_fn=lambda: [_healthy_snapshot()],
                mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                mixer_restore_fn=restore,
                persist_fn=_noop_persist,
                tuning=VoiceTuningConfig(),
                half_heal_wal_path=wal_path,
            )

        # Recovery ran: restore was called at boot with the WAL's
        # snapshot promoted to MixerApplySnapshot shape.
        restore.assert_awaited()
        # WAL was cleared by recovery.
        assert not wal_path.exists()

    @pytest.mark.asyncio()
    async def test_no_wal_path_no_file_created(self, tmp_path: Path) -> None:
        """Opt-out: when ``half_heal_wal_path`` is None, no WAL is
        written regardless of apply outcome."""
        marker = tmp_path / "nothing-here.json"
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
                tuning=VoiceTuningConfig(),
                half_heal_wal_path=None,
            )
        assert result.decision is MixerSanityDecision.HEALED
        assert not marker.exists()


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


class TestBuildMixerSanitySetupWalWiring:
    """Paranoid-QA R3 HIGH #14 regression.

    ``_factory_integration.run_boot_cascade_for_candidates`` threads
    ``half_heal_wal_path=default_wal_path(data_dir)`` through
    ``build_mixer_sanity_setup``. Without this test, silently
    deleting the kwarg ships production without crash recovery and
    every existing test still passes.
    """

    @pytest.mark.asyncio()
    async def test_wal_path_propagates_into_setup(self, tmp_path: Path) -> None:
        from sovyx.voice.health._mixer_sanity import build_mixer_sanity_setup
        from sovyx.voice.health.probe import probe as _probe_fn

        wal_path = tmp_path / "voice_health" / "mixer_sanity_half_heal.json"
        # Inject hw override so this test works on non-Linux too.
        setup = await build_mixer_sanity_setup(
            probe_fn=_probe_fn,
            hw=_hw(),
            kb_lookup=_lookup_with(None),
            role_resolver=MixerControlRoleResolver(),
            half_heal_wal_path=wal_path,
        )
        # On non-Linux, build_mixer_sanity_setup returns None. Skip
        # the assertion there — the point of this test is the Linux
        # production wiring.
        if setup is None:
            pytest.skip(
                "build_mixer_sanity_setup returned None "
                "(non-Linux or unknown driver family); nothing to assert"
            )
        assert setup.half_heal_wal_path == wal_path

    @pytest.mark.asyncio()
    async def test_wal_path_defaults_to_none_when_not_provided(
        self,
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        from sovyx.voice.health._mixer_sanity import build_mixer_sanity_setup
        from sovyx.voice.health.probe import probe as _probe_fn

        setup = await build_mixer_sanity_setup(
            probe_fn=_probe_fn,
            hw=_hw(),
            kb_lookup=_lookup_with(None),
            role_resolver=MixerControlRoleResolver(),
        )
        if setup is None:
            pytest.skip(
                "build_mixer_sanity_setup returned None "
                "(non-Linux or unknown driver family); nothing to assert"
            )
        assert setup.half_heal_wal_path is None

    def test_factory_integration_invokes_with_wal_path(self) -> None:
        """Paranoid-QA R3 HIGH-14 + R4 MEDIUM-5: AST-based invariant.

        The production call site in ``_factory_integration.py`` MUST
        invoke ``build_mixer_sanity_setup`` with a
        ``half_heal_wal_path`` kwarg whose value is a call to
        ``default_wal_path(...)``. R3 greps the literal string —
        fragile to any whitespace / identifier rename / line wrap.
        R4 upgrades to an AST walk that verifies the kwarg
        structure, not the exact source text.
        """
        import ast
        from pathlib import Path as _Path

        repo_root = _Path(__file__).resolve().parents[4]
        src = (
            repo_root / "src" / "sovyx" / "voice" / "health" / "_factory_integration.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)

        found_kwarg_value: ast.AST | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_mixer_sanity_setup"
            ):
                for kw in node.keywords:
                    if kw.arg == "half_heal_wal_path":
                        found_kwarg_value = kw.value
                        break
                if found_kwarg_value is not None:
                    break

        assert found_kwarg_value is not None, (
            "_factory_integration.py no longer calls "
            "build_mixer_sanity_setup with a ``half_heal_wal_path`` "
            "kwarg — production has lost crash recovery. Regression "
            "on R3 HIGH-14 invariant."
        )
        assert isinstance(found_kwarg_value, ast.Call), (
            f"half_heal_wal_path kwarg is not a Call expression: "
            f"got {type(found_kwarg_value).__name__}"
        )
        assert isinstance(found_kwarg_value.func, ast.Name), (
            f"half_heal_wal_path kwarg's callable is not a bare name: "
            f"got {type(found_kwarg_value.func).__name__}"
        )
        assert found_kwarg_value.func.id == "default_wal_path", (
            f"half_heal_wal_path kwarg calls {found_kwarg_value.func.id}(), not default_wal_path"
        )


class TestValidationMetricsLogExplicitFields:
    """Paranoid-QA R3 HIGH #12 regression.

    R2 HIGH #6 replaced ``logger.info(..., metrics=metrics)`` with
    per-field kwargs so a future field addition (raw buffer sample,
    device name, etc.) wouldn't silently leak through repr. That
    fix had no test — a refactor back to the dataclass-repr form
    would pass every existing test. This test patches the logger
    and asserts the expected fields appear individually.
    """

    @pytest.mark.asyncio()
    async def test_validation_gates_failed_log_has_explicit_fields(
        self,
    ) -> None:
        captured_calls: list[tuple[str, dict[str, object]]] = []

        def fake_info(event: str, **kwargs: object) -> None:
            captured_calls.append((event, dict(kwargs)))

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            with patch.object(mod.logger, "info", side_effect=fake_info):
                await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(_pilot_profile()),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=_fail_validation,
                    mixer_probe_fn=lambda: [_factory_attenuated_card()],
                    mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                    mixer_restore_fn=AsyncMock(),
                    persist_fn=_noop_persist,
                    tuning=VoiceTuningConfig(),
                )
        # Find the validation-failed log event.
        failed_event = next(
            (kw for ev, kw in captured_calls if ev == "mixer_sanity_validation_gates_failed"),
            None,
        )
        assert failed_event is not None, (
            "validation-gates-failed log event did not fire — the test "
            "premise is broken (did validation actually fail?)"
        )
        # Must NOT carry a dataclass blob named ``metrics``.
        assert "metrics" not in failed_event, (
            "log event carries ``metrics=<dataclass>`` — R2 HIGH #6 "
            "invariant broken, please pass explicit per-field kwargs"
        )
        # Must carry every explicit field we care about.
        for expected in (
            "rms_dbfs",
            "peak_dbfs",
            "snr_db_vocal_band",
            "silero_max_prob",
            "silero_mean_prob",
            "wake_word_stage2_prob",
            "measurement_duration_ms",
        ):
            assert expected in failed_event, (
                f"explicit field {expected!r} missing from "
                f"mixer_sanity_validation_gates_failed kwargs"
            )


class TestRollbackFailureSurfacing:
    """Paranoid-QA R3 CRIT-3 regression.

    When the injected ``mixer_restore_fn`` raises, the orchestrator
    logged-and-swallowed but ALSO stamped ``rollback_performed=True``,
    causing the final result to advertise ``decision=ROLLED_BACK,
    rollback_snapshot=X`` as if restore succeeded — while the mixer
    remained stuck in the failing-validation applied state.
    ``rollback_failed`` now records the truth and downgrades the
    result + preserves the WAL for cross-boot retry.
    """

    @pytest.mark.asyncio()
    async def test_restore_raise_surfaces_as_error_with_token(
        self,
    ) -> None:
        restore = AsyncMock(
            side_effect=RuntimeError("amixer binary vanished mid-rollback"),
        )
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
        # Decision MUST NOT be ROLLED_BACK — the mixer is NOT
        # restored; downgrading to ERROR lets the dashboard render
        # the failure distinctly.
        assert result.decision is MixerSanityDecision.ERROR
        assert result.error is not None
        assert "ROLLBACK_FAILED" in result.error
        # The original upstream trigger should also be discoverable
        # in the error token (VALIDATION_FAILED was the validate
        # step outcome before rollback was attempted).
        assert "VALIDATION_FAILED" in result.error
        # And restore was attempted (i.e., not short-circuited
        # before the restore_fn was called).
        restore.assert_awaited()

    @pytest.mark.asyncio()
    async def test_restore_raise_preserves_wal_for_next_boot(self, tmp_path: Path) -> None:
        """If restore raised, the WAL MUST remain on disk so the
        next cascade's ``recover_if_present`` can retry via a fresh
        ``restore_fn``. Without this, a stuck mixer never self-heals."""
        wal_path = tmp_path / "wal.json"
        restore = AsyncMock(side_effect=RuntimeError("synthetic"))
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            await check_and_maybe_heal(
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
                half_heal_wal_path=wal_path,
            )
        # WAL persists for next-boot recovery retry.
        assert wal_path.exists(), (
            "rollback_failed path must NOT clear the WAL — the next "
            "cascade needs it to retry the restore"
        )


class TestPersistExceptionNarrowing:
    """Paranoid-QA R3 CRIT-2 regression.

    ``_step_persist`` previously used ``except BaseException`` which
    contradicted the R2 HIGH #1 narrowing on ``rollback_if_needed``.
    A ``CancelledError`` delivered while ``persist_fn`` was awaiting
    would be swallowed, ``persist_succeeded=False`` set, and the
    state machine would march on to HEALED. Exception-only ensures
    CancelledError / KeyboardInterrupt / SystemExit propagate.
    """

    @pytest.mark.asyncio()
    async def test_persist_cancel_propagates(self) -> None:
        import asyncio as aio

        async def cancelling_persist(
            _cards: Sequence[int],  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> bool:
            # Simulate a cancel fired while systemctl start was
            # mid-subprocess.
            raise aio.CancelledError

        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "linux"
            with pytest.raises(aio.CancelledError):
                await check_and_maybe_heal(
                    _endpoint(),
                    _hw(),
                    kb_lookup=_lookup_with(_pilot_profile()),
                    role_resolver=MixerControlRoleResolver(),
                    validation_probe_fn=_pass_validation,
                    mixer_probe_fn=lambda: [_factory_attenuated_card()],
                    mixer_apply_fn=AsyncMock(return_value=_apply_snapshot()),
                    mixer_restore_fn=AsyncMock(),
                    persist_fn=cancelling_persist,
                    tuning=VoiceTuningConfig(),
                )

    @pytest.mark.asyncio()
    async def test_persist_exception_still_surfaces_healed(self) -> None:
        """Regular Exception (not BaseException) is still swallowed
        and results in HEALED with MIXER_SANITY_PERSIST_FAILED — the
        R2 semantics must survive the R3 narrowing."""

        async def raising_persist(
            _cards: Sequence[int],  # noqa: ARG001
            _tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> bool:
            raise RuntimeError("systemctl is not available")

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
                persist_fn=raising_persist,
                tuning=VoiceTuningConfig(),
            )
        assert result.decision is MixerSanityDecision.HEALED
        assert result.error == "MIXER_SANITY_PERSIST_FAILED"


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
