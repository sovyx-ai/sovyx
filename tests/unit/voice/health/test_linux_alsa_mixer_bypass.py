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
            saturating=saturating,
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
            saturating=saturating,
            endpoint_friendly_name="intel speaker front",
        )
        assert target is saturating[0]

    def test_short_tokens_never_match(self) -> None:
        saturating = [
            _card(card_index=0, card_longname="AT Sound Card"),
            _card(card_index=1, card_longname="HD Audio Xyz"),
        ]
        target = _match_target_card(saturating=saturating, endpoint_friendly_name="at hd device")
        # Neither "at" nor "hd" length>=4 — multiple cards tie.
        assert target is None

    def test_single_card_fallback(self) -> None:
        only = [_card()]
        target = _match_target_card(saturating=only, endpoint_friendly_name="unrelated name")
        assert target is only[0]

    def test_ambiguous_returns_none(self) -> None:
        saturating = [_card(card_index=0), _card(card_index=1, card_id="OTHER")]
        target = _match_target_card(saturating=saturating, endpoint_friendly_name="unrelated")
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
    async def test_no_saturation(self) -> None:
        strat = LinuxALSAMixerResetBypass()
        with patch.object(
            mod,
            "enumerate_alsa_mixer_snapshots",
            return_value=[_card(saturation_warning=False)],
        ):
            res = await strat.probe_eligibility(_context())
        assert res.applicable is False
        assert res.reason == "no_saturated_controls_detected"

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
