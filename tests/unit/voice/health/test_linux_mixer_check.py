"""Unit tests for :mod:`sovyx.voice.health._linux_mixer_check`.

Covers the three behavioural branches the preflight contract requires:

* non-Linux platforms — pass unconditionally with ``skipped=True``
* Linux hosts where :func:`enumerate_alsa_mixer_snapshots` returns ``[]``
  — pass with an informational note (``amixer`` absent or no controls)
* Linux hosts where at least one card reports ``saturation_warning`` —
  fail, hint + full snapshot details
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health import _linux_mixer_check as mod
from sovyx.voice.health._linux_mixer_check import check_linux_mixer_sanity
from sovyx.voice.health.contract import MixerCardSnapshot, MixerControlSnapshot


def _card(
    *,
    card_index: int = 1,
    saturation_warning: bool = False,
    controls: tuple[MixerControlSnapshot, ...] | None = None,
) -> MixerCardSnapshot:
    return MixerCardSnapshot(
        card_index=card_index,
        card_id="PCH",
        card_longname="HDA Intel PCH",
        controls=controls
        or (
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
        ),
        aggregated_boost_db=36.0 if saturation_warning else 0.0,
        saturation_warning=saturation_warning,
    )


class TestCheckLinuxMixerSanity:
    @pytest.mark.asyncio()
    async def test_non_linux_passes_unconditionally(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            check = check_linux_mixer_sanity()
            ok, hint, details = await check()
        assert ok is True
        assert hint == ""
        assert details["skipped"] is True
        assert details["platform"] == "win32"

    @pytest.mark.asyncio()
    async def test_linux_no_snapshots_passes_with_note(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=[]),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, hint, details = await check()
        assert ok is True
        assert hint == ""
        assert details["snapshots"] == []
        assert "amixer" in details["note"]

    @pytest.mark.asyncio()
    async def test_linux_healthy_snapshots_pass(self) -> None:
        snapshots = [_card(saturation_warning=False)]
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=snapshots),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, hint, details = await check()
        assert ok is True
        assert hint == ""
        assert len(details["snapshots"]) == 1
        assert details["snapshots"][0]["saturation_warning"] is False

    @pytest.mark.asyncio()
    async def test_linux_saturation_fails_with_hint(self) -> None:
        snapshots = [_card(saturation_warning=True)]
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "enumerate_alsa_mixer_snapshots", return_value=snapshots),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, hint, details = await check()
        assert ok is False
        assert "Reset microphone gain" in hint
        assert details["snapshots"][0]["saturation_warning"] is True
        assert details["snapshots"][0]["saturating_controls"] == ["Capture"]

    @pytest.mark.asyncio()
    async def test_tuning_ceilings_are_echoed(self) -> None:
        tuning = VoiceTuningConfig()
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[_card(saturation_warning=True)],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity(tuning=tuning)
            _ok, _hint, details = await check()
        assert (
            details["aggregated_boost_db_ceiling"]
            == tuning.linux_mixer_aggregated_boost_db_ceiling
        )
        assert details["saturation_ratio_ceiling"] == tuning.linux_mixer_saturation_ratio_ceiling


# ── Attenuation branch (F1.F) ───────────────────────────────────────


def _attenuated_card(
    *,
    capture_fraction: float = 0.4,
    boost_at_zero: bool = True,
) -> MixerCardSnapshot:
    """Card matching the L2.5 attenuation signature (pilot VAIO case)."""
    capture = MixerControlSnapshot(
        name="Capture",
        min_raw=0,
        max_raw=80,
        current_raw=int(80 * capture_fraction),
        current_db=-34.0,
        max_db=None,
        is_boost_control=True,
        saturation_risk=False,
    )
    boost = MixerControlSnapshot(
        name="Internal Mic Boost",
        min_raw=0,
        max_raw=3,
        current_raw=0 if boost_at_zero else 3,
        current_db=0.0 if boost_at_zero else 36.0,
        max_db=None,
        is_boost_control=True,
        saturation_risk=False,
    )
    return MixerCardSnapshot(
        card_index=0,
        card_id="PCH",
        card_longname="HDA Intel PCH (SN6180)",
        controls=(capture, boost),
        aggregated_boost_db=0.0,
        saturation_warning=False,
    )


class TestAttenuationBranch:
    @pytest.mark.asyncio()
    async def test_attenuated_card_flags_fail(self) -> None:
        """Capture<=0.5 + boost at 0 → FAIL with attenuation hint."""
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[_attenuated_card()],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, hint, details = await check()
        assert ok is False
        assert "heavily attenuated" in hint
        assert details["snapshots"][0]["attenuation_warning"] is True

    @pytest.mark.asyncio()
    async def test_capture_above_ceiling_no_attenuation_flag(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[_attenuated_card(capture_fraction=0.8)],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, _hint, details = await check()
        assert ok is True
        assert details["snapshots"][0]["attenuation_warning"] is False

    @pytest.mark.asyncio()
    async def test_boost_non_zero_skips_attenuation(self) -> None:
        """Low Capture but Boost > 0 → user added their own gain, not
        attenuation.
        """
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[_attenuated_card(boost_at_zero=False)],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, _hint, details = await check()
        assert ok is True
        assert details["snapshots"][0]["attenuation_warning"] is False

    @pytest.mark.asyncio()
    async def test_saturation_takes_precedence_over_attenuation(self) -> None:
        """If a card is BOTH saturated (rare edge) and attenuated, the
        saturation hint wins (cure is simpler + already shipped).
        """
        sat_card = _card(saturation_warning=True)
        att_card = _attenuated_card()
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[sat_card, att_card],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            ok, hint, _details = await check()
        assert ok is False
        # Saturation hint — the older + already-remediated path.
        assert "saturated pre-ADC" in hint

    @pytest.mark.asyncio()
    async def test_attenuation_ceiling_is_echoed_in_details(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(
                mod,
                "enumerate_alsa_mixer_snapshots",
                return_value=[_attenuated_card()],
            ),
        ):
            sys_mock.platform = "linux"
            check = check_linux_mixer_sanity()
            _ok, _hint, details = await check()
        assert details["attenuation_capture_fraction_ceiling"] == 0.5  # noqa: PLR2004

    def test_is_attenuated_helper_degenerate_card(self) -> None:
        """Card with no capture + no boost controls → not attenuated."""
        weird = MixerCardSnapshot(
            card_index=9,
            card_id="X",
            card_longname="No capture/boost controls",
            controls=(),
            aggregated_boost_db=0.0,
            saturation_warning=False,
        )
        assert mod._is_attenuated(weird) is False

    def test_is_attenuated_helper_zero_span_control_skipped(self) -> None:
        """Control with max_raw == min_raw (e.g. a single-value enum)
        is skipped — no division by zero, no false attenuation.
        """
        stuck = MixerCardSnapshot(
            card_index=9,
            card_id="X",
            card_longname="Stuck control",
            controls=(
                MixerControlSnapshot(
                    name="Capture",
                    min_raw=42,
                    max_raw=42,
                    current_raw=42,
                    current_db=0.0,
                    max_db=None,
                    is_boost_control=False,
                    saturation_risk=False,
                ),
            ),
            aggregated_boost_db=0.0,
            saturation_warning=False,
        )
        assert mod._is_attenuated(stuck) is False
