"""Integration test — v1.3 §7.5 "hybrid" recovery scenario.

Stitches together the real :class:`CaptureIntegrityCoordinator`, a real
:class:`CaptureIntegrityProbe` (with a deterministic fake VAD), and a
real :class:`AudioCaptureTask` driven through its private
``_ring_write`` surface. The strategy's ``apply`` is a scripted shim —
it does not invoke ``amixer`` — but it mutates the backing ring buffer
the same way a real mixer reset would: bad frames stop, clean frames
start.

The point is to verify the bug that motivated the whole plan
(``SVX-VOICE-LINUX-20260422``) does not recur when every layer above
the apply is real:

1. Pre-apply, the ring holds clipped frames (simulated 200 Hz rolloff).
2. The coordinator captures the mark before ``apply`` is invoked.
3. ``apply`` stops writing clipped frames and starts writing silence.
4. The coordinator's ``tap_frames_since_mark`` returns ONLY post-apply
   frames (silence), the probe classifies them as VAD_MUTE with high
   rolloff, and the improvement heuristic treats that as resolved.
5. The strategy is not reverted.

Tests here run on every platform — no subprocess, no PortAudio.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice.health.capture_integrity import (
    CaptureIntegrityCoordinator,
    CaptureIntegrityProbe,
)
from sovyx.voice.health.contract import (
    BypassContext,
    BypassVerdict,
    Eligibility,
    IntegrityVerdict,
)

_SAMPLE_RATE = 16_000


def _synth_clipped(duration_s: float) -> npt.NDArray[np.int16]:
    """Square wave near int16 max — extreme clipping, low rolloff."""
    n = int(duration_s * _SAMPLE_RATE)
    if n <= 0:
        return np.zeros(0, dtype=np.int16)
    # 200 Hz square: 80 samples per half-period at 16 kHz.
    half_period = 40
    sign = 1
    out = np.empty(n, dtype=np.int16)
    for i in range(0, n, half_period):
        out[i : i + half_period] = 30_000 if sign > 0 else -30_000
        sign *= -1
    return out


def _synth_quiet_clean(duration_s: float) -> npt.NDArray[np.int16]:
    """Quiet white noise at RMS ≈ -60 dBFS with a wideband spectrum.

    Chosen so the probe classifies the post-apply window as VAD_MUTE
    (not DRIVER_SILENT, not APO_DEGRADED) and the improvement
    heuristic fires on the rolloff jump from ~600 Hz (clipped square)
    to well above the 4 kHz spectrum-degraded ceiling. Pure zeros
    would land in DRIVER_SILENT and the improvement branch would not
    trigger; a loud tone would trip APO_DEGRADED on its narrow
    spectrum. Low-amplitude noise is the exact shape the classifier
    reads as "signal present, not speech, spectrum healthy".
    """
    n = int(duration_s * _SAMPLE_RATE)
    rng = np.random.default_rng(0xC0DEC)
    # Amplitude 30 → RMS ≈ 30 / sqrt(3) / 32768 ≈ -66 dBFS (below the
    # -50 dBFS APO floor, above the -80 dBFS driver-silent ceiling).
    return rng.integers(-30, 30, size=n, dtype=np.int16)


class _ScriptedFakeVAD:
    """Deterministic VAD — returns vad_max_prob below the VAD_MUTE
    ceiling for every input, so the post-apply probe always classifies
    the silent buffer as VAD_MUTE (triggering the improvement heuristic
    branch the coordinator needs to exercise)."""

    def reset(self) -> None:
        pass

    def process_frame(self, _window: npt.NDArray[np.float32]) -> object:
        @dataclass(frozen=True)
        class _Event:
            probability: float = 0.001

        return _Event()


@dataclass
class _ScriptedMixerResetStrategy:
    """Fake ``linux.alsa_mixer_reset`` that flips the capture task's
    input from clipped frames to silence — the exact side effect the
    real strategy has on the post-apply ring buffer."""

    name: str = "linux.alsa_mixer_reset"
    capture_task: AudioCaptureTask | None = None

    async def probe_eligibility(self, _ctx: BypassContext) -> Eligibility:
        return Eligibility(applicable=True, reason="", estimated_cost_ms=0)

    async def apply(self, ctx: BypassContext) -> str:
        # Flush buffer and fill with post-apply quiet-clean signal so
        # the mark-based tap sees only post-fix frames. Quiet noise
        # (not silence) keeps the classifier above DRIVER_SILENT while
        # its flat/wideband spectrum exercises the v1.3 §14.E2
        # improvement heuristic (rolloff jump >> 5× the clipped
        # baseline).
        assert self.capture_task is not None
        # Write 3.5 s of clean signal post-apply (covering the probe window).
        for chunk in range(7):  # 7 × 0.5 s  ≈ 3.5 s
            self.capture_task._ring_write(_synth_quiet_clean(0.5))  # noqa: SLF001
            # Yield to the tap loop so it can observe progress.
            await asyncio.sleep(0)
            _ = chunk
        return "mixer_reset_applied"

    async def revert(self, _ctx: BypassContext) -> None:
        # Revert would write clipped frames back — the coordinator's
        # improvement heuristic should prevent us from reaching here.
        pytest.fail(
            "strategy was reverted despite healthy recovery — the L4-B "
            "fix did not prevent the v0.21.2 probe-window regression.",
        )


def _make_capture_prefilled_with_clipped() -> AudioCaptureTask:
    """Build a capture task with 3 s of clipped pre-apply frames.

    Uses ``__new__`` to skip the real PortAudio open — we set only
    the attributes the coordinator's ``_build_context`` +
    mark/tap path actually touches, so the task never attempts real
    audio IO. ``_running=True`` is required because
    ``active_device_kind`` reads it before resolving the device kind.
    """
    task = AudioCaptureTask.__new__(AudioCaptureTask)
    task._ring_buffer = np.zeros(int(33.0 * _SAMPLE_RATE), dtype=np.int16)  # noqa: SLF001
    task._ring_capacity = task._ring_buffer.size  # noqa: SLF001
    task._ring_write_index = 0  # noqa: SLF001
    task._ring_state = 0  # noqa: SLF001
    task._tuning = VoiceTuningConfig(mark_tap_poll_interval_s=0.01)  # noqa: SLF001
    task._running = True  # noqa: SLF001 — reached via ``active_device_kind``
    task._endpoint_guid = "guid-integration"  # noqa: SLF001
    task._resolved_device_name = "Integration Fake Mic"  # noqa: SLF001
    task._host_api_name = "ALSA"  # noqa: SLF001
    task._input_device = 1  # noqa: SLF001

    # Pre-apply content: 3 s of clipped saturation.
    task._allocate_ring_buffer(task._tuning)  # noqa: SLF001
    task._ring_write(_synth_clipped(3.0))  # noqa: SLF001
    return task


class TestProbeWindowHybridRecovery:
    """Full coordinator + probe + capture task + scripted strategy."""

    @pytest.mark.asyncio()
    async def test_apply_triggers_post_apply_only_probe_and_resolves(self) -> None:
        tuning = VoiceTuningConfig(
            # Keep the probe snappy so the test finishes in <1 s real
            # time while still exercising the jitter margin.
            integrity_probe_duration_s=0.8,
            bypass_strategy_post_apply_settle_s=1.0,
            probe_jitter_margin_s=0.5,
            mark_tap_poll_interval_s=0.02,
        )
        capture = _make_capture_prefilled_with_clipped()

        probe = CaptureIntegrityProbe(vad=_ScriptedFakeVAD(), tuning=tuning)  # type: ignore[arg-type]
        strategy = _ScriptedMixerResetStrategy(capture_task=capture)

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,
            strategies=[strategy],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=tuning,
        )

        outcomes = await coordinator.handle_deaf_signal()

        # Coordinator reached APPLIED_HEALTHY (either directly, via
        # HEALTHY, or via the improvement heuristic) — never
        # APPLIED_STILL_DEAD + revert.
        assert len(outcomes) >= 1
        final = outcomes[-1]
        assert final.verdict is BypassVerdict.APPLIED_HEALTHY, (
            f"expected APPLIED_HEALTHY, got {final.verdict!r} — "
            f"detail={final.detail!r}; before rolloff="
            f"{final.integrity_before.spectral_rolloff_hz:.0f} Hz, "
            f"after rolloff="
            f"{final.integrity_after.spectral_rolloff_hz if final.integrity_after else 'n/a'} Hz"
        )
        # The post-apply probe sampled the post-silence region — its
        # rolloff must be much higher than the pre-apply clipped
        # signal's ~200 Hz.
        assert final.integrity_after is not None
        assert final.integrity_after.spectral_rolloff_hz > (
            final.integrity_before.spectral_rolloff_hz * tuning.improvement_rolloff_factor
        ), (
            "post-apply rolloff did not improve by the factor threshold — "
            "either the ring buffer still contains pre-apply frames "
            "(probe-window regression), or the strategy failed to install "
            "clean post-apply frames"
        )
        assert coordinator.is_resolved is True

    @pytest.mark.asyncio()
    async def test_pre_apply_verdict_is_degraded_not_healthy(self) -> None:
        """Sanity guard — the scenario genuinely starts in a degraded
        state (the coordinator would short-circuit on a HEALTHY pre-probe
        and the rest of the test would silently pass for the wrong
        reason)."""
        tuning = VoiceTuningConfig(
            integrity_probe_duration_s=0.8,
            bypass_strategy_post_apply_settle_s=1.0,
        )
        capture = _make_capture_prefilled_with_clipped()

        probe = CaptureIntegrityProbe(vad=_ScriptedFakeVAD(), tuning=tuning)  # type: ignore[arg-type]
        result = await probe.probe_warm(capture)  # type: ignore[arg-type]
        assert result.verdict is not IntegrityVerdict.HEALTHY
        # Clipped square wave has rolloff near the fundamental, far
        # below clean-speech's 6–8 kHz band.
        assert result.spectral_rolloff_hz < 2_000.0
