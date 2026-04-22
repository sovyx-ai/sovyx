"""T12.1 — unit tests for ``build_linux_cascade_for_device`` (VLX-005)."""

from __future__ import annotations

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health.cascade import LINUX_CASCADE, build_linux_cascade_for_device


class TestBuildLinuxCascadeForDevice:
    def test_non_hardware_returns_default(self) -> None:
        # Session-manager virtuals resample internally; default cascade
        # handles them fine.
        assert build_linux_cascade_for_device(48000, "session_manager_virtual") is LINUX_CASCADE
        assert build_linux_cascade_for_device(44100, "os_default") is LINUX_CASCADE
        assert build_linux_cascade_for_device(48000, "unknown") is LINUX_CASCADE

    def test_canonical_rate_returns_default(self) -> None:
        # 16k and 48k are already covered by the first two combos.
        assert build_linux_cascade_for_device(16000, "hardware") is LINUX_CASCADE
        assert build_linux_cascade_for_device(48000, "hardware") is LINUX_CASCADE

    def test_non_canonical_rate_prepends_native_combo(self) -> None:
        tailored = build_linux_cascade_for_device(44100, "hardware")
        assert tailored is not LINUX_CASCADE
        assert len(tailored) == len(LINUX_CASCADE) + 1
        assert tailored[0].sample_rate == 44100
        assert tailored[0].host_api == "ALSA"
        assert tailored[0].exclusive is True
        # Remainder is LINUX_CASCADE unchanged.
        assert tailored[1:] == LINUX_CASCADE

    def test_junk_rate_below_min_returns_default(self) -> None:
        assert build_linux_cascade_for_device(4, "hardware") is LINUX_CASCADE

    def test_junk_rate_above_max_returns_default(self) -> None:
        assert build_linux_cascade_for_device(1_000_000, "hardware") is LINUX_CASCADE

    def test_rate_outside_allowed_combo_rates_returns_default(self) -> None:
        # 11025 is within the min/max bounds but not in
        # ``ALLOWED_SAMPLE_RATES`` — Combo ctor would raise. Builder
        # must skip silently.
        assert build_linux_cascade_for_device(11025, "hardware") is LINUX_CASCADE

    def test_custom_tuning_bounds_can_narrow_window(self) -> None:
        tuning = VoiceTuningConfig(
            cascade_native_rate_min_hz=40_000,
            cascade_native_rate_max_hz=50_000,
        )
        # 22_050 is now below the min — default cascade.
        assert build_linux_cascade_for_device(22050, "hardware", tuning=tuning) is LINUX_CASCADE
        # 44_100 is within the custom window and canonical for
        # ``ALLOWED_SAMPLE_RATES``.
        tailored = build_linux_cascade_for_device(44100, "hardware", tuning=tuning)
        assert tailored[0].sample_rate == 44100

    @pytest.mark.parametrize("rate", [22050, 24000, 32000, 88200, 96000, 192000])
    def test_each_non_canonical_allowed_rate_is_prepended(self, rate: int) -> None:
        tailored = build_linux_cascade_for_device(rate, "hardware")
        assert tailored[0].sample_rate == rate
