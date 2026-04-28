"""Unit tests for :class:`LinuxALSAMixerResetBypass`.

Covers every branch the strategy contract requires:

* ``probe_eligibility`` — not-linux / disabled-by-tuning / no-snapshots
  / no-saturation / card-match-ambiguous / applicable happy path
* ``apply`` — re-probe returns empty / card vanishes / target-card
  has zero saturating controls / delegation to :func:`apply_mixer_reset`
* ``revert`` — idempotent when snapshot is None / delegates to
  :func:`restore_mixer_snapshot` and clears state
* ``_match_target_card`` — card_id match, long-name-token match,
  single-card fallback, ambiguous tie
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sovyx.voice.health.bypass import _linux_alsa_mixer as mod
from sovyx.voice.health.bypass._linux_alsa_mixer import (
    LinuxALSAMixerResetBypass,
    _match_target_card,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import (
    BypassContext,
    MixerApplySnapshot,
    MixerCardSnapshot,
    MixerControlSnapshot,
)


def _card(
    *,
    card_index: int = 1,
    card_id: str = "PCH",
    card_longname: str = "HDA Intel PCH",
    saturation_warning: bool = True,
    controls: tuple[MixerControlSnapshot, ...] | None = None,
) -> MixerCardSnapshot:
    default_controls = (
        MixerControlSnapshot(
            name="Capture",
            min_raw=0,
            max_raw=31,
            current_raw=31 if saturation_warning else 15,
            current_db=36.0,
            max_db=36.0,
            is_boost_control=True,
            saturation_risk=saturation_warning,
        ),
    )
    return MixerCardSnapshot(
        card_index=card_index,
        card_id=card_id,
        card_longname=card_longname,
        controls=controls or default_controls,
        aggregated_boost_db=36.0 if saturation_warning else 0.0,
        saturation_warning=saturation_warning,
    )


def _context(
    *,
    platform_key: str = "linux",
    endpoint_friendly_name: str = "HDA Intel PCH: ALC295 Analog (hw:0,0)",
    host_api_name: str = "ALSA",
) -> BypassContext:
    # The strategy never calls into capture_task or probe_fn during
    # eligibility/apply/revert — pass plain stubs so the dataclass is
    # well-formed without pulling in real infrastructure.
    async def _probe() -> None:  # pragma: no cover — never invoked
        raise AssertionError("probe_fn must not be called")

    capture_task = object()
    return BypassContext(
        endpoint_guid="guid",
        endpoint_friendly_name=endpoint_friendly_name,
        host_api_name=host_api_name,
        platform_key=platform_key,
        capture_task=capture_task,  # type: ignore[arg-type]
        probe_fn=_probe,  # type: ignore[arg-type]
    )


class TestMatchTargetCard:
    def test_card_id_substring_match(self) -> None:
        saturating = [_card(card_id="Generic_1"), _card(card_index=2, card_id="ZZZ")]
        target = _match_target_card(
            faulted=saturating,
            endpoint_friendly_name="Generic_1: USB Audio",
        )
        assert target is saturating[0]

    def test_longname_token_match(self) -> None:
        saturating = [
            _card(
                card_index=0,
                card_id="PCH",
                card_longname="HDA Intel PCH at 0xf7e40000",
            ),
            _card(card_index=1, card_id="HDMI", card_longname="HDMI audio"),
        ]
        target = _match_target_card(
            faulted=saturating,
            endpoint_friendly_name="intel speaker front",
        )
        assert target is saturating[0]

    def test_short_tokens_never_match(self) -> None:
        saturating = [
            _card(card_index=0, card_longname="AT Sound Card"),
            _card(card_index=1, card_longname="HD Audio Xyz"),
        ]
        target = _match_target_card(faulted=saturating, endpoint_friendly_name="at hd device")
        # Neither "at" nor "hd" length>=4 — multiple cards tie.
        assert target is None

    def test_single_card_fallback(self) -> None:
        only = [_card()]
        target = _match_target_card(faulted=only, endpoint_friendly_name="unrelated name")
        assert target is only[0]

    def test_ambiguous_returns_none(self) -> None:
        saturating = [_card(card_index=0), _card(card_index=1, card_id="OTHER")]
        target = _match_target_card(faulted=saturating, endpoint_friendly_name="unrelated")
        assert target is None


class TestProbeEligibility:
    @pytest.mark.asyncio()
    async def test_not_linux(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        res = await strat.probe_eligibility(_context(platform_key="win32"))
        assert res.applicable is False
        assert res.reason == "not_linux_platform"

    @pytest.mark.asyncio()
    async def test_disabled_by_tuning(self) -> None:
        strat = LinuxALSAMixerResetBypass()

        class _T:
            linux_alsa_mixer_reset_enabled = False

        with patch.object(mod, "_tuning_from_context", return_value=_T()):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is False
        assert res.reason == "alsa_mixer_reset_disabled_by_tuning"

    @pytest.mark.asyncio()
    async def test_no_amixer(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        with patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[]):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is False
        assert res.reason == "amixer_unavailable_on_host"

    @pytest.mark.asyncio()
    async def test_no_fault_detected(self) -> None:
        # Card with neither saturation_warning nor attenuation pattern.
        strat = LinuxALSAMixerResetBypass()
        with patch.object(
            mod,
            "enumerate_alsa_mixer_snapshots",
            return_value=[_card(saturation_warning=False)],
        ):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is False
        # Renamed in v0.22.3: covers both saturation AND attenuation regimes.
        assert res.reason == "no_saturation_or_attenuation_detected"

    @pytest.mark.asyncio()
    async def test_card_match_ambiguous(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        with patch.object(
            mod,
            "enumerate_alsa_mixer_snapshots",
            return_value=[
                _card(card_index=0, card_id="AAA"),
                _card(card_index=1, card_id="BBB"),
            ],
        ):
            res = await strat.probe_eligibility(_context(endpoint_friendly_name="something else"))
        assert res.applicable is False
        assert res.reason == "card_match_ambiguous"

    @pytest.mark.asyncio()
    async def test_applicable_happy_path(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        with patch.object(
            mod,
            "enumerate_alsa_mixer_snapshots",
            return_value=[_card()],
        ):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is True
        assert res.estimated_cost_ms > 0


class TestApply:
    @pytest.mark.asyncio()
    async def test_no_snapshots_at_apply(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        with (
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[]),
            pytest.raises(BypassApplyError) as exc,
        ):
            await strat.apply(_context())
        assert exc.value.reason == "no_mixer_snapshots_at_apply"

    @pytest.mark.asyncio()
    async def test_target_card_vanishes(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        # Two ambiguous cards, no match.
        with (
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[
                    _card(card_index=0, card_id="AAA"),
                    _card(card_index=1, card_id="BBB"),
                ],
            ),
            pytest.raises(BypassApplyError) as exc,
        ):
            await strat.apply(_context(endpoint_friendly_name="xxx"))
        assert exc.value.reason == "target_card_unavailable_at_apply"

    @pytest.mark.asyncio()
    async def test_no_saturated_controls_on_target(self) -> None:
        # saturation_warning True but no control has saturation_risk set.
        control = MixerControlSnapshot(
            name="Capture",
            min_raw=0,
            max_raw=31,
            current_raw=10,
            current_db=0.0,
            max_db=36.0,
            is_boost_control=True,
            saturation_risk=False,
        )
        card = _card(controls=(control,), saturation_warning=True)
        strat = LinuxALSAMixerResetBypass()
        with (
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[card]),
            pytest.raises(BypassApplyError) as exc,
        ):
            await strat.apply(_context())
        assert exc.value.reason == "no_saturated_controls_at_apply"

    @pytest.mark.asyncio()
    async def test_happy_path_records_snapshot(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 31),),
            applied_controls=(("Capture", 15),),
        )
        with (
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[_card()]),
            patch.object(mod, "apply_mixer_reset", new=AsyncMock(return_value=snap)) as apply_mock,
        ):
            detail = await strat.apply(_context())
        assert detail == "mixer_reset_applied"
        assert strat._applied_snapshot is snap  # noqa: SLF001
        apply_mock.assert_awaited_once()


class TestRevert:
    @pytest.mark.asyncio()
    async def test_noop_when_no_snapshot(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        # No prior apply — revert must not raise or call amixer.
        with patch.object(mod, "restore_mixer_snapshot") as restore:
            await strat.revert(_context())
        restore.assert_not_called()

    @pytest.mark.asyncio()
    async def test_delegates_and_clears(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 31),),
            applied_controls=(("Capture", 15),),
        )
        strat._applied_snapshot = snap  # noqa: SLF001
        with patch.object(mod, "restore_mixer_snapshot", new=AsyncMock()) as restore:
            await strat.revert(_context())
        restore.assert_awaited_once()
        assert strat._applied_snapshot is None  # noqa: SLF001


# ============================================================================
# Attenuation regime — added in v0.22.3 (commit fixes the SAME architectural
# bug that the CLI `--fix` had: the auto-bypass strategy filtered only
# `saturation_warning`, so attenuated cards never reached apply().
# Pilot evidence: VAIO VJFE69F11X-B0221H, 2026-04-25 — bypass coordinator
# tried 3 strategies, all returned `not_applicable`, endpoint quarantined.
# ============================================================================


def _card_attenuated(
    *,
    card_index: int = 1,
    card_id: str = "Generic_1",
    card_longname: str = "HD-Audio Generic",
) -> MixerCardSnapshot:
    """Pilot fixture: Mic Boost 0/3 + Capture 40/80 + Internal Mic Boost 1/3.

    `_is_attenuated` requires: at least one capture-class control at or
    below 0.5 fraction AND at least one boost-class control at min_raw.
    """
    controls = (
        MixerControlSnapshot(
            name="Mic Boost",
            min_raw=0,
            max_raw=3,
            current_raw=0,  # zeroed boost — attenuation signature
            current_db=0.0,
            max_db=36.0,
            is_boost_control=True,
            saturation_risk=False,
        ),
        MixerControlSnapshot(
            name="Capture",
            min_raw=0,
            max_raw=80,
            current_raw=40,  # 50% — at the attenuation ceiling
            current_db=-34.0,
            max_db=30.0,
            is_boost_control=True,
            saturation_risk=False,
        ),
        MixerControlSnapshot(
            name="Internal Mic Boost",
            min_raw=0,
            max_raw=3,
            current_raw=1,
            current_db=12.0,
            max_db=36.0,
            is_boost_control=True,
            saturation_risk=False,
        ),
    )
    return MixerCardSnapshot(
        card_index=card_index,
        card_id=card_id,
        card_longname=card_longname,
        controls=controls,
        aggregated_boost_db=-22.0,
        saturation_warning=False,  # attenuation, not saturation
    )


class TestProbeEligibilityAttenuation:
    @pytest.mark.asyncio()
    async def test_attenuated_card_is_applicable(self) -> None:
        """v0.22.3 fix: attenuated cards now trigger applicable=True."""
        strat = LinuxALSAMixerResetBypass()
        with patch.object(
            mod,
            "enumerate_alsa_mixer_snapshots",
            return_value=[_card_attenuated()],
        ):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is True
        assert res.estimated_cost_ms > 0


class TestApplyAttenuationRegime:
    @pytest.mark.asyncio()
    async def test_apply_routes_to_boost_up(self) -> None:
        """v0.22.3 fix: apply() must call apply_mixer_boost_up for attenuation."""
        strat = LinuxALSAMixerResetBypass()
        attenuated_card = _card_attenuated()

        boost_snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(
                ("Mic Boost", 0),
                ("Capture", 40),
                ("Internal Mic Boost", 1),
            ),
            applied_controls=(
                ("Mic Boost", 2),
                ("Capture", 60),
                ("Internal Mic Boost", 2),
            ),
        )
        with (
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[attenuated_card],
            ),
            patch.object(
                mod,
                "apply_mixer_boost_up",
                new=AsyncMock(return_value=boost_snap),
            ) as boost,
            patch.object(mod, "apply_mixer_reset", new=AsyncMock()) as reset,
        ):
            outcome = await strat.apply(_context())

        assert outcome == "mixer_boost_up_applied"
        # boost_up called, reset NOT called.
        boost.assert_awaited_once()
        reset.assert_not_called()
        # Snapshot stashed for revert path.
        assert strat._applied_snapshot is boost_snap  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_apply_routes_to_reset_when_saturated(self) -> None:
        """Saturation regime still routes to apply_mixer_reset (regression)."""
        strat = LinuxALSAMixerResetBypass()
        sat_card = _card()  # default_controls with saturation_risk=True
        reset_snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 31),),
            applied_controls=(("Capture", 15),),
        )
        with (
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[sat_card],
            ),
            patch.object(
                mod,
                "apply_mixer_reset",
                new=AsyncMock(return_value=reset_snap),
            ) as reset,
            patch.object(mod, "apply_mixer_boost_up", new=AsyncMock()) as boost,
        ):
            outcome = await strat.apply(_context())

        assert outcome == "mixer_reset_applied"
        reset.assert_awaited_once()
        boost.assert_not_called()

    @pytest.mark.asyncio()
    async def test_apply_rolls_back_when_boost_up_overshoots(self) -> None:
        """v0.22.4 safety: if boost-up flips regime to saturation, roll back.

        Pilot evidence (VAIO VJFE69F11X, 2026-04-25, post-v0.22.3): the
        first attenuation-fix defaults (0.75/0.66) lifted the controls
        past the saturation_ratio_ceiling (0.5). Pre-fix preflight
        reported attenuation; post-fix reported saturation. VAD still
        deaf (signal clipped). Safety check must detect that and roll
        back to leave the mixer in its pre-apply state.
        """
        strat = LinuxALSAMixerResetBypass()
        attenuated_pre = _card_attenuated()
        # Same card_index but post-apply now saturating.
        saturated_post = _card(
            card_index=1,
            card_id="Generic_1",
            saturation_warning=True,
        )
        boost_snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Mic Boost", 0), ("Capture", 40)),
            applied_controls=(("Mic Boost", 2), ("Capture", 60)),
        )
        # First call returns pre-apply (attenuated); second call returns
        # post-apply (now saturated) → triggers rollback.
        snapshot_seq = [[attenuated_pre], [saturated_post]]
        with (
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                side_effect=snapshot_seq,
            ),
            patch.object(
                mod,
                "apply_mixer_boost_up",
                new=AsyncMock(return_value=boost_snap),
            ) as boost,
            patch.object(
                mod,
                "restore_mixer_snapshot",
                new=AsyncMock(),
            ) as restore,
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strat.apply(_context())

        # boost_up was called, then restore was called (rollback path).
        boost.assert_awaited_once()
        restore.assert_awaited_once()
        # Snapshot cleared so revert() does not double-restore.
        assert strat._applied_snapshot is None  # noqa: SLF001
        # Reason token surfaced for coordinator telemetry.
        assert exc_info.value.reason == "apply_overcorrected_to_saturation"


class TestBandAidDeprecationWarn:
    """Mission §9.1.1 / Gap 1a — every successful band-aid apply emits
    a structured WARN so dashboards / log search can identify
    deployments still relying on the legacy fraction-based path.

    The WARN is the operator-visible deprecation surface; it does NOT
    change behaviour (the band-aid still applies and reverts as
    before). Removal target: v0.24.0, after the L2.5 KB-driven preset
    cascade (Layer 3) covers the codecs reported via this WARN.
    """

    @pytest.mark.asyncio()
    async def test_saturation_apply_emits_band_aid_warn(self) -> None:
        """A successful saturation-regime apply emits exactly one
        ``voice.mixer.alsa_band_aid_used`` WARN with regime="saturation"
        and the operator-actionable message naming KB profile
        contribution as the future-proof path. Spied on the module
        logger so the test is invariant to structlog → stdlib bridge
        configuration state (which caplog otherwise depends on)."""
        from unittest.mock import MagicMock

        strat = LinuxALSAMixerResetBypass()
        snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 31),),
            applied_controls=(("Capture", 15),),
        )
        spy = MagicMock()
        with (
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[_card()]),
            patch.object(mod, "apply_mixer_reset", new=AsyncMock(return_value=snap)),
            patch.object(mod, "logger", spy),
        ):
            await strat.apply(_context())

        # The strategy emits 2 warning calls only on overshoot rollback
        # (which we did not set up here) — so the only warning fired
        # is the deprecation surface.
        deprecation_calls = [
            call
            for call in spy.warning.call_args_list
            if call.args and call.args[0] == "voice.mixer.alsa_band_aid_used"
        ]
        assert len(deprecation_calls) == 1
        kwargs = deprecation_calls[0].kwargs
        assert kwargs["voice.regime"] == "saturation"
        # T1.51 — removal target bumped from v0.24.0 to v0.27.0 (Phase 4)
        # because the bypass-coordinator wire-up gating Phase 2 + 3 must
        # land first; aligned with the function-level deprecation WARN
        # at ``_linux_mixer_apply.py::_emit_legacy_band_aid_warning``.
        assert kwargs["voice.removal_target"] == "v0.27.0"
        assert "voice.action_required" in kwargs
        assert "v0.27.0" in str(kwargs["voice.action_required"])
