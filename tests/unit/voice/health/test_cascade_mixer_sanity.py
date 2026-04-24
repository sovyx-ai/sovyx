"""Tests for L2.5 integration into cascade.run_cascade (F1.F).

Focus: confirms that

* ``mixer_sanity=None`` (default) preserves pre-L2.5 behaviour byte-
  for-byte — zero regression for every existing caller.
* A provided ``MixerSanitySetup`` on Linux triggers
  ``check_and_maybe_heal`` between the ComboStore fast-path and the
  platform cascade walk.
* Non-Linux platforms skip the L2.5 call even when ``mixer_sanity``
  is provided (F1 scope — F3 adds cross-platform parity).
* L2.5 failures never abort the cascade (invariant P6 at the
  integration boundary).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health import (
    HardwareContext,
    MixerControlRoleResolver,
    MixerKBLookup,
    MixerSanityDecision,
    MixerSanityResult,
    MixerSanitySetup,
)
from sovyx.voice.health import cascade as cascade_mod
from sovyx.voice.health.cascade import run_cascade
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from sovyx.engine.config import VoiceTuningConfig


def _healthy_probe_result(combo: Combo) -> ProbeResult:
    return ProbeResult(
        diagnosis=Diagnosis.HEALTHY,
        mode=ProbeMode.COLD,
        combo=combo,
        vad_max_prob=None,
        vad_mean_prob=None,
        rms_db=-25.0,
        callbacks_fired=10,
        duration_ms=300,
    )


async def _passing_probe(
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,  # noqa: ARG001
    hard_timeout_s: float,  # noqa: ARG001
) -> ProbeResult:
    """Always-HEALTHY probe — cascade exits on first attempt."""
    return ProbeResult(
        diagnosis=Diagnosis.HEALTHY,
        mode=mode,
        combo=combo,
        vad_max_prob=0.9 if mode == ProbeMode.WARM else None,
        vad_mean_prob=0.4 if mode == ProbeMode.WARM else None,
        rms_db=-22.0,
        callbacks_fired=20,
        duration_ms=1500,
    )


def _sanity_setup(
    *,
    validation_metrics_pass: bool = True,
) -> MixerSanitySetup:
    """Build a minimal MixerSanitySetup for cascade integration tests."""
    from sovyx.voice.health import MixerValidationMetrics

    async def fake_validation(
        _endpoint: object,
        _tuning: VoiceTuningConfig,
    ) -> MixerValidationMetrics:
        return MixerValidationMetrics(
            rms_dbfs=-22.0 if validation_metrics_pass else 0.0,
            peak_dbfs=-6.0,
            snr_db_vocal_band=18.0 if validation_metrics_pass else 0.0,
            silero_max_prob=0.85 if validation_metrics_pass else 0.01,
            silero_mean_prob=0.4,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )

    hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
    resolver = MixerControlRoleResolver()
    lookup = MixerKBLookup([], resolver=resolver)  # empty KB → no match
    return MixerSanitySetup(
        hw=hw,
        kb_lookup=lookup,
        role_resolver=resolver,
        validation_probe_fn=fake_validation,
    )


class TestMixerSanityOptIn:
    @pytest.mark.asyncio()
    async def test_none_preserves_pre_l25_behaviour(self) -> None:
        """mixer_sanity=None → L2.5 never touched, cascade works as before."""
        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=AsyncMock(),
        ) as spy:
            result = await run_cascade(
                endpoint_guid="endpoint-test",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="win32",
                probe_fn=_passing_probe,
                mixer_sanity=None,
            )
        spy.assert_not_awaited()
        assert result.source in {"cascade", "store", "pinned"}

    @pytest.mark.asyncio()
    async def test_non_linux_platform_skips_l25(self) -> None:
        """Even with mixer_sanity set, Windows/macOS doesn't invoke L2.5."""
        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=AsyncMock(),
        ) as spy:
            await run_cascade(
                endpoint_guid="endpoint-test",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="win32",
                probe_fn=_passing_probe,
                mixer_sanity=_sanity_setup(),
            )
        spy.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_linux_invokes_l25(self) -> None:
        """platform_key=='linux' + setup → _run_mixer_sanity awaited once."""
        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=AsyncMock(),
        ) as spy:
            await run_cascade(
                endpoint_guid="endpoint-test",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="linux",
                probe_fn=_passing_probe,
                mixer_sanity=_sanity_setup(),
            )
        spy.assert_awaited_once()


class TestMixerSanityCallSite:
    @pytest.mark.asyncio()
    async def test_call_args_routed_correctly(self) -> None:
        """_run_mixer_sanity receives the endpoint metadata + setup."""
        spy = AsyncMock()
        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=spy,
        ):
            await run_cascade(
                endpoint_guid="endpoint-xyz",
                device_index=3,
                device_friendly_name="Test Mic",
                mode=ProbeMode.COLD,
                platform_key="linux",
                probe_fn=_passing_probe,
                mixer_sanity=_sanity_setup(),
            )
        spy.assert_awaited_once()
        kwargs = spy.await_args.kwargs
        assert kwargs["endpoint_guid"] == "endpoint-xyz"
        assert kwargs["device_index"] == 3  # noqa: PLR2004
        assert kwargs["device_friendly_name"] == "Test Mic"
        assert isinstance(kwargs["mixer_sanity"], MixerSanitySetup)

    @pytest.mark.asyncio()
    async def test_l25_error_does_not_abort_cascade(self) -> None:
        """Any BaseException from L2.5 is swallowed; cascade proceeds."""

        async def raising_sanity(**_kwargs: object) -> None:
            msg = "synthetic L2.5 explosion"
            raise RuntimeError(msg)

        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=raising_sanity,
        ):
            # The cascade should still complete without raising.
            # _run_mixer_sanity swallows errors internally; we test
            # that here by running against a healthy probe so the
            # cascade reaches its platform walk and returns.
            result = await run_cascade(
                endpoint_guid="endpoint-test",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="linux",
                probe_fn=_passing_probe,
                mixer_sanity=_sanity_setup(),
            )
        # With a healthy probe the cascade returns successfully.
        assert result.winning_combo is not None


class TestRunMixerSanityHelper:
    """Direct tests of _run_mixer_sanity — ensures telemetry/logging
    path and the error-swallowing contract.
    """

    @pytest.mark.asyncio()
    async def test_happy_path_logs_outcome(self) -> None:
        result_stub = MixerSanityResult(
            decision=MixerSanityDecision.SKIPPED_HEALTHY,
            diagnosis_before=Diagnosis.HEALTHY,
            diagnosis_after=None,
            regime="healthy",
            matched_kb_profile=None,
            kb_match_score=0.0,
            user_customization_score=0.0,
            cards_probed=(0,),
            controls_modified=(),
            rollback_snapshot=None,
            probe_duration_ms=120,
            apply_duration_ms=None,
            validation_passed=None,
            validation_metrics=None,
        )
        stub = AsyncMock(return_value=result_stub)
        with patch(
            "sovyx.voice.health._mixer_sanity.check_and_maybe_heal",
            new=stub,
        ):
            await cascade_mod._run_mixer_sanity(
                mixer_sanity=_sanity_setup(),
                endpoint_guid="endpoint-test",
                device_index=0,
                device_friendly_name="Test Mic",
                combo_store=None,
                capture_overrides=None,
            )
        stub.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_check_and_maybe_heal_exception_is_swallowed(self) -> None:
        async def raising_heal(**_kwargs: object) -> MixerSanityResult:
            msg = "synthetic"
            raise RuntimeError(msg)

        with patch(
            "sovyx.voice.health._mixer_sanity.check_and_maybe_heal",
            new=raising_heal,
        ):
            # Must NOT raise — swallowed by _run_mixer_sanity.
            await cascade_mod._run_mixer_sanity(
                mixer_sanity=_sanity_setup(),
                endpoint_guid="endpoint-test",
                device_index=0,
                device_friendly_name="Test Mic",
                combo_store=None,
                capture_overrides=None,
            )

    @pytest.mark.asyncio()
    async def test_no_hw_friendly_name_synthesizes_placeholder(self) -> None:
        """When device_friendly_name is empty, synthesize one from guid."""
        stub = AsyncMock(
            return_value=MixerSanityResult(
                decision=MixerSanityDecision.DEFERRED_NO_KB,
                diagnosis_before=Diagnosis.MIXER_UNKNOWN_PATTERN,
                diagnosis_after=None,
                regime="unknown",
                matched_kb_profile=None,
                kb_match_score=0.0,
                user_customization_score=0.0,
                cards_probed=(),
                controls_modified=(),
                rollback_snapshot=None,
                probe_duration_ms=0,
                apply_duration_ms=None,
                validation_passed=None,
                validation_metrics=None,
            ),
        )
        with patch(
            "sovyx.voice.health._mixer_sanity.check_and_maybe_heal",
            new=stub,
        ):
            await cascade_mod._run_mixer_sanity(
                mixer_sanity=_sanity_setup(),
                endpoint_guid="endpoint-xyz",
                device_index=0,
                device_friendly_name="",
                combo_store=None,
                capture_overrides=None,
            )
        endpoint_arg = stub.await_args.args[0]
        assert endpoint_arg.friendly_name == "endpoint-endpoint-xyz"
        assert endpoint_arg.canonical_name == "endpoint-endpoint-xyz"


class TestBackwardCompat:
    """Every pre-F1.F caller must observe identical behaviour."""

    @pytest.mark.asyncio()
    async def test_existing_cascade_kwargs_unchanged(self) -> None:
        """Calling run_cascade with the exact old signature works."""
        # Build the call with no mixer_sanity kwarg.
        result = await run_cascade(
            endpoint_guid="endpoint-legacy",
            device_index=0,
            mode=ProbeMode.COLD,
            platform_key="win32",
            probe_fn=_passing_probe,
        )
        assert result.endpoint_guid == "endpoint-legacy"

    @pytest.mark.asyncio()
    async def test_mixer_sanity_not_propagated_to_unrelated_paths(self) -> None:
        """Quarantined endpoints short-circuit BEFORE L2.5 runs."""
        from sovyx.voice.health._quarantine import EndpointQuarantine

        quarantine = EndpointQuarantine(quarantine_s=60.0)
        quarantine.add(
            endpoint_guid="endpoint-quarantined",
            reason="probe_kernel_invalidated",
        )
        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=AsyncMock(),
        ) as spy:
            result = await run_cascade(
                endpoint_guid="endpoint-quarantined",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="linux",
                probe_fn=_passing_probe,
                quarantine=quarantine,
                mixer_sanity=_sanity_setup(),
            )
        # Quarantined endpoints return early without invoking L2.5.
        spy.assert_not_awaited()
        assert result.source == "quarantined"


class TestPinnedFastPathWithL25:
    @pytest.mark.asyncio()
    async def test_pinned_override_hits_without_l25(self) -> None:
        """Pinned combo short-circuits BEFORE L2.5."""
        from sovyx.voice.health.capture_overrides import CaptureOverrides

        overrides = MagicMock(spec=CaptureOverrides)
        # Mock a pinned combo that's valid and passes a probe.
        pinned = Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key="linux",
        )
        overrides.get.return_value = pinned

        with patch(
            "sovyx.voice.health.cascade._run_mixer_sanity",
            new=AsyncMock(),
        ) as spy:
            await run_cascade(
                endpoint_guid="endpoint-pinned",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="linux",
                probe_fn=_passing_probe,
                capture_overrides=overrides,
                mixer_sanity=_sanity_setup(),
            )
        # Pinned success → cascade returns before L2.5 runs.
        spy.assert_not_awaited()
