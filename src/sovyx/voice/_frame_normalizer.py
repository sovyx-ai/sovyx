"""Normalise arbitrary PortAudio input into the pipeline's frame contract.

:class:`sovyx.voice.pipeline.VoicePipeline` hard-requires every frame fed via
:meth:`~sovyx.voice.pipeline._orchestrator.VoicePipeline.feed_frame` to be
exactly ``(512,) int16`` at **16 kHz mono** — that is the invariant Silero
v5 was trained on and any deviation causes the model to see silence even
when the microphone is capturing loud speech (probability stuck near
zero, pipeline never leaves IDLE).

Historically the capture task forwarded whatever shape PortAudio delivered
and only downmixed (``indata[:, 0]``). That silently worked when the WASAPI
mixer format already matched (16 kHz / 1 ch) but broke whenever the opener
pyramid fell back to 48 kHz / 2 ch — common on Windows shared-mode mics
(Razer BlackShark, most USB headsets). See the root-cause writeup in
``docs-internal/audits/voice-silent-vad.md`` (local) for the full debug
trace.

This module owns the four transformations that have to happen between
the PortAudio callback and :meth:`feed_frame`:

1. **Format normalise** — the VCHL cascade (``docs-internal/ADR-voice-
   capture-health-lifecycle.md`` §5.1) can negotiate ``int16`` / ``int24`` /
   ``float32`` capture. PortAudio delivers ``int24`` inside ``int32`` numpy
   arrays with the 24-bit payload sign-extended, so it scales with
   ``2**23`` (= 8 388 608), not ``2**31``. ``float32`` is already in
   [-1, 1]. All three collapse into the common ``float32 [-1, 1]``
   representation before any DSP runs.
2. **Downmix** — ``(N, C) → (N,)`` via channel averaging (mean) or
   explicit first-channel pick when the source is already mono.
3. **Resample** — arbitrary ``source_rate → 16 kHz`` via
   :func:`scipy.signal.resample_poly` (polyphase FIR). Stateless per call,
   which introduces a sub-millisecond filter-state discontinuity at block
   boundaries. The discontinuity is well below Silero's sensitivity and
   negligible for voice activity detection; end-to-end FFT tests verify
   that a 1 kHz tone stays at 1 kHz after the transform.
4. **Ducking gain (optional)** — §4.4.6.b of the ADR: while TTS is
   playing, attenuate the mic by ``-18 dB`` so residual bleed cannot
   retrigger the wake word / VAD. Applied in the resampled ``float32``
   domain with a short linear ramp (default ``10 ms`` at 16 kHz = 160
   samples) so step changes never click. Ramp length is well below the
   50 ms "gain removed within 50 ms of TTS-end" requirement in the ADR.
5. **Rewindow** — accumulate the resampled stream in a bounded buffer
   and emit as many complete 512-sample windows as possible. Partial
   windows are held for the next call so frame boundaries stay aligned
   across PortAudio blocks.

Fast-path: when ``source_rate == 16000`` and ``source_channels == 1`` the
resampler is skipped entirely and the normaliser degenerates into a pure
rewindower — zero DSP cost. Callers can still hand 16 kHz mono in blocks
of any size and get back 512-sample windows. The ducking stage stays a
multiply-by-1 no-op when the target gain is unity, so passthrough of
already-16-kHz ``int16`` mono remains bit-exact end-to-end.

Thread-safety: the class is **not** thread-safe. PortAudio delivers frames
on a worker thread and the sovyx capture task hops onto the asyncio loop
(``call_soon_threadsafe``) before calling :meth:`push`, so all invocations
serialise on the event loop. Adding a lock would buy nothing in the
current architecture and would drop ~5 µs/frame.

Memory: the internal buffers are bounded by ``_TARGET_WINDOW`` for output
(max 512 samples = 1 KiB at int16) and by ``blocksize`` for input (max ~6
KiB at 48 kHz / 32 ms). Safe for long-running daemons.
"""

from __future__ import annotations

import math
import time
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    measure_stage_duration,
    record_stage_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice._aec import AecProcessor, RenderPcmProvider
    from sovyx.voice._agc2 import AGC2
    from sovyx.voice._double_talk_detector import DoubleTalkDetector
    from sovyx.voice._noise_suppression import NoiseSuppressor
    from sovyx.voice._snr_estimator import SnrEstimator

logger = get_logger(__name__)


_TARGET_RATE = 16_000
"""Output sample rate — the invariant SileroVAD v5 and MoonshineSTT share."""

_TARGET_WINDOW = 512
"""Output window size in samples. 32 ms at 16 kHz — matches VAD config."""

_SUPPORTED_FORMATS = frozenset({"int16", "int24", "float32"})
"""Source formats the cascade can negotiate (ADR §5.1)."""

_INT24_SCALE = float(1 << 23)
"""PortAudio int24 sign-extended into int32 scales by 2**23, not 2**31."""

_INT16_SCALE = float(1 << 15)
"""int16 full-scale divisor."""

_DEFAULT_DUCKING_RAMP_MS = 10.0
"""Default ramp duration for mic-ducking step changes (ms).

10 ms at 16 kHz = 160 samples. Well under the ADR §4.4.6.b "gain removed
within 50 ms of TTS-end" requirement and short enough to stay inaudible
on voice content while long enough to avoid click artefacts from sudden
multiplicative steps.
"""


# ── Band-aid #41 replacement — enforced ducking ramp bounds ──────────
#
# Pre-band-aid #41: ``__init__`` only rejected ``ducking_ramp_ms <= 0``.
# A ramp of 0.001 ms (sub-sample) rounded to 0 ramp samples — clamped
# to ``max(1, ...)`` = a ONE-sample transition. A 1-sample step from
# unity to -18 dB at 16 kHz is a 62 µs gain ramp: well inside the
# audible-click window (~3 ms perceptual threshold for transient
# discontinuities) and produces a hard click on every TTS start/stop.
#
# Spec (F1 #41): "Min 160 samples". 160 samples at 16 kHz = 10 ms,
# matching the ADR-recommended default. The fix per CLAUDE.md anti-
# pattern #11 (loud-fail bounds): enforce the minimum at construction
# so a misconfigured deployment fails at boot with a clear ValueError
# rather than producing audible clicks the user will report.
#
# Upper bound: 1 000 ms (1 s). Above that the per-step ramp outlasts
# any realistic TTS phrase boundary — set_ducking_gain_db calls would
# never reach their target before the next change. Beyond 1 s the
# value is almost certainly a unit confusion (seconds vs. ms).
_MIN_DUCKING_RAMP_MS = 10.0
"""Minimum click-free ramp at 16 kHz (160 samples). Below this,
multiplicative gain steps produce audible clicks at TTS start/stop.
Anchored to the human transient-perception threshold (~3 ms) with
~3× headroom for the -18 dB default ducking range."""

_MAX_DUCKING_RAMP_MS = 1_000.0
"""Upper sanity bound. Above 1 s a per-step ramp outlasts any
realistic TTS phrase boundary — strong indicator of a unit error
(seconds vs. ms). Loud-fail rather than silently slowing
ducking response below the operator's intent."""


# ---------------------------------------------------------------------------
# R2 saturation feedback loop tuning
# ---------------------------------------------------------------------------
#
# Pre-R2: ``_float_to_int16_saturate`` clipped silently. Loud transients
# wrapped to int16 rails with no counter, no event, no signal back to the
# rest of the system. Sustained clipping (a too-hot mic, a runaway boost
# control) was invisible until a user complained. R2 adds:
#
# * A pure-function counter that returns ``(samples, SaturationCounters)``
#   so any caller sees the exact number of clipped samples per call.
# * A rolling-window monitor on :class:`FrameNormalizer` that aggregates
#   counters across blocks and emits a structured
#   ``voice.audio.saturation_clipping`` event when the clipping fraction
#   crosses the warning threshold. The event is rate-limited so a
#   sustained-clipping condition produces one log per window, not one
#   per block.
# * Public properties (``clipping_fraction``, ``samples_processed``,
#   ``samples_clipped``) that future AGC2 / mixer-recalibration logic
#   can read to drive a closed-loop gain controller (Ring 1 Layer 4).

_SATURATION_WARN_FRACTION = 0.05
"""Clipping fraction (clipped / total) above which the warning event
fires. 5% sustained clipping is the canonical "too hot" threshold from
EBU R128 and the ITU-R BS.1770 broadcast loudness standards: short
percussive transients (<5%) are perceptually transparent; sustained
saturation (>5%) is audible distortion."""

_SATURATION_WINDOW_SECONDS = 1.0
"""Rolling window over which the clipping fraction is computed and
warnings rate-limited. One second matches a typical voice-frame
heartbeat cadence and is long enough to suppress per-block noise
while short enough to react to sustained conditions before a user
notices."""

_SATURATION_MIN_SAMPLES_FOR_WARNING = 4096
"""Lower bound on ``samples_processed`` before a warning can fire. At
16 kHz this is ~256 ms of audio — enough to make the clipping fraction
statistically meaningful. Below this, even a 100% clipped block of
512 samples is too short to confidently classify as "sustained".
Prevents false positives on the very first (typically tiny) block
following stream open."""


# ---------------------------------------------------------------------------
# Phase-inversion downmix detection (band-aid #8 replacement)
# ---------------------------------------------------------------------------
#
# Pre-band-aid #8: ``_downmix`` averages stereo channels via
# ``block_f.mean(axis=1)``. When the channels are 180° out of phase
# (e.g. a USB stereo mic with a channel-swap firmware bug, or an
# active noise-cancelling headset that delivers an inverted reference
# signal in the right channel), the average collapses to silence.
# The cascade then sees a "deaf" capture, the deaf-detection
# coordinator promotes the device to APO bypass, and the user is
# none the wiser that their hardware is actively broken.
#
# Band-aid #8 fix: compute Pearson correlation between the channels
# (``r = sum(L*R) / sqrt(sum(L²)*sum(R²))``); if ``r < -0.3`` (the
# canonical "destructively correlated" threshold from audio-stream
# QA), emit a structured ``voice.audio.downmix_phase_inverted`` WARN
# rate-limited per stream. The downmix output continues unchanged
# (no behaviour change at this stage — operator decides whether to
# pick L-only / R-only / sum based on the WARN signal).

_PHASE_INVERSION_CORR_THRESHOLD = -0.3
"""Cross-channel Pearson correlation below which the downmix is
flagged as phase-inverted. -0.3 is the canonical "destructively
correlated" threshold from audio-stream QA references — below it
the average-downmix output is meaningfully attenuated relative to
either single channel. Above -0.3 the channels are merely
loosely-correlated (a normal stereo recording), not actively
cancelling. Below -0.7 they are essentially exact inversions
(the silence-on-downmix worst case)."""

_PHASE_INVERSION_MIN_RMS_FOR_CHECK = 0.001
"""Per-channel RMS floor below which the phase check is skipped
(returns 0.0 correlation = "no signal"). Below this floor the
denominator in the Pearson formula is dominated by quantisation
noise and the correlation is meaningless. 0.001 normalised =
~ -60 dBFS, well below speech levels but above the typical
silence floor."""

_PHASE_INVERSION_LOG_INTERVAL_S = 5.0
"""Minimum gap between two phase-inversion WARN logs from the same
FrameNormalizer instance. Without rate-limiting, a sustained
phase-inverted stereo input would log once per push() call
(~100 Hz), drowning the dashboard. 5 s matches the heartbeat
cadence and is short enough that operators see the issue within
the first failed utterance."""


# ── #7 — Format-detection probe (extends #40 dtype validation) ──
#
# #40 caught the dtype-mismatch case (caller declares ``float32`` but
# delivers ``int16`` array — np.asarray would silently coerce types).
# #7 closes the remaining two failure modes:
#
# 1. **Channel-layout drift.** A 2-D block whose ``shape[1]`` does NOT
#    match the cascade-negotiated ``source_channels`` was previously
#    averaged silently — a 5.1 surround source (6 channels) declared
#    as stereo (2 channels) would still produce a single-channel mean
#    over 6 columns, distorting the spatial recovery and silently
#    masking the misnegotiation.
#
# 2. **Float32-magnitude misnegotiation.** A caller declaring
#    ``float32`` whose buffer carries int16-magnitude values (e.g. a
#    PortAudio host adapter that mistakenly forwards raw int16 as
#    ``float32`` via cast instead of scaling) would trigger massive
#    saturation downstream. The dtype check passes; the data is wrong.
#    A loud one-shot WARN at the source — rate-limited — points the
#    operator at the upstream format negotiation rather than chasing
#    the symptom in R2 saturation logs.
#
# Both checks run on the slow (non-passthrough) path inside
# ``_to_float32_unscaled``. The passthrough fast-path is bit-exact
# and never sees these inputs.

_FLOAT32_MAGNITUDE_PROBE_THRESHOLD = 10.0
"""Maximum-absolute-value ceiling for a properly-scaled float32 source
block. Legitimate float32 audio sits in ``[-1, 1]`` (with optional
sub-unit headroom up to ~1.5 in some pipelines that allow soft-clip
margin). A block whose ``max(|x|) > 10`` is — with effectively
100 % confidence — int16-magnitude data mistakenly tagged as float32
upstream. 10.0 is conservative enough to absorb legitimate brick-wall
limiter overshoots without false-positiving."""

_FORMAT_DRIFT_LOG_INTERVAL_S = 60.0
"""Minimum gap between two format-drift WARN logs from the same
FrameNormalizer instance. Sustained misnegotiation would produce
~100 WARNs/sec without rate-limiting; 60 s gives operators one
clear signal per minute without log-flooding."""


@dataclass(frozen=True, slots=True)
class SaturationCounters:
    """Per-call clipping diagnostics from :func:`_float_to_int16_saturate`.

    Immutable so callers can pass it freely (telemetry payloads,
    rolling-window aggregators) without defensive copying.

    Attributes:
        total_samples: Number of float samples examined (the input
            array length). Always non-negative.
        clipped_positive: Samples that would have wrapped past the
            int16 positive rail (+32 767) and were clamped instead.
        clipped_negative: Samples that would have wrapped past the
            int16 negative rail (-32 768) and were clamped instead.
    """

    total_samples: int
    clipped_positive: int
    clipped_negative: int

    @property
    def clipped_total(self) -> int:
        """Sum of positive- and negative-rail clipped samples."""
        return self.clipped_positive + self.clipped_negative

    @property
    def clipping_fraction(self) -> float:
        """Fraction of samples that hit a rail. Returns 0.0 when empty."""
        if self.total_samples <= 0:
            return 0.0
        return self.clipped_total / self.total_samples


class FrameNormalizer:
    """Stream-oriented resample + downmix + rewindow for PortAudio input.

    Construct once per opened stream, call :meth:`push` on every callback
    block, forward each returned array to the pipeline. The class keeps
    a small output tail between calls so 512-sample windows stay aligned
    across PortAudio's variable block size.

    Args:
        source_rate: Rate PortAudio is delivering at (Hz). Must be > 0.
        source_channels: Channel count in each incoming block. Must be
            ≥ 1. When > 1, each block is downmixed by channel averaging
            before resampling.
        source_format: Sample format the cascade negotiated. Must be one
            of ``"int16"`` (default — numpy ``int16`` blocks), ``"int24"``
            (numpy ``int32`` blocks with 24-bit sign-extended payload), or
            ``"float32"`` (numpy ``float32`` blocks in ``[-1, 1]``).
        ducking_ramp_ms: Duration of the linear ramp used when
            :meth:`set_ducking_gain_db` changes the target gain. Default
            10 ms, which is well under the ADR's 50 ms release bound.
            Bounded ``[10.0, 1000.0]`` ms (band-aid #41) — sub-10 ms
            ramps produce audible clicks at TTS start/stop; >1 s ramps
            outlast realistic TTS phrase boundaries.
        agc2: Optional :class:`~sovyx.voice._agc2.AGC2` controller (F5).
            When provided, every produced int16 frame is passed through
            ``agc2.process`` before being rewindowed — closed-loop digital
            gain replaces the band-aid ``apply_mixer_boost_up`` mixer-
            mutation path. Wired opt-in (``None`` default) so existing
            integrations behave identically; the future F6 commit flips
            the factory default after pilot validation. AGC2 only runs
            on the non-passthrough path (where there's actual DSP work
            anyway); the passthrough fast-path stays bit-exact.

    Raises:
        ValueError: If ``source_rate`` ≤ 0, ``source_channels`` < 1,
            ``source_format`` is not one of the supported strings, or
            ``ducking_ramp_ms`` is outside ``[10.0, 1000.0]`` ms.
    """

    def __init__(
        self,
        source_rate: int,
        source_channels: int,
        *,
        source_format: str = "int16",
        ducking_ramp_ms: float = _DEFAULT_DUCKING_RAMP_MS,
        agc2: AGC2 | None = None,
        aec: AecProcessor | None = None,
        render_provider: RenderPcmProvider | None = None,
        double_talk_detector: DoubleTalkDetector | None = None,
        noise_suppressor: NoiseSuppressor | None = None,
        snr_estimator: SnrEstimator | None = None,
        dither_enabled: bool = False,
        dither_amplitude_lsb: float = 1.0,
        dither_rng: np.random.Generator | None = None,
    ) -> None:
        if source_rate <= 0:
            msg = f"source_rate must be positive, got {source_rate}"
            raise ValueError(msg)
        if source_channels < 1:
            msg = f"source_channels must be >= 1, got {source_channels}"
            raise ValueError(msg)
        if source_format not in _SUPPORTED_FORMATS:
            msg = (
                f"source_format must be one of {sorted(_SUPPORTED_FORMATS)}, got {source_format!r}"
            )
            raise ValueError(msg)
        if not (_MIN_DUCKING_RAMP_MS <= ducking_ramp_ms <= _MAX_DUCKING_RAMP_MS):
            # Band-aid #41 loud-fail: sub-10 ms ramps produce audible
            # clicks; >1 s ramps outlast TTS boundaries (likely unit
            # confusion). Anti-pattern #11 — fail at construction with
            # a clear message instead of silently producing artefacts.
            msg = (
                f"ducking_ramp_ms must be in "
                f"[{_MIN_DUCKING_RAMP_MS}, {_MAX_DUCKING_RAMP_MS}] ms "
                f"(band-aid #41 click-free bound), got {ducking_ramp_ms}"
            )
            raise ValueError(msg)

        import numpy as np

        self._source_rate = source_rate
        self._source_channels = source_channels
        self._source_format = source_format
        self._passthrough = source_rate == _TARGET_RATE and source_channels == 1

        gcd = math.gcd(source_rate, _TARGET_RATE)
        self._up = _TARGET_RATE // gcd
        self._down = source_rate // gcd

        self._output_buf: npt.NDArray[np.int16] = np.zeros(0, dtype=np.int16)

        self._ducking_ramp_ms = ducking_ramp_ms
        # Band-aid #41 made the prior ``max(1, ...)`` clamp dead — at
        # the minimum bound (10 ms) we already get 160 samples.
        self._ducking_ramp_samples = int(round(_TARGET_RATE * ducking_ramp_ms / 1000.0))
        self._current_linear_gain: float = 1.0
        self._target_linear_gain: float = 1.0

        # ── R2 saturation feedback monitor ───────────────────────────
        # Lifetime cumulative counters (visible via the public
        # properties). Survive ``reset`` because they describe the
        # health of the normaliser's data path over its entire
        # lifetime — a long-running daemon's clipping count is a
        # signal that the upstream gain is mis-set, regardless of any
        # mid-session resets.
        self._lifetime_samples_processed: int = 0
        self._lifetime_samples_clipped: int = 0
        # Rolling-window state. ``_window_*`` accumulators reset every
        # ``_SATURATION_WINDOW_SECONDS`` so the warning fraction
        # reflects RECENT clipping, not the integral over the entire
        # session. ``_last_warning_monotonic`` rate-limits the warning
        # event so a sustained-clipping condition produces one log per
        # window, not one per block.
        self._window_samples_processed: int = 0
        self._window_samples_clipped: int = 0
        self._window_started_monotonic: float | None = None
        self._last_warning_monotonic: float | None = None
        # Band-aid #8: phase-inversion downmix detector state.
        # ``_phase_inverted_count`` is cumulative over the lifetime
        # of the normaliser; ``_last_phase_warning_monotonic`` rate-
        # limits the WARN log per :data:`_PHASE_INVERSION_LOG_INTERVAL_S`.
        # Public counter exposed via :attr:`phase_inverted_count` for
        # dashboard attribution.
        self._phase_inverted_count: int = 0
        self._last_phase_warning_monotonic: float | None = None
        # #7 — format-detection probe state. ``_format_drift_count``
        # is cumulative over the lifetime of the normaliser;
        # ``_last_format_drift_warning_monotonic`` rate-limits the
        # WARN log per :data:`_FORMAT_DRIFT_LOG_INTERVAL_S`.
        self._format_drift_count: int = 0
        self._last_format_drift_warning_monotonic: float | None = None
        # Injectable clock for deterministic tests. Shape mirrors
        # ``time.monotonic``.
        self._monotonic: Callable[[], float] = time.monotonic

        # F5/F6 AGC2 — opt-in closed-loop digital gain controller.
        # Runs after _float_to_int16_saturate on the non-passthrough
        # path (where there's already DSP work). When None the
        # output is bit-identical to the pre-AGC2 behaviour.
        self._agc2: AGC2 | None = agc2

        # Phase 4 / T4.4 — Acoustic Echo Cancellation. Operates on
        # complete 512-sample windows AFTER rewindowing, before
        # emission. Stays None until an operator explicitly wires an
        # :class:`~sovyx.voice._aec.AecProcessor` (default lenient
        # per ``feedback_staged_adoption``). When present, the
        # ``render_provider`` is consulted for each emitted window —
        # see :meth:`_apply_aec_to_window`. Wire-up is bit-exact
        # passthrough whenever the provider returns silence (TTS
        # idle), so wiring AEC before the T4.4.b render-PCM capture
        # infra lands costs nothing in practice.
        self._aec: AecProcessor | None = aec
        self._render_provider: RenderPcmProvider | None = render_provider
        # Phase 4 / T4.9 — observability-only double-talk detector.
        # When wired, runs on every ``processed`` window and emits
        # ``voice.aec.double_talk{state}``. The freeze-the-AEC-filter
        # action is staged for a follow-up commit (Speex's pyaec
        # binding doesn't expose adaptation control); foundation
        # measures the NCC distribution so operators can calibrate
        # the threshold before the freeze action lands.
        self._double_talk_detector: DoubleTalkDetector | None = double_talk_detector
        # Phase 4 / T4.13 — Noise suppression. Operates on the
        # complete 512-sample window AFTER AEC and BEFORE emission
        # (the "AEC → NS" order matches Skype/Zoom/Teams VoIP
        # pipelines: AEC removes the echo first, NS attenuates the
        # background-noise residual on the cleaned signal). Default
        # ``None`` preserves the pre-NS contract bit-exactly.
        self._noise_suppressor: NoiseSuppressor | None = noise_suppressor
        # Phase 4 / T4.32 — SNR estimator. Observability-only:
        # called AFTER NS on the cleaned window, emits
        # ``voice.audio.snr_db`` per measurement, doesn't mutate
        # the signal. Default ``None`` preserves the pre-SNR
        # contract bit-exactly. The estimator's silent-frame
        # branch (returns _SNR_FLOOR_DB) suppresses the histogram
        # emission so silent windows don't pollute p50.
        self._snr_estimator: SnrEstimator | None = snr_estimator
        # Phase 4 / T4.43.b — TPDF dither for the float→int16
        # conversion. Off by default (foundation lenient per
        # ``feedback_staged_adoption``); when enabled, a single
        # ``np.random.Generator`` instance is reused across all
        # ``_float_to_int16_saturate`` calls so the dither
        # sequence is reproducible per FrameNormalizer lifetime.
        # Tests pass a seeded generator; production uses
        # ``np.random.default_rng()`` (system entropy).
        self._dither_enabled: bool = dither_enabled
        self._dither_amplitude_lsb: float = dither_amplitude_lsb
        if dither_enabled and dither_rng is None:
            import numpy as _np

            dither_rng = _np.random.default_rng()
        self._dither_rng: np.random.Generator | None = dither_rng

        logger.debug(
            "frame_normalizer_created",
            source_rate=source_rate,
            source_channels=source_channels,
            source_format=source_format,
            target_rate=_TARGET_RATE,
            target_window=_TARGET_WINDOW,
            passthrough=self._passthrough,
            up=self._up,
            down=self._down,
            ducking_ramp_samples=self._ducking_ramp_samples,
        )

    @property
    def source_rate(self) -> int:
        """Configured source sample rate in Hz."""
        return self._source_rate

    @property
    def source_channels(self) -> int:
        """Configured source channel count."""
        return self._source_channels

    @property
    def source_format(self) -> str:
        """Configured source sample format (int16 / int24 / float32)."""
        return self._source_format

    @property
    def is_passthrough(self) -> bool:
        """Whether the fast path is active (source already 16 kHz mono)."""
        return self._passthrough

    @property
    def target_rate(self) -> int:
        """Output sample rate in Hz (always 16 000)."""
        return _TARGET_RATE

    @property
    def target_window(self) -> int:
        """Output window size in samples (always 512)."""
        return _TARGET_WINDOW

    @property
    def ducking_gain_db(self) -> float:
        """Target mic-ducking gain in dB. ``0.0`` means no attenuation.

        Returns the *target* set by :meth:`set_ducking_gain_db`, not the
        instantaneous value mid-ramp. Use :attr:`current_ducking_gain_db`
        for the instantaneous value.
        """
        return _linear_to_db(self._target_linear_gain)

    @property
    def current_ducking_gain_db(self) -> float:
        """Instantaneous mic-ducking gain in dB (may differ from target mid-ramp)."""
        return _linear_to_db(self._current_linear_gain)

    def set_ducking_gain_db(self, gain_db: float) -> None:
        """Set the target mic-ducking gain (dB attenuation, ≤ 0).

        Per ADR §4.4.6.b, a ``-18 dB`` cut during TTS playback is the
        standard ducking level. Setting ``0 dB`` restores unity. Changes
        are transitioned linearly over ``ducking_ramp_ms`` to avoid
        clicks at the step edge.

        Setting the same value twice is a cheap no-op — it does NOT
        reset an in-progress ramp.

        Args:
            gain_db: Target gain in dB. Must be ``≤ 0`` (the stage is
                an *attenuator*, never an amplifier). ``float('-inf')``
                is accepted and collapses to linear gain ``0.0``.

        Raises:
            ValueError: If ``gain_db > 0``.
        """
        if gain_db > 0.0:
            msg = f"ducking gain must be <= 0 dB (attenuation only), got {gain_db}"
            raise ValueError(msg)

        target_linear = _db_to_linear(gain_db)
        if math.isclose(
            target_linear,
            self._target_linear_gain,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            return
        self._target_linear_gain = target_linear
        logger.debug(
            "frame_normalizer_ducking_target",
            gain_db=gain_db,
            target_linear=target_linear,
            current_linear=self._current_linear_gain,
        )

    def push(
        self,
        block: (npt.NDArray[np.int16] | npt.NDArray[np.int32] | npt.NDArray[np.float32]),
    ) -> list[npt.NDArray[np.int16]]:
        """Push one PortAudio callback block, get any complete 16 kHz windows back.

        Accepts either ``(N,)`` mono or ``(N, C)`` multichannel input.
        The block's dtype is validated against ``source_format``:

        - ``int16``: numpy ``int16`` (or ``float32`` in ``[-1, 1]`` for
          tests / loopback drivers).
        - ``int24``: numpy ``int32`` with 24-bit sign-extended payload.
        - ``float32``: numpy ``float32`` in ``[-1, 1]``.

        Internally everything is converted to ``float32`` in [-1, 1] for
        the resample / ducking stages, then back to ``int16`` before
        handoff to the pipeline (saturation clip per ADR §5.1).

        Args:
            block: Raw samples as delivered by the PortAudio callback.

        Returns:
            A list of 512-sample ``int16`` arrays at 16 kHz. Empty when
            the internal buffer doesn't yet hold a full window.

        Raises:
            ValueError: If ``block`` has an incompatible channel count
                or dtype relative to ``source_format``, or a rank other
                than 1 or 2.
        """
        import numpy as np

        if block.size == 0:
            # Pure no-op: PortAudio occasionally delivers zero-sized
            # callbacks at stream open / close boundaries. Skipping
            # telemetry here keeps the metric noise floor honest —
            # operators querying voice.capture.events expect them to
            # correlate with real audio work.
            return []

        # Ring 6 RED + USE: every push() invocation is one capture-stage
        # call. Wrap the entire body so even ValueError on bad
        # dtype/shape is captured with outcome=error via
        # measure_stage_duration's BaseException handler. SUCCESS
        # event fires once before return; window-emission rate is
        # available separately through the existing
        # lifetime_samples_processed counter.
        with measure_stage_duration(VoiceStage.CAPTURE):
            # Ultra-fast path: int16 mono 16 kHz with unity ducking.
            # This is the dominant case once the cascade settles on
            # the invariant format on good hardware. Skips the
            # float32 round-trip so the capture→VAD path is a memcpy.
            # R2 saturation counters do NOT apply here — int16 input
            # is already bounded, so by definition nothing can clip
            # in this branch.
            if (
                self._passthrough
                and self._source_format == "int16"
                and block.dtype == np.int16
                and self._target_linear_gain == 1.0
                and self._current_linear_gain == 1.0
            ):
                as_int16 = block if block.ndim == 1 else block[:, 0].copy()
            else:
                mono_f32 = self._downmix(block)
                resampled = mono_f32 if self._passthrough else self._resample(mono_f32)
                ducked = self._apply_ducking(resampled)
                # Phase 4 / T4.43.b — pass the dither rng when
                # enabled. Disabled path is bit-exact pre-T4.43
                # behaviour (the saturate fn short-circuits when
                # ``dither_rng is None``).
                as_int16, saturation = _float_to_int16_saturate(
                    ducked,
                    dither_rng=self._dither_rng if self._dither_enabled else None,
                    dither_amplitude_lsb=self._dither_amplitude_lsb,
                )
                self._record_saturation(saturation)
                # F5/F6: optional AGC2 post-process. Only on the
                # non-passthrough path because the passthrough path is
                # bit-exact by contract (operators rely on it for A/B
                # comparisons + golden-recording playback). When the
                # cascade negotiates 16 kHz mono int16, the user is
                # already operating at the target — AGC isn't needed.
                if self._agc2 is not None:
                    as_int16 = self._agc2.process(as_int16)

            self._output_buf = np.concatenate([self._output_buf, as_int16])

            windows: list[npt.NDArray[np.int16]] = []
            while len(self._output_buf) >= _TARGET_WINDOW:
                window = self._output_buf[:_TARGET_WINDOW].copy()
                self._output_buf = self._output_buf[_TARGET_WINDOW:]
                # Phase 4 / T4.4 — AEC runs on the complete 512-sample
                # window AFTER rewindowing so the Speex frame_size
                # matches the FrameNormalizer's _TARGET_WINDOW
                # invariant. Bit-exact passthrough when ``self._aec``
                # is None or the render provider returns silence
                # (NoOpAec / NullRenderProvider both reach this
                # passthrough branch in the foundation default
                # configuration).
                if self._aec is not None:
                    window = self._apply_aec_to_window(window)
                # Phase 4 / T4.13 — NS runs AFTER AEC on the same
                # 512-sample window. Bit-exact passthrough when
                # ``self._noise_suppressor`` is None (foundation
                # default per ``feedback_staged_adoption``) or when
                # the wired suppressor is :class:`NoOpNoiseSuppressor`.
                if self._noise_suppressor is not None:
                    window = self._apply_ns_to_window(window)
                # Phase 4 / T4.32 — SNR observation runs LAST on
                # the cleaned window so the metric reflects what
                # downstream stages (VAD/STT) actually see. The
                # estimator only emits a histogram sample for
                # measurable frames (silent frames return
                # _SNR_FLOOR_DB and are filtered out — see
                # _apply_snr_to_window).
                if self._snr_estimator is not None:
                    self._apply_snr_to_window(window)
                windows.append(window)

            record_stage_event(VoiceStage.CAPTURE, StageEventKind.SUCCESS)
            return windows

    def _apply_aec_to_window(
        self,
        window: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]:
        """Run one 512-sample capture window through the AEC stage.

        Pulls the time-aligned render reference from
        :attr:`_render_provider` (zeros when the provider is None or
        TTS is idle). The :class:`SpeexAecProcessor` short-circuits
        on silent reference frames so the early-return path is the
        common case until T4.4.b wires the playback PCM capture.

        T4.7 + T4.8 telemetry: emit
        :data:`sovyx.voice.aec.windows{state}` per window and
        :data:`sovyx.voice.aec.erle_db` per processed (non-silent
        render) window for the dashboard's AEC quality panel.
        """
        import numpy as np

        from sovyx.voice._aec import compute_erle
        from sovyx.voice.health._metrics import (
            record_aec_double_talk,
            record_aec_erle,
            record_aec_window,
        )

        if self._render_provider is not None:
            render_window = self._render_provider.get_aligned_window(_TARGET_WINDOW)
        else:
            render_window = np.zeros(_TARGET_WINDOW, dtype=np.int16)
        assert self._aec is not None  # called only when guarded above

        if not np.any(render_window):
            # Render reference silent → AEC short-circuits to the
            # passthrough branch. ERLE is undefined in this state
            # (no echo to measure); only the windows counter fires
            # so the dashboard can compute the processed/total ratio.
            cleaned = self._aec.process(window, render_window)
            record_aec_window(state="render_silent")
            if self._double_talk_detector is not None:
                # Render side silent → NCC undefined; emit
                # ``undecided`` so the dashboard symmetry between
                # voice.aec.windows + voice.aec.double_talk holds.
                record_aec_double_talk(state="undecided")
            return cleaned

        cleaned = self._aec.process(window, render_window)
        record_aec_window(state="processed")
        record_aec_erle(erle_db=compute_erle(render_window, window, cleaned))

        if self._double_talk_detector is not None:
            # T4.9 observability: NCC of the PRE-AEC capture window
            # against the render reference. Computed pre-AEC so the
            # detector sees the user's voice contribution before the
            # filter attenuates it; running on the cleaned signal
            # would make the NCC almost always low (post-AEC
            # capture is mostly residual noise, not echo).
            decision = self._double_talk_detector.analyze(render_window, window)
            if decision.ncc is None:
                record_aec_double_talk(state="undecided")
            elif decision.detected:
                record_aec_double_talk(state="detected")
            else:
                record_aec_double_talk(state="absent")

        return cleaned

    @property
    def aec(self) -> AecProcessor | None:
        """Currently-wired AEC processor, or ``None`` if disabled."""
        return self._aec

    def set_aec(self, aec: AecProcessor | None) -> None:
        """Wire (or unwire) the AEC processor at runtime.

        Mirrors :meth:`set_agc2` — operators / dashboards can flip
        AEC on/off without rebuilding the FrameNormalizer (which
        would lose the ducking ramp + output buffer + AGC2 state).
        Pass ``None`` to disable, a fresh processor instance to
        enable.
        """
        self._aec = aec

    def set_render_provider(self, provider: RenderPcmProvider | None) -> None:
        """Wire (or unwire) the render-PCM provider at runtime.

        The provider source is decoupled from the AEC processor so
        the playback path (T4.4.b — :mod:`sovyx.voice.pipeline._output_queue`)
        can register a concrete buffer once it lands without
        rebuilding the FrameNormalizer.
        """
        self._render_provider = provider

    def _apply_ns_to_window(
        self,
        window: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]:
        """Run one 512-sample window through the NS stage.

        T4.16 telemetry: emits
        :data:`sovyx.voice.ns.windows{state}` per window and
        :data:`sovyx.voice.ns.suppression_db` per processed window
        for the dashboard's NS quality panel.
        """
        from sovyx.voice._noise_suppression import estimate_frame_dbfs
        from sovyx.voice.health._metrics import (
            record_ns_suppression_db,
            record_ns_window,
        )

        assert self._noise_suppressor is not None  # gated by caller

        in_dbfs = estimate_frame_dbfs(window)
        cleaned = self._noise_suppressor.process(window)
        out_dbfs = estimate_frame_dbfs(cleaned)
        suppression_db = in_dbfs - out_dbfs

        # 0.5 dB threshold separates real attenuation from FFT
        # round-trip noise. Below this, NS effectively passed
        # through; firing 'processed' for sub-LSB drift would
        # poison the histogram p50 with floor-level noise.
        if suppression_db > 0.5:
            record_ns_window(state="processed")
            # Cap at +120 dB so a near-perfect-zero residual on
            # synthetic test inputs doesn't blow histogram buckets.
            record_ns_suppression_db(suppression_db=min(suppression_db, 120.0))
        else:
            record_ns_window(state="passthrough")

        return cleaned

    def _apply_snr_to_window(
        self,
        window: npt.NDArray[np.int16],
    ) -> None:
        """Compute + emit per-window SNR (T4.33 telemetry).

        Observability-only — does NOT mutate the audio signal.
        Skips emission for silent windows (estimator returns
        :data:`_SNR_FLOOR_DB`) and for the degenerate first-frame
        anchor (estimator returns 0.0 because the only sample IS
        the noise floor) so the histogram p50 isn't poisoned with
        synthetic floor values.
        """
        from sovyx.voice._snr_estimator import _SNR_FLOOR_DB
        from sovyx.voice.health._metrics import record_audio_snr_db

        assert self._snr_estimator is not None  # gated by caller
        snr_db = self._snr_estimator.estimate(window)
        # _SNR_FLOOR_DB signals "silent frame, no measurement".
        # 0.0 is the first-frame anchor (signal == noise floor by
        # construction); emitting it would distort the p50 with a
        # synthetic floor sample. Emit only real measurements.
        if snr_db <= _SNR_FLOOR_DB or snr_db == 0.0:
            return
        record_audio_snr_db(snr_db=snr_db)

    @property
    def noise_suppressor(self) -> NoiseSuppressor | None:
        """Currently-wired NS processor, or ``None`` if disabled."""
        return self._noise_suppressor

    def set_noise_suppressor(self, ns: NoiseSuppressor | None) -> None:
        """Wire (or unwire) the noise suppressor at runtime.

        Mirrors :meth:`set_aec` — operators / dashboards can flip
        NS on/off without rebuilding the FrameNormalizer (which
        would lose the ducking ramp + output buffer + AGC2 + AEC
        state). Pass ``None`` to disable, a fresh suppressor
        instance to enable.
        """
        self._noise_suppressor = ns

    @property
    def snr_estimator(self) -> SnrEstimator | None:
        """Currently-wired SNR estimator, or ``None`` if disabled."""
        return self._snr_estimator

    def set_snr_estimator(self, estimator: SnrEstimator | None) -> None:
        """Wire (or unwire) the SNR estimator at runtime.

        Mirrors :meth:`set_noise_suppressor`. Resetting an
        existing estimator's noise tracker requires calling
        :meth:`SnrEstimator.reset` directly — flipping in a new
        instance via this method effectively resets too because
        the new estimator starts with an empty noise window.
        """
        self._snr_estimator = estimator

    @property
    def agc2(self) -> AGC2 | None:
        """Currently-wired AGC2 controller, or ``None`` if disabled."""
        return self._agc2

    def set_agc2(self, agc2: AGC2 | None) -> None:
        """Wire (or unwire) the AGC2 post-processor at runtime.

        Enables operators / dashboards to enable AGC2 on a live
        capture stream without rebuilding the FrameNormalizer (which
        would lose the ducking ramp + output buffer). Pass ``None``
        to disable, or a fresh :class:`~sovyx.voice._agc2.AGC2`
        instance to enable. Replacing one AGC2 with another is
        permitted (keeps the new instance's adaptation state).
        """
        self._agc2 = agc2

    def reset(self) -> None:
        """Drop any buffered samples and collapse ducking ramp to target.

        Called on stream restart. The ducking *target* stays at whatever
        the caller last set, but the ramp state snaps to that target so
        the next block does not start at a stale mid-ramp gain.
        """
        import numpy as np

        self._output_buf = np.zeros(0, dtype=np.int16)
        self._current_linear_gain = self._target_linear_gain

    # ── R2: saturation feedback monitor public surface ─────────────────

    @property
    def lifetime_samples_processed(self) -> int:
        """Cumulative count of float samples passed through the saturate
        stage since construction. Survives :meth:`reset`. Excludes the
        ultra-fast int16 path (which can't clip)."""
        return self._lifetime_samples_processed

    @property
    def phase_inverted_count(self) -> int:
        """Cumulative count of stereo blocks where L/R Pearson
        correlation was below
        :data:`_PHASE_INVERSION_CORR_THRESHOLD` (band-aid #8). Each
        flagged block emits a rate-limited
        ``voice.audio.downmix_phase_inverted`` WARN (see
        :data:`_PHASE_INVERSION_LOG_INTERVAL_S`). Non-zero on a
        healthy mic = stereo channels are destructively correlated;
        average-downmix collapses to silence."""
        return self._phase_inverted_count

    @property
    def lifetime_samples_clipped(self) -> int:
        """Cumulative count of float samples that hit either int16 rail
        since construction. Survives :meth:`reset`. Foundation for the
        future Layer 4 AGC2 closed-loop gain controller, which reads
        this to recalibrate the upstream gain when sustained clipping
        is detected."""
        return self._lifetime_samples_clipped

    @property
    def lifetime_clipping_fraction(self) -> float:
        """``lifetime_samples_clipped / lifetime_samples_processed``.

        Returns 0.0 before any sample has been processed. This is the
        long-window signal — for the rolling short-window value used
        to gate warnings, see ``_window_*`` internals (intentionally
        not exposed; warnings are the public observability surface)."""
        if self._lifetime_samples_processed <= 0:
            return 0.0
        return self._lifetime_samples_clipped / self._lifetime_samples_processed

    def _record_saturation(self, counters: SaturationCounters) -> None:
        """Aggregate per-call counters into the rolling window monitor (R2).

        Called after every non-passthrough ``_float_to_int16_saturate``
        invocation. Updates lifetime + rolling counters, then evaluates
        the warning-emit gate:

        1. Sufficient samples accumulated in the window to be
           statistically meaningful (:data:`_SATURATION_MIN_SAMPLES_FOR_WARNING`).
        2. Window-fraction exceeds the warning threshold
           (:data:`_SATURATION_WARN_FRACTION`).
        3. At least :data:`_SATURATION_WINDOW_SECONDS` since the last
           warning emission (rate limiting).

        When all three pass, a structured ``voice.audio.saturation_clipping``
        event fires with both window and lifetime stats. The window
        counters then reset so the next window starts fresh.
        """
        # Empty counters from a zero-sized block — nothing to record.
        if counters.total_samples <= 0:
            return

        self._lifetime_samples_processed += counters.total_samples
        self._lifetime_samples_clipped += counters.clipped_total
        self._window_samples_processed += counters.total_samples
        self._window_samples_clipped += counters.clipped_total

        now = self._monotonic()
        if self._window_started_monotonic is None:
            self._window_started_monotonic = now

        window_age = now - self._window_started_monotonic
        window_fraction = (
            self._window_samples_clipped / self._window_samples_processed
            if self._window_samples_processed > 0
            else 0.0
        )

        # Rate-limit gate first — cheapest check, short-circuits the
        # rest when the window is still hot. ``None`` sentinel means
        # "no warning has fired yet" (distinct from monotonic clock = 0,
        # which is a valid time value tests use for determinism).
        if (
            self._last_warning_monotonic is not None
            and (now - self._last_warning_monotonic) < _SATURATION_WINDOW_SECONDS
        ):
            # Still inside the rate-limit window. If the rolling
            # accumulators have aged past one window, reset them so the
            # next eligible warning reflects fresh data, not stale carry.
            if window_age >= _SATURATION_WINDOW_SECONDS:
                self._window_samples_processed = 0
                self._window_samples_clipped = 0
                self._window_started_monotonic = now
            return

        if (
            self._window_samples_processed >= _SATURATION_MIN_SAMPLES_FOR_WARNING
            and window_fraction >= _SATURATION_WARN_FRACTION
        ):
            logger.warning(
                "voice.audio.saturation_clipping",
                **{
                    "voice.window_clipping_fraction": round(window_fraction, 4),
                    "voice.window_samples_processed": self._window_samples_processed,
                    "voice.window_samples_clipped": self._window_samples_clipped,
                    "voice.window_clipped_positive_fraction": round(
                        counters.clipped_positive / counters.total_samples,
                        4,
                    ),
                    "voice.window_clipped_negative_fraction": round(
                        counters.clipped_negative / counters.total_samples,
                        4,
                    ),
                    "voice.lifetime_clipping_fraction": round(
                        self.lifetime_clipping_fraction,
                        4,
                    ),
                    "voice.lifetime_samples_processed": self._lifetime_samples_processed,
                    "voice.lifetime_samples_clipped": self._lifetime_samples_clipped,
                    "voice.warning_threshold_fraction": _SATURATION_WARN_FRACTION,
                    "voice.action_required": ("reduce_upstream_gain_or_disable_capture_boost"),
                },
            )
            self._last_warning_monotonic = now
            self._window_samples_processed = 0
            self._window_samples_clipped = 0
            self._window_started_monotonic = now
            return

        # Window aged out without crossing the threshold — recycle so
        # the next window starts from a clean slate.
        if window_age >= _SATURATION_WINDOW_SECONDS:
            self._window_samples_processed = 0
            self._window_samples_clipped = 0
            self._window_started_monotonic = now

    def _downmix(
        self,
        block: (npt.NDArray[np.int16] | npt.NDArray[np.int32] | npt.NDArray[np.float32]),
    ) -> npt.NDArray[np.float32]:
        """Collapse ``block`` to mono ``float32`` samples in [-1, 1].

        Normalises to ``float32`` *before* channel averaging so integer
        arithmetic does not overflow on loud stereo pairs and so the
        downstream ``_float_to_int16_saturate`` step sees values already
        on the [-1, 1] scale regardless of source dtype.

        The scale factor comes from ``source_format``:

        - ``int16``: ``2**15 = 32 768``
        - ``int24`` (int32 payload): ``2**23 = 8 388 608``
        - ``float32``: identity (no scaling)
        """
        import numpy as np

        block_f: npt.NDArray[np.float32]
        if self._source_format == "int24":
            if block.dtype != np.int32:
                msg = (
                    f"int24 source requires numpy int32 blocks "
                    f"(sign-extended 24-bit payload), got dtype={block.dtype}"
                )
                raise ValueError(msg)
            block_f = (block.astype(np.float32) / _INT24_SCALE).astype(np.float32)
        elif self._source_format == "float32":
            # Mission #40 (Format Validation): pre-hardening this branch
            # silently coerced ANY dtype to float32 via ``np.asarray``.
            # A caller declaring ``source_format="float32"`` but actually
            # delivering int16 would produce a ~1000× amplified buffer
            # (int16 values like 16000 cast directly to float32 16000.0
            # instead of being scaled into [-1, 1]) — saturation would
            # clip the output and R2 would surface the clipping, but the
            # root-cause "you said float32 but sent int16" would never
            # surface explicitly. The strict dtype check makes the
            # contract violation loud (ValueError) so the caller fixes
            # the source rather than chasing downstream symptoms.
            if block.dtype != np.float32:
                msg = (
                    f"float32 source requires numpy float32 blocks in [-1, 1], "
                    f"got dtype={block.dtype} — declare the correct source_format "
                    f"(int16 / int24) or scale the input before push()"
                )
                raise ValueError(msg)
            # Cast narrows the numpy-stubs union (int16 | int32 | float32)
            # back to the NDArray[float32] we already know astype produces.
            block_f = typing.cast("npt.NDArray[np.float32]", block)
        else:  # int16
            if block.dtype == np.int16:
                block_f = (block.astype(np.float32) / _INT16_SCALE).astype(np.float32)
            elif block.dtype == np.float32:
                # Tolerated for test-suite / loopback callers that
                # already hand [-1, 1] float32 against an int16-declared
                # source. Treated as already on the target scale.
                block_f = typing.cast("npt.NDArray[np.float32]", block)
            else:
                msg = (
                    f"int16 source expects numpy int16 (or float32 in [-1, 1]) "
                    f"blocks, got dtype={block.dtype}"
                )
                raise ValueError(msg)

        # #7 — format-detection probe (extends #40 dtype check).
        # Runs BEFORE the rank dispatch so both 1-D and 2-D paths
        # benefit from the probe; the channel-layout strict check
        # only applies to 2-D blocks (a 1-D block is unambiguously
        # mono and matches any source_channels declaration).
        if self._source_format == "float32" and block_f.size > 0:
            self._maybe_flag_format_drift(block_f)
        if block_f.ndim == 2 and block_f.shape[1] != self._source_channels:
            msg = (
                f"block channel layout drift — declared source_channels="
                f"{self._source_channels} but block has shape={block_f.shape} "
                f"(rank-2 columns={block_f.shape[1]}). Either the cascade "
                f"negotiated the wrong channel count or the device started "
                f"delivering a different layout mid-stream"
            )
            raise ValueError(msg)

        if block_f.ndim == 1:
            return block_f
        if block_f.ndim == 2:
            if self._source_channels == 1:
                out: npt.NDArray[np.float32] = block_f[:, 0].astype(np.float32)
                return out
            # Band-aid #8: phase-inversion check on stereo downmix.
            # Only the 2-channel case is meaningful for the
            # destructive-correlation failure mode (a 6-channel
            # surround source mixed to mono can have any spatial
            # relationship; the L/R-only case is the silent-
            # cancellation pathology this guard exists to flag).
            if block_f.shape[1] == 2:
                self._maybe_flag_phase_inversion(
                    left=block_f[:, 0],
                    right=block_f[:, 1],
                )
            avg: npt.NDArray[np.float32] = block_f.mean(axis=1).astype(np.float32)
            return avg
        msg = f"block must be 1-D or 2-D, got ndim={block_f.ndim}"
        raise ValueError(msg)

    def _maybe_flag_phase_inversion(
        self,
        *,
        left: npt.NDArray[np.float32],
        right: npt.NDArray[np.float32],
    ) -> None:
        """Compute L/R correlation; emit rate-limited WARN if inverted.

        Pure observability — does NOT alter the downmix output. The
        WARN is the recovery signal for the operator (consider
        switching to L-only or R-only via a future opt-in toggle).
        """
        correlation = _channel_correlation(left, right)
        if correlation > _PHASE_INVERSION_CORR_THRESHOLD:
            return
        # Below threshold → flag as phase-inverted. Bump cumulative
        # counter unconditionally; rate-limit the WARN log.
        self._phase_inverted_count += 1
        now = self._monotonic()
        last = self._last_phase_warning_monotonic
        if last is not None and (now - last) < _PHASE_INVERSION_LOG_INTERVAL_S:
            return
        self._last_phase_warning_monotonic = now
        logger.warning(
            "voice.audio.downmix_phase_inverted",
            **{
                "voice.correlation": round(correlation, 4),
                "voice.threshold": _PHASE_INVERSION_CORR_THRESHOLD,
                "voice.lifetime_inversion_count": self._phase_inverted_count,
                "voice.action_required": (
                    "stereo channels are destructively correlated — "
                    "average-downmix collapses to silence; consider "
                    "switching to a single-channel source or sum-instead-"
                    "of-average downmix"
                ),
            },
        )

    def _maybe_flag_format_drift(
        self,
        block_f: npt.NDArray[np.float32],
    ) -> None:
        """#7 — float32-magnitude probe; emit rate-limited WARN on drift.

        Pure observability — does NOT alter the downstream output.
        The WARN points the operator at upstream format negotiation
        (PortAudio host adapter, capture device callback) rather
        than the saturation symptom in R2 logs.
        """
        import numpy as np

        max_abs = float(np.max(np.abs(block_f))) if block_f.size > 0 else 0.0
        if max_abs <= _FLOAT32_MAGNITUDE_PROBE_THRESHOLD:
            return
        # Above threshold → flag as format drift. Bump cumulative
        # counter unconditionally; rate-limit the WARN log.
        self._format_drift_count += 1
        now = self._monotonic()
        last = self._last_format_drift_warning_monotonic
        if last is not None and (now - last) < _FORMAT_DRIFT_LOG_INTERVAL_S:
            return
        self._last_format_drift_warning_monotonic = now
        logger.warning(
            "voice.audio.format_drift",
            **{
                "voice.declared_format": self._source_format,
                "voice.observed_max_abs": round(max_abs, 4),
                "voice.threshold": _FLOAT32_MAGNITUDE_PROBE_THRESHOLD,
                "voice.lifetime_drift_count": self._format_drift_count,
                "voice.action_required": (
                    f"declared source_format='float32' but block "
                    f"max(|x|)={max_abs:.1f} >> 1.0 — likely int16-"
                    f"magnitude data tagged as float32 upstream. "
                    f"Audit the PortAudio host-adapter format conversion "
                    f"OR re-declare source_format='int16' if the device "
                    f"actually delivers int16"
                ),
            },
        )

    @property
    def format_drift_count(self) -> int:
        """Lifetime counter of format-drift detections (#7).

        Incremented every time :meth:`_maybe_flag_format_drift` sees a
        float32 block whose ``max(|x|)`` exceeds the probe threshold.
        Exposed for dashboard attribution + integration tests; survives
        :meth:`reset` because it describes the data path's lifetime
        format-negotiation health."""
        return self._format_drift_count

    def _resample(
        self,
        mono: npt.NDArray[np.float32],
    ) -> npt.NDArray[np.float32]:
        """Polyphase resample to 16 kHz using scipy."""
        from scipy.signal import resample_poly

        out = resample_poly(mono, self._up, self._down).astype("float32")
        return out  # type: ignore[no-any-return]

    def _apply_ducking(
        self,
        samples: npt.NDArray[np.float32],
    ) -> npt.NDArray[np.float32]:
        """Apply mic-ducking gain with a linear ramp toward the target.

        No-op when both ``current`` and ``target`` are unity (the common
        case: TTS not playing). When the caller has set a non-unity
        target, the ramp proceeds at a fixed step of
        ``(target - current) / ducking_ramp_samples`` per output sample
        until ``current`` reaches ``target``; then the gain stays flat.

        Returns a fresh array. Input is not mutated.
        """
        import numpy as np

        n = len(samples)
        if n == 0:
            return samples

        if self._current_linear_gain == self._target_linear_gain:
            if self._current_linear_gain == 1.0:
                return samples
            return samples * np.float32(self._current_linear_gain)

        step = (self._target_linear_gain - self._current_linear_gain) / self._ducking_ramp_samples
        indices = np.arange(1, n + 1, dtype=np.float32)
        envelope = np.float32(self._current_linear_gain) + np.float32(step) * indices

        low = min(self._current_linear_gain, self._target_linear_gain)
        high = max(self._current_linear_gain, self._target_linear_gain)
        envelope = np.clip(envelope, low, high)

        self._current_linear_gain = float(envelope[-1])
        ducked: npt.NDArray[np.float32] = (samples * envelope).astype(np.float32, copy=False)
        return ducked


def _float_to_int16_saturate(
    samples: npt.NDArray[np.float32],
    *,
    dither_rng: np.random.Generator | None = None,
    dither_amplitude_lsb: float = 1.0,
) -> tuple[npt.NDArray[np.int16], SaturationCounters]:
    """Convert ``float32`` in [-1, 1] to ``int16`` with saturation clip.

    Per ADR §5.1 ("int24/float32 → int16 via saturation clip"). Loud
    transients near ±1.0 would otherwise wrap to ``INT16_MIN`` (which
    Silero reads as a non-physical impulse).

    R2: returns a :class:`SaturationCounters` describing how many input
    samples hit the +full-scale and -full-scale rails. The caller is
    responsible for aggregating across calls if it wants a rolling
    fraction; :class:`FrameNormalizer` does this and emits a structured
    ``voice.audio.saturation_clipping`` event when the fraction crosses
    the warning threshold. Pre-R2 callers that ignored the saturation
    silently — and the band-aid that produced — are now extinct.

    Phase 4 / T4.43.b — when ``dither_rng`` is supplied, TPDF dither
    is added to the scaled signal BEFORE the clip + cast. The dither
    decorrelates the quantization error from the signal, eliminating
    harmonic distortion on quiet sustained tones at the cost of
    +4.77 dB of broadband noise (canonical TPDF dither penalty).
    Saturation counting still works post-dither — a dithered sample
    pushed over the rail by the +1 LSB noise legitimately clipped.

    The function is pure (no side effects, no instance state): the
    counter logic that drives observability lives in the aggregator,
    not here, so the function stays trivially testable in isolation.

    NaN inputs are clamped by ``np.clip`` to neither rail (NaN
    comparisons return False), but the surrounding cast to int16 then
    produces an undefined value. This function does NOT NaN-guard the
    input; that contract is enforced upstream (Ring 2 signal integrity)
    so the saturation stage doesn't need to duplicate it.
    """
    import numpy as np

    if samples.size == 0:
        # Avoid the overhead of np.sum on an empty array — and also
        # produce a deterministic empty-counters snapshot the caller
        # can safely accumulate without special-casing.
        return np.zeros(0, dtype=np.int16), SaturationCounters(
            total_samples=0,
            clipped_positive=0,
            clipped_negative=0,
        )

    scaled = samples * 32768.0
    if dither_rng is not None:
        # Inline TPDF dither: avoids the import overhead in the
        # zero-dither hot path. The same algebra as
        # :func:`sovyx.voice._dither.tpdf_noise` — sum of two
        # uniforms, scaled to int16-LSB amplitude, added to the
        # already-at-int16-magnitude ``scaled`` array.
        u1 = dither_rng.random(scaled.size, dtype=np.float64)
        u2 = dither_rng.random(scaled.size, dtype=np.float64)
        noise = (u1 - u2) * dither_amplitude_lsb
        scaled = scaled.astype(np.float64) + noise.reshape(scaled.shape)

    # Count BEFORE the clamp — these are the values that would have
    # wrapped if the clip wasn't there. Counting after the clamp would
    # always yield zero (every sample is in-range post-clip).
    clipped_positive = int(np.sum(scaled > 32767.0))
    clipped_negative = int(np.sum(scaled < -32768.0))
    clipped = np.clip(scaled, -32768.0, 32767.0)
    out: npt.NDArray[np.int16] = clipped.astype(np.int16)
    return out, SaturationCounters(
        total_samples=int(samples.size),
        clipped_positive=clipped_positive,
        clipped_negative=clipped_negative,
    )


def _db_to_linear(db: float) -> float:
    """Convert dB to linear gain. ``-inf dB → 0.0``. ``0 dB → 1.0``."""
    if db == float("-inf"):
        return 0.0
    return float(10.0 ** (db / 20.0))


def _linear_to_db(linear: float) -> float:
    """Convert linear gain to dB. ``0.0 → -inf``. ``1.0 → 0.0``."""
    if linear <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(linear)


def _channel_correlation(
    left: npt.NDArray[np.float32],
    right: npt.NDArray[np.float32],
) -> float:
    """Pearson correlation between two equal-length channel buffers.

    Pure function. Returns ``r ∈ [-1, 1]`` where:

    * ``+1.0`` — channels are identical (mono played on both speakers).
    * ``0.0`` — channels are uncorrelated (true stereo separation).
    * ``-1.0`` — channels are exact inverses (phase-flipped). Average-
      downmix collapses these to silence.

    Returns ``0.0`` when either channel's RMS is below
    :data:`_PHASE_INVERSION_MIN_RMS_FOR_CHECK` — Pearson correlation
    is undefined for the zero-signal case (denominator is zero), and
    "no signal" is the safe interpretation (no inversion to flag).

    Args:
        left: First channel, ``float32`` in ``[-1, 1]``.
        right: Second channel, same shape + dtype as ``left``.

    Raises:
        ValueError: ``left`` and ``right`` have different shapes.
    """
    import numpy as np

    if left.shape != right.shape:
        msg = f"left/right shape mismatch: left={left.shape}, right={right.shape}"
        raise ValueError(msg)
    if left.size == 0:
        return 0.0
    # Cast to float64 for the dot products — float32 can lose
    # precision on long buffers + the correlation is a single
    # scalar so the cost is negligible.
    left_f64 = left.astype(np.float64)
    right_f64 = right.astype(np.float64)
    rms_l = float(np.sqrt(np.mean(left_f64 * left_f64)))
    rms_r = float(np.sqrt(np.mean(right_f64 * right_f64)))
    if rms_l < _PHASE_INVERSION_MIN_RMS_FOR_CHECK:
        return 0.0
    if rms_r < _PHASE_INVERSION_MIN_RMS_FOR_CHECK:
        return 0.0
    # Pearson correlation: dot / (||L|| * ||R||) where ||.|| is L2.
    # Equivalent to mean(L*R) / (rms_l * rms_r) for the sample case.
    cov = float(np.mean(left_f64 * right_f64))
    correlation = cov / (rms_l * rms_r)
    # Clamp to [-1, 1] — float arithmetic can drift slightly past
    # the bounds for nearly-perfect correlation.
    return max(-1.0, min(1.0, correlation))


__all__ = ["FrameNormalizer", "SaturationCounters"]
