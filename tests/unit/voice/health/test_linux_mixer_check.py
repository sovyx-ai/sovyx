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
