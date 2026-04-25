"""End-to-end integration: AGC2 recovers attenuated-mic input (the
mission's original trigger).

The user's voice was broken on Linux Mint VAIO due to ALSA mixer
attenuation that delivered ~ -40 dBFS speech to the cascade. The
v0.22.4 band-aid (``apply_mixer_boost_up``) "fixed" this with
hardcoded ALSA mixer fractions — fragile, device-specific,
required user-space mixer write access.

The enterprise replacement: AGC2 closed-loop digital gain
controller (F5, commit 8e17e8c) wired as opt-in into
FrameNormalizer (1083744), promoted to default-on (2e36893 +
7408e43). This integration test validates that the full
production pipeline — TS1 synthetic attenuated input →
FrameNormalizer.push → AGC2 — actually recovers the user's
audio level WITHOUT any band-aid mixer writes.

This is the closest thing to validating the fix works without
the original VAIO hardware. Property tests in TS4 prove AGC2
satisfies its DSP invariants in isolation; this test proves
the production wire-up delivers the user-visible result.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25
F5/F6; commits 8e17e8c (AGC2), 1083744 (opt-in wire-up),
2e36893 (config + factory), 7408e43 (default-on at 6 sites);
F1 inventory band-aid #L4.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sovyx.voice._agc2 import AGC2, AGC2Config
from sovyx.voice._frame_normalizer import FrameNormalizer
from tests._voice_audio_fixtures import sine_tone


def _rms_dbfs(samples: np.ndarray) -> float:
    """Compute RMS in dBFS (full-scale = 32768 int16 sample value)."""
    if samples.size == 0:
        return float("-inf")
    as_float = samples.astype(np.float64)
    rms_linear = float(np.sqrt(np.mean(as_float * as_float)))
    if rms_linear <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms_linear / 32768.0)


# ── Reproduction parameters (matches mission VAIO baseline) ────────


_ATTENUATED_INPUT_AMPLITUDE = 0.01
"""Normalised amplitude that produces ~ -40 dBFS RMS — the mission's
documented baseline for the attenuated-mic failure mode. The
user's ALSA mixer was reading 13/100 (13% of full scale), which
corresponds to roughly this normalised amplitude after the
mixer attenuation was applied to typical speech levels."""


_AGC2_TARGET_DBFS = -18.0
"""WebRTC AGC2 / EBU R128 broadcast loudness target for speech.
The pipeline's default ``AGC2Config.target_dbfs``. After AGC2
convergence, the post-process RMS must be within tolerance of
this value."""


_CONVERGENCE_TOLERANCE_DB = 6.0
"""Acceptable post-convergence deviation from
``_AGC2_TARGET_DBFS``. AGC2's slew-rate limiter (default 6 dB/sec)
caps the per-second gain change, so even after 10 s of
convergence at full slew rate we may not hit the target exactly.
6 dB tolerance is one full slew-step + headroom for the
saturation protector + asymmetric attack/release behaviour."""


# ── End-to-end recovery test ──────────────────────────────────────


class TestAttenuatedMicAgc2Recovery:
    """End-to-end validation: attenuated input → FrameNormalizer + AGC2
    → recovered output near target level."""

    def test_pipeline_recovers_attenuated_input_to_target(self) -> None:
        """Drive 10 seconds of -40 dBFS sine through the production
        FrameNormalizer + AGC2 path. Final-second RMS must be within
        6 dB of the AGC2 target dBFS."""
        # Force the non-passthrough path so AGC2 runs (passthrough
        # int16-mono-16kHz-unity-gain skips AGC2 by design — that
        # path is bit-exact for operators who need golden recordings).
        # source_rate=48_000 → resample to 16_000 → non-passthrough.
        agc2 = AGC2(AGC2Config(target_dbfs=_AGC2_TARGET_DBFS))
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            agc2=agc2,
        )

        # Build 10 s of -40 dBFS sine at 1 kHz (audible speech band).
        block_duration_s = 0.05  # 50 ms blocks (50 × 20 = 10 s)
        n_blocks = 200
        attenuated_block = sine_tone(
            freq_hz=1_000.0,
            duration_s=block_duration_s,
            amplitude=_ATTENUATED_INPUT_AMPLITUDE,
            sample_rate=48_000,
        )
        # Confirm input level matches mission baseline.
        input_rms = _rms_dbfs(attenuated_block)
        assert input_rms < -36.0, (  # noqa: PLR2004
            f"input RMS {input_rms:.1f} dBFS not in attenuated range; check fixture amplitude"
        )

        # Drive the pipeline. Collect output windows for level analysis.
        all_windows: list[np.ndarray] = []
        for _ in range(n_blocks):
            windows = norm.push(attenuated_block)
            all_windows.extend(windows)

        assert all_windows, "FrameNormalizer produced no windows"

        # Last second of windows reflects post-convergence behaviour.
        # 16 kHz × 1 s / 512 sample window ≈ 31 windows.
        last_second = np.concatenate(all_windows[-31:])
        post_agc2_rms = _rms_dbfs(last_second)

        # AGC2 should have raised the level toward the target.
        # Target is -18 dBFS; tolerance is 6 dB (slew-rate limited).
        assert (
            (_AGC2_TARGET_DBFS - _CONVERGENCE_TOLERANCE_DB)
            <= post_agc2_rms
            <= (_AGC2_TARGET_DBFS + _CONVERGENCE_TOLERANCE_DB)
        ), (
            f"AGC2 failed to converge: post-AGC2 RMS={post_agc2_rms:.1f} dBFS, "
            f"target={_AGC2_TARGET_DBFS} ± {_CONVERGENCE_TOLERANCE_DB} dB. "
            f"Input was {input_rms:.1f} dBFS. AGC2 current_gain_db="
            f"{agc2.current_gain_db:.1f}, frames_processed="
            f"{agc2.frames_processed}, frames_clipped="
            f"{agc2.frames_clipped}."
        )

    def test_pipeline_does_nothing_on_already_well_levelled_input(
        self,
    ) -> None:
        """Drive 10 seconds of -18 dBFS (already-target) sine.
        AGC2 should stay near 0 dB gain — it's a transparent no-op
        when the input is at the right level."""
        agc2 = AGC2(AGC2Config(target_dbfs=_AGC2_TARGET_DBFS))
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            agc2=agc2,
        )
        # Amplitude 0.125 → ~ -18 dBFS (target).
        well_levelled_block = sine_tone(
            freq_hz=1_000.0,
            duration_s=0.05,
            amplitude=0.125,
            sample_rate=48_000,
        )
        input_rms = _rms_dbfs(well_levelled_block)
        assert -22.0 < input_rms < -14.0, (  # noqa: PLR2004
            f"input fixture RMS {input_rms:.1f} dBFS not in target range"
        )

        for _ in range(200):
            norm.push(well_levelled_block)

        # AGC2 gain should stay close to 0 dB — already-correct
        # input doesn't need amplification.
        assert abs(agc2.current_gain_db) < 6.0, (  # noqa: PLR2004
            f"AGC2 over-corrected on already-target input: "
            f"current_gain_db={agc2.current_gain_db:.2f}"
        )

    def test_pipeline_does_not_amplify_silence_indefinitely(self) -> None:
        """Pure silence input must NOT trigger AGC2 to ramp gain
        up — silence-floor gating prevents pumping up the noise
        floor (the canonical AGC failure mode)."""
        agc2 = AGC2(AGC2Config(target_dbfs=_AGC2_TARGET_DBFS))
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            agc2=agc2,
        )
        # Pure-zero block, 50 ms × 200 = 10 s of silence.
        silent_block = np.zeros(int(0.05 * 48_000), dtype=np.int16)
        for _ in range(200):
            norm.push(silent_block)

        # AGC2's silence-floor gate (-60 dBFS) should have prevented
        # the speech-level estimator from updating, so gain stays at
        # the initial 0 dB.
        assert agc2.current_gain_db == 0.0, (
            f"AGC2 ramped gain on pure silence: "
            f"current_gain_db={agc2.current_gain_db:.2f}; expected 0.0"
        )
        # Most frames should be classified as silenced.
        assert agc2.frames_silenced > 0
        assert agc2.frames_silenced >= agc2.frames_processed * 0.95

    def test_agc2_disabled_does_not_recover(self) -> None:
        """Sanity guard: WITHOUT AGC2 (the pre-F5 behaviour), the
        attenuated input flows through the cascade unchanged. Proves
        AGC2 is doing the work — not some other path."""
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            agc2=None,  # explicit: no AGC2
        )
        attenuated_block = sine_tone(
            freq_hz=1_000.0,
            duration_s=0.05,
            amplitude=_ATTENUATED_INPUT_AMPLITUDE,
            sample_rate=48_000,
        )

        all_windows: list[np.ndarray] = []
        for _ in range(200):
            windows = norm.push(attenuated_block)
            all_windows.extend(windows)

        last_second = np.concatenate(all_windows[-31:])
        post_rms = _rms_dbfs(last_second)
        # Without AGC2, the output stays attenuated (within ~3 dB of
        # input — small drift from resample filter is normal).
        assert post_rms < -30.0, (  # noqa: PLR2004
            f"output RMS {post_rms:.1f} dBFS unexpectedly high without "
            f"AGC2 — something else is amplifying"
        )


# ── Skip-marker if scipy not installed ─────────────────────────────


# scipy is a hard dep but mark it explicit for the resample path
# in case future deployment modes strip it.
@pytest.fixture(autouse=True)
def _require_scipy() -> None:
    pytest.importorskip("scipy")
