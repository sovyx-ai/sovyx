"""In-process AGC2 — closed-loop digital gain controller (F5, Layer 4).

Layer 4 of the Linux mixer cascade (mission §4) — the universal
fallback that runs in-process on captured audio when no upstream
mixer / KB profile / system AGC can deliver the right operating
level. AGC2 (Automatic Gain Control v2) is WebRTC's canonical
closed-loop digital gain controller; this implementation distils
the production WebRTC algorithm
(``chromium.googlesource.com/external/webrtc/+/main/modules/audio_processing/agc2/``)
to its load-bearing primitives:

* **Speech-level estimator** — exponential moving average of
  per-frame RMS dBFS, gated by an energy floor so silence doesn't
  drag the estimate down.
* **P-controller** — proportional gain adjustment toward the target
  dBFS. Asymmetric attack vs release time constants match human
  perception (fast at suppressing loud transients; slow at boosting
  quiet input so noise floor doesn't pump up).
* **Slew-rate limiter** — cap the per-second gain change to a
  perceptually transparent rate (default 6 dB/sec, matching the
  WebRTC AGC2 ``max_gain_change_db_per_second``).
* **Saturation protector** — peak-detect the upcoming chunk, clamp
  the gain so the post-multiply peak stays below the int16 rail.
  Prevents the silent-clip class of bugs the R2 saturation feedback
  monitor would otherwise have to surface after the fact.
* **Configurable target + max gain** — operators tune via
  :class:`AGC2Config` (or via ``SOVYX_TUNING__VOICE__AGC2_*`` env
  vars when wired into ``VoiceTuningConfig`` in a future commit).

This module is the band-aid replacement for
:func:`sovyx.voice.health._linux_mixer_apply.apply_mixer_boost_up`.
The pre-F5 path mutated ALSA mixer raw values via hard-coded
fractions when an attenuated capture was detected — fragile,
device-specific, and required user-space write access to the mixer.
The post-F5 path operates entirely on the captured PCM in user
space, so it works regardless of mixer permissions, mixer presence
(USB devices that lack mixer controls entirely), or the
distribution's audio stack (PulseAudio / PipeWire / bare ALSA).

The future Layer-4-deletion commit (F6) replaces ``apply_mixer_reset``
+ ``apply_mixer_boost_up`` with a single KB-driven path that falls
back to this AGC2 when no KB profile matches. F5 ships the
foundation; F6 wires it.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4
(Linux 4-layer cascade), §3.12 (Linux mixer band-aids), F5 task,
WebRTC AGC2 source (chromium.googlesource.com), Apple AVAudioUnit
voice-processing AGC reference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_INT16_FULL_SCALE = 32768.0
"""int16 normalisation divisor. RMS dBFS = 20 * log10(rms / 32768)."""

_INT16_RAIL = 32767
"""int16 positive rail. The saturation protector caps post-gain peaks
just below this value (using ``_INT16_RAIL - 1`` for headroom against
floating-point rounding)."""

_DEFAULT_TARGET_DBFS = -18.0
"""Target operating level. -18 dBFS is the EBU R128 / ITU-R BS.1770
broadcast loudness target for speech — perceptually balanced
(~half-scale RMS), gives ~18 dB of headroom against transients,
and matches Whisper / Moonshine training-set normalisation."""

_DEFAULT_MAX_GAIN_DB = 30.0
"""Maximum positive gain the AGC will apply. 30 dB matches WebRTC AGC2
default — enough to lift a -48 dBFS mic to -18 dBFS (typical
attenuated-mixer scenario), bounded to prevent infinite-amplification
runaway on a fully muted input."""

_DEFAULT_MIN_GAIN_DB = -10.0
"""Minimum (negative) gain — i.e. attenuation. -10 dB handles a
hot-mic input (e.g. boost stage left high). Floor prevents the
controller from suppressing loud-but-legitimate dynamics
(percussion, exclamations) into inaudibility."""

_DEFAULT_SILENCE_FLOOR_DBFS = -60.0
"""Below this RMS the speech-level estimator does NOT update. Below
-60 dBFS is consumer-playback noise floor (R128); estimating from
silence would drag the gain target toward 0 dB and pump up the
noise during the next speech burst."""

_DEFAULT_ATTACK_TIME_S = 0.010
"""Time constant for gain DECREASE (response to loud input). 10 ms
is fast enough to catch a transient before it audibly clips, slow
enough to avoid pumping artefacts on natural speech dynamics
(consonants vs vowels). WebRTC AGC2 uses 10 ms for the same reason."""

_DEFAULT_RELEASE_TIME_S = 0.250
"""Time constant for gain INCREASE (response to quiet input). 250 ms
matches the perceptual threshold for noticing a level change
(Glasberg + Moore, 2002) — slower would feel sluggish on
quiet-then-resume speech; faster would pump up the noise floor
during pauses."""

_DEFAULT_MAX_GAIN_CHANGE_DB_PER_SECOND = 6.0
"""Slew-rate limit on the gain change. 6 dB/s matches WebRTC AGC2
canonical and is the perceptually transparent ceiling — faster
gain ramps are audible as "level pumping". Operators wanting a
more aggressive ramp can override via :class:`AGC2Config`, but
the default is the safe broadcast-grade value."""


@dataclass(frozen=True, slots=True)
class AGC2Config:
    """Calibrated AGC2 parameters.

    Defaults match WebRTC AGC2 / EBU R128 / ITU-R BS.1770 canonical
    values; deviating without rationale is the band-aid pattern
    F5 exists to replace.

    Attributes:
        target_dbfs: Speech-level operating point. Default -18 dBFS.
        max_gain_db: Maximum positive gain (dB).
        min_gain_db: Minimum (negative) gain (dB) — i.e. attenuation
            ceiling. Must satisfy ``min_gain_db <= 0 <= max_gain_db``.
        silence_floor_dbfs: RMS below this value gates out the
            speech-level estimator update.
        attack_time_s: Time constant for gain DECREASE (loud input).
        release_time_s: Time constant for gain INCREASE (quiet input).
            Should be ≥ ``attack_time_s`` (asymmetric attack/release
            is what makes AGC perceptually transparent).
        max_gain_change_db_per_second: Slew-rate ceiling for gain
            updates. The integration-frame magnitude of the gain
            change is clamped to this rate × frame duration.
        sample_rate: Audio sample rate the controller will see at
            ``process``. Used to translate frame-sample counts to
            seconds for the slew-rate limiter and time-constant
            integrators. Bounded to the Sovyx-supported
            ``[8000, 48000]`` Hz range.
    """

    target_dbfs: float = _DEFAULT_TARGET_DBFS
    max_gain_db: float = _DEFAULT_MAX_GAIN_DB
    min_gain_db: float = _DEFAULT_MIN_GAIN_DB
    silence_floor_dbfs: float = _DEFAULT_SILENCE_FLOOR_DBFS
    attack_time_s: float = _DEFAULT_ATTACK_TIME_S
    release_time_s: float = _DEFAULT_RELEASE_TIME_S
    max_gain_change_db_per_second: float = _DEFAULT_MAX_GAIN_CHANGE_DB_PER_SECOND
    sample_rate: int = 16_000


def _validate_config(config: AGC2Config) -> None:
    """Reject obviously-pathological config at construction.

    Pre-F5 the band-aid mixer-fractions had no validation. Loud
    failure at construction is the enterprise pattern this module
    inherits from V3 (Schmitt-trigger min-delta floor) and the
    pipeline config bounds work.
    """
    if not (-80.0 <= config.target_dbfs <= 0.0):
        msg = (
            f"target_dbfs must be in [-80, 0], got {config.target_dbfs} "
            f"(0 dBFS is full-scale; broadcast targets are around -18 to -23)"
        )
        raise ValueError(msg)
    if config.max_gain_db < 0.0:
        msg = f"max_gain_db must be >= 0, got {config.max_gain_db}"
        raise ValueError(msg)
    if config.min_gain_db > 0.0:
        msg = f"min_gain_db must be <= 0, got {config.min_gain_db}"
        raise ValueError(msg)
    if config.silence_floor_dbfs > config.target_dbfs:
        msg = (
            f"silence_floor_dbfs ({config.silence_floor_dbfs}) must be "
            f"<= target_dbfs ({config.target_dbfs}) — gating out the "
            f"target itself would freeze the controller"
        )
        raise ValueError(msg)
    if config.attack_time_s <= 0.0:
        msg = f"attack_time_s must be > 0, got {config.attack_time_s}"
        raise ValueError(msg)
    if config.release_time_s <= 0.0:
        msg = f"release_time_s must be > 0, got {config.release_time_s}"
        raise ValueError(msg)
    if config.release_time_s < config.attack_time_s:
        msg = (
            f"release_time_s ({config.release_time_s}) must be >= "
            f"attack_time_s ({config.attack_time_s}) — a faster release "
            f"than attack pumps up the noise floor"
        )
        raise ValueError(msg)
    if config.max_gain_change_db_per_second <= 0.0:
        msg = (
            f"max_gain_change_db_per_second must be > 0, "
            f"got {config.max_gain_change_db_per_second}"
        )
        raise ValueError(msg)
    if not (8_000 <= config.sample_rate <= 48_000):
        msg = (
            f"sample_rate must be in [8000, 48000], got {config.sample_rate} "
            f"(Sovyx-supported range)"
        )
        raise ValueError(msg)


class AGC2:
    """Closed-loop digital gain controller for int16 PCM audio.

    Constructed once per capture stream; ``process`` is called for
    every frame. The controller maintains its own internal gain state
    so the next call's gain target reflects the previous frame's
    speech level (asymmetric attack/release smoothing).

    Args:
        config: Calibrated parameters. ``None`` falls back to
            :class:`AGC2Config` defaults (WebRTC AGC2 canonical
            values).

    Raises:
        ValueError: ``config`` violates an invariant (e.g. release
            faster than attack — would pump up the noise floor).
    """

    def __init__(self, config: AGC2Config | None = None) -> None:
        cfg = config or AGC2Config()
        _validate_config(cfg)
        self._config = cfg
        # Current applied gain (dB). Initialised at 0 dB so the first
        # frame is unaltered until the controller has a speech-level
        # estimate to act on.
        self._current_gain_db: float = 0.0
        # Speech-level estimate (dBFS). Initialised at the target so
        # the first frame error is zero — controller stays put until
        # a real speech RMS arrives.
        self._speech_level_dbfs: float = cfg.target_dbfs
        # Counters surface via the public properties so operators can
        # see whether the AGC is actually doing work or sitting idle
        # (e.g. when a KB profile delivers correct levels and AGC2
        # never has to adapt).
        self._frames_processed: int = 0
        self._frames_silenced: int = 0  # below silence floor, not adapted
        self._frames_clipped: int = 0  # saturation protector engaged
        logger.debug(
            "agc2_initialised",
            target_dbfs=cfg.target_dbfs,
            max_gain_db=cfg.max_gain_db,
            min_gain_db=cfg.min_gain_db,
            sample_rate=cfg.sample_rate,
        )

    @property
    def config(self) -> AGC2Config:
        """Active configuration (read-only)."""
        return self._config

    @property
    def current_gain_db(self) -> float:
        """Currently-applied gain in dB. Useful for dashboard
        attribution + the future Layer 4 telemetry feedback loop."""
        return self._current_gain_db

    @property
    def speech_level_dbfs(self) -> float:
        """Current speech-level estimate (dBFS). Above the
        ``silence_floor_dbfs``, this is the smoothed RMS the
        P-controller compares against ``target_dbfs``."""
        return self._speech_level_dbfs

    @property
    def frames_processed(self) -> int:
        """Lifetime count of frames passed through ``process``."""
        return self._frames_processed

    @property
    def frames_silenced(self) -> int:
        """Lifetime count of frames whose RMS was below
        ``silence_floor_dbfs`` (no estimator update). Non-zero
        ratio over a long window is normal (silences between
        utterances)."""
        return self._frames_silenced

    @property
    def frames_clipped(self) -> int:
        """Lifetime count of frames where the saturation protector
        engaged (post-gain peak would have exceeded the int16 rail
        without the clamp). Non-zero on a healthy mic = the AGC is
        protecting against a hot-input transient; chronic non-zero
        = the configured ``max_gain_db`` is too high."""
        return self._frames_clipped

    def process(
        self,
        samples: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]:
        """Apply gain to ``samples`` and return the controlled output.

        Per-frame loop:

        1. Compute frame RMS in dBFS.
        2. If RMS >= silence floor: update speech-level estimate via
           exponential moving average (asymmetric attack/release time
           constants — fast at suppressing loud, slow at boosting
           quiet, prevents noise-floor pumping).
        3. Compute desired gain = target - speech_level (dB). Clamp
           to ``[min_gain_db, max_gain_db]``.
        4. Slew-rate-limit the change from current gain toward
           desired gain (clamped by
           ``max_gain_change_db_per_second × frame_duration``).
        5. Saturation protector: compute peak * linear_gain. If the
           result exceeds the int16 rail, lower the gain so peak ×
           gain stays just below the rail. Bumps the
           ``frames_clipped`` counter for observability.
        6. Apply linear_gain to samples; clip to int16 rails for
           defence-in-depth (in case of float rounding past the
           saturation protector's calculated bound).

        Args:
            samples: ``int16`` PCM mono frame. Empty arrays are
                returned unchanged + counted as silenced (no work
                to do).

        Returns:
            ``int16`` PCM array of the same shape as ``samples``,
            with the controlled gain applied + clipped to the
            int16 rails.
        """
        import numpy as np

        if samples.size == 0:
            self._frames_processed += 1
            self._frames_silenced += 1
            return samples

        # Step 1: RMS in dBFS. Compute on float64 for numerical
        # stability — int16 squares overflow if naively summed.
        as_float = samples.astype(np.float64)
        rms_linear = float(np.sqrt(np.mean(as_float * as_float)))
        if rms_linear <= 0.0:
            rms_dbfs = float("-inf")
        else:
            rms_dbfs = 20.0 * math.log10(rms_linear / _INT16_FULL_SCALE)

        # Step 2: speech-level estimator update (gated by floor).
        if rms_dbfs >= self._config.silence_floor_dbfs:
            self._update_speech_level(rms_dbfs, samples_in_frame=samples.size)
        else:
            self._frames_silenced += 1

        # Step 3: P-controller — desired gain = target - estimate.
        desired_gain_db = self._config.target_dbfs - self._speech_level_dbfs
        desired_gain_db = max(
            self._config.min_gain_db,
            min(self._config.max_gain_db, desired_gain_db),
        )

        # Step 4: slew-rate limit.
        new_gain_db = self._slew_limit_gain(
            desired_gain_db,
            samples_in_frame=samples.size,
        )

        # Step 5: saturation protector. Compute the peak the new gain
        # would produce and clamp gain so the output stays below the
        # rail with 1-LSB headroom.
        peak = float(np.max(np.abs(as_float))) if as_float.size else 0.0
        new_gain_db = self._clamp_for_saturation(new_gain_db, peak=peak)

        self._current_gain_db = new_gain_db
        self._frames_processed += 1

        # Step 6: apply gain, clip, cast.
        linear_gain = 10.0 ** (new_gain_db / 20.0)
        scaled = as_float * linear_gain
        clipped = np.clip(scaled, -float(_INT16_FULL_SCALE), float(_INT16_RAIL))
        out: npt.NDArray[np.int16] = clipped.astype(np.int16)
        return out

    def reset(self) -> None:
        """Reset the controller to its initial state.

        Clears the speech-level estimate (back to target), the
        applied gain (back to 0 dB), and the lifetime counters.
        Call between unrelated capture sessions so the prior
        session's adaptation doesn't bias the next session's first
        frame.
        """
        self._current_gain_db = 0.0
        self._speech_level_dbfs = self._config.target_dbfs
        self._frames_processed = 0
        self._frames_silenced = 0
        self._frames_clipped = 0

    # ── Private helpers ─────────────────────────────────────────────

    def _update_speech_level(
        self,
        rms_dbfs: float,
        *,
        samples_in_frame: int,
    ) -> None:
        """Asymmetric exponential moving average update.

        Uses ``attack_time_s`` when the new RMS is LOUDER than the
        current estimate (need to react fast to suppress), and
        ``release_time_s`` when it's QUIETER (slow release prevents
        noise-floor pumping). The smoothing coefficient is derived
        from the time constant and the frame duration via the
        canonical first-order EMA discrete-time formula:
        ``alpha = 1 - exp(-frame_duration / time_constant)``.
        """
        frame_duration_s = samples_in_frame / self._config.sample_rate
        if rms_dbfs > self._speech_level_dbfs:
            tau = self._config.attack_time_s
        else:
            tau = self._config.release_time_s
        alpha = 1.0 - math.exp(-frame_duration_s / tau)
        self._speech_level_dbfs = alpha * rms_dbfs + (1.0 - alpha) * self._speech_level_dbfs

    def _slew_limit_gain(
        self,
        desired_gain_db: float,
        *,
        samples_in_frame: int,
    ) -> float:
        """Cap the gain change to ``max_gain_change_db_per_second``.

        The frame's allowed gain change is the rate ceiling × frame
        duration in seconds. Any larger move is clamped to the
        per-frame ceiling, in the appropriate direction.
        """
        frame_duration_s = samples_in_frame / self._config.sample_rate
        max_change_db = self._config.max_gain_change_db_per_second * frame_duration_s
        delta = desired_gain_db - self._current_gain_db
        if delta > max_change_db:
            return self._current_gain_db + max_change_db
        if delta < -max_change_db:
            return self._current_gain_db - max_change_db
        return desired_gain_db

    def _clamp_for_saturation(self, gain_db: float, *, peak: float) -> float:
        """Lower ``gain_db`` so ``peak × linear_gain`` stays under the rail.

        Bumps the ``frames_clipped`` counter when an actual clamp
        fires (i.e. the unclamped post-gain peak would have exceeded
        the rail). Without the clamp the existing
        :func:`sovyx.voice._frame_normalizer._float_to_int16_saturate`
        would clip downstream, but the AGC's controlled-gain
        contract requires preventing clip BEFORE it happens —
        otherwise the R2 saturation monitor flags every loud
        transient as a real over-gain event.
        """
        if peak <= 0.0:
            return gain_db
        linear_gain = 10.0 ** (gain_db / 20.0)
        post_peak = peak * linear_gain
        rail = float(_INT16_RAIL - 1)  # 1-LSB headroom for FP rounding
        if post_peak <= rail:
            return gain_db
        # Solve for the gain that brings post_peak down to rail.
        max_safe_linear = rail / peak
        max_safe_db = (
            20.0 * math.log10(max_safe_linear)
            if max_safe_linear > 0.0
            else (self._config.min_gain_db)
        )
        # Bound the clamp at min_gain_db so we never reduce below the
        # configured attenuation floor even on a fully-saturated input.
        max_safe_db = max(max_safe_db, self._config.min_gain_db)
        self._frames_clipped += 1
        return min(gain_db, max_safe_db)


__all__ = ["AGC2", "AGC2Config"]
