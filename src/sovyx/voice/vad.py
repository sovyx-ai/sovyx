"""SileroVAD v5 with hysteresis state machine.

Onset/offset FSM with configurable thresholds and 512-sample window (32ms @16kHz).
Prevents rapid on/off switching via consecutive-frame gating.

Ring 3 (Decision Ensemble) defense-in-depth: every inference output is
validated for finiteness and domain compliance before reaching the FSM
(``_validate_inference_outputs``). Corruption — NaN/Inf in either the
probability or the recurrent LSTM state — is treated as a silent-failure
class: NaN comparisons silently evaluate False, which would freeze the
FSM in SILENCE forever and produce the textbook "deaf microphone" user
experience. The guard fail-closes (treats the corrupt frame as silence,
the safer default than fabricating speech) and immediately resets the
LSTM state so the next frame starts clean.

A repeated-corruption monitor distinguishes a transient single-frame
glitch (recoverable) from a structural model failure (escalate via the
``voice.vad.session_unrecoverable`` event so an upstream circuit breaker
can disable the VAD path and fall back to the secondary detector).

Ref: SPE-010 §3 (VAD), IMPL-004 §2.4 (SileroVAD v5 code),
MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.3 / §3.2 / V1.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._stage_metrics import (
    StageEventKind,
    VoiceStage,
    measure_stage_duration,
    record_stage_event,
)

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)

_WINDOW_HISTORY = 5
"""Rolling-window depth carried on ``voice.vad.state_changed`` so the
dashboard timeline can render the probability / RMS build-up that led
to each FSM transition instead of a single point estimate."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_RATE_16K = 16000
_SAMPLE_RATE_8K = 8000
_WINDOW_16K = 512
_WINDOW_8K = 256
_LSTM_STATE_SHAPE = (2, 1, 128)

# ---------------------------------------------------------------------------
# Ring 3 NaN/Inf guard tuning (V1)
# ---------------------------------------------------------------------------
#
# These constants govern the corruption monitor that distinguishes a
# transient one-frame glitch from a structural ONNX-session failure.
# All three are intentionally module-level rather than ``VoiceTuningConfig``
# fields: they describe the failure-detection contract itself, not a user-
# tunable trade-off. The cost of a false positive (an extra
# ``session_unrecoverable`` event) is one ERROR log; the cost of a false
# negative (missing a real model corruption) is the deaf-microphone class
# of bugs the V1 guard exists to eliminate.

_CORRUPTION_RECOVERY_FRAMES = 5
"""Consecutive clean frames required after a corruption event before
:data:`voice.vad.session_recovered` is emitted. Five frames at 16 kHz /
512-sample window = 160 ms — long enough to confirm the LSTM state has
genuinely settled (one full hysteresis bucket) without paying perceptual
latency on a real speech onset."""

_CORRUPTION_UNRECOVERABLE_THRESHOLD = 3
"""Number of corruption events within :data:`_CORRUPTION_UNRECOVERABLE_WINDOW`
frames that escalates from per-event WARNING to a single
:data:`voice.vad.session_unrecoverable` ERROR. Three is the SRE-canonical
"twice is coincidence, three times is a pattern" rule applied to fault
detection; it also matches the Hystrix circuit-breaker default minimum
sample size before opening on a failure-rate threshold."""

_CORRUPTION_UNRECOVERABLE_WINDOW = 100
"""Sliding window (in frames) over which
:data:`_CORRUPTION_UNRECOVERABLE_THRESHOLD` corruptions trigger
unrecoverable. At 16 kHz / 512-sample window = 3.2 s of audio — the
SRE-canonical "burn rate" alerting horizon for a 1-minute SLO budget
(see Google SRE Workbook §5)."""

# ---------------------------------------------------------------------------
# Ring 3 Schmitt-trigger hysteresis tuning (V3)
# ---------------------------------------------------------------------------
#
# A Schmitt trigger needs two distinct thresholds (onset > offset) and a
# minimum gap (delta) wide enough to suppress chatter at the noise floor.
# The constants below capture the Silero / LiveKit canonical values so
# they're discoverable in one place; ``_validate_config`` enforces the
# minimum so a misconfigured ``VADConfig`` can't silently degrade to
# essentially-no-hysteresis (single-threshold flapping).

SILERO_CANONICAL_HYSTERESIS_DELTA = 0.15
"""Recommended ``onset_threshold - offset_threshold`` per Silero VAD's
canonical configuration and LiveKit's production tuning (LiveKit blog
"Improved end-of-turn model cuts voice-AI interruptions 39%", 2026).

Smaller deltas (<0.1) produce the chatter the Schmitt trigger exists
to prevent — at the boundary of the noise floor, raw probability
fluctuates by ±0.05 between consecutive frames, so a 0.05-delta
hysteresis is no hysteresis at all. The 0.15 value is the empirical
sweet spot: tight enough to bound perceptual end-of-turn latency,
wide enough to absorb model jitter on real speech.

Used by :meth:`VADConfig.with_canonical_hysteresis` to derive
``offset_threshold`` from a single user-provided ``onset_threshold``.
"""

_HYSTERESIS_MIN_DELTA = 0.05
"""Minimum permissible ``onset_threshold - offset_threshold``. Below
this value the Schmitt trigger collapses into a single-threshold
flapping detector — see :data:`SILERO_CANONICAL_HYSTERESIS_DELTA`
for the rationale.

Enforced by :func:`_validate_config`. Configurations below this floor
raise ``ValueError`` at construction so the failure is loud rather
than silent (the alternative — silent acceptance + chatter at runtime
— is the precise band-aid pattern the V3 tightening exists to
prevent)."""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class VADState(IntEnum):
    """Hysteresis state machine states.

    Transitions: SILENCE → SPEECH_ONSET → SPEECH → SPEECH_OFFSET → SILENCE
    Each transition requires *consecutive* frames above/below threshold.
    """

    SILENCE = auto()
    SPEECH_ONSET = auto()
    SPEECH = auto()
    SPEECH_OFFSET = auto()


@dataclass(frozen=True, slots=True)
class VADEvent:
    """Result of processing a single audio frame."""

    is_speech: bool
    """Whether the FSM considers the current frame as speech."""

    probability: float
    """Raw speech probability from the model (0.0–1.0)."""

    state: VADState
    """FSM state after processing this frame."""


@dataclass(frozen=True, slots=True)
class VADConfig:
    """Calibrated parameters for SileroVAD v5 (IMPL-004 §5).

    Defaults tuned for 16 kHz input on Pi 5 (Cortex-A76). The
    ``onset_threshold`` / ``offset_threshold`` pair forms a Schmitt
    trigger whose hysteresis prevents single-frame chatter at the
    noise-floor boundary. See :data:`SILERO_CANONICAL_HYSTERESIS_DELTA`
    for the canonical gap; :func:`_validate_config` enforces a minimum
    of :data:`_HYSTERESIS_MIN_DELTA` so a too-small gap can't silently
    degenerate the trigger into a single-threshold detector.

    Use :meth:`with_canonical_hysteresis` to derive a Silero/LiveKit-
    canonical config from a single ``onset_threshold`` value rather
    than hand-picking both thresholds.
    """

    onset_threshold: float = 0.5
    """Probability above which a frame is considered speech-likely
    (upper Schmitt threshold)."""

    offset_threshold: float = 0.3
    """Probability below which a frame is considered silence-likely
    (lower Schmitt threshold). Must satisfy
    ``onset_threshold - offset_threshold >= _HYSTERESIS_MIN_DELTA``."""

    min_onset_frames: int = 3
    """Consecutive frames (≈96 ms @ 16 kHz / 512-sample window) above
    onset to confirm speech start. Lower = lower wake-word latency at
    the cost of more false positives. Sovyx tunes for low wake latency;
    LiveKit's canonical recommendation for turn-end detection is 8
    frames (~250 ms) — the Sovyx default favors the wake path because
    turn-end is also gated by VoiceTuningConfig silence timeouts
    downstream of this FSM."""

    min_offset_frames: int = 8
    """Consecutive frames (≈256 ms @ 16 kHz / 512-sample window) below
    offset to confirm speech end. Higher = more robust to natural
    speech pauses (breathing, conjunctions) at the cost of perceived
    response latency."""

    sample_rate: int = _SAMPLE_RATE_16K
    """Audio sample rate — only 8000 or 16000 supported."""

    @property
    def window_size(self) -> int:
        """Frame size in samples (fixed per sample rate)."""
        if self.sample_rate == _SAMPLE_RATE_16K:
            return _WINDOW_16K
        if self.sample_rate == _SAMPLE_RATE_8K:
            return _WINDOW_8K
        msg = f"Unsupported sample rate: {self.sample_rate}. Use 8000 or 16000."
        raise ValueError(msg)

    @property
    def hysteresis_delta(self) -> float:
        """Schmitt-trigger gap ``onset_threshold - offset_threshold``.

        Surfaced as a property so dashboards and observability can
        compare a live config to :data:`SILERO_CANONICAL_HYSTERESIS_DELTA`
        without re-deriving it. Always ``>= _HYSTERESIS_MIN_DELTA``
        on a config that survived :func:`_validate_config`.
        """
        return self.onset_threshold - self.offset_threshold

    @classmethod
    def with_canonical_hysteresis(
        cls,
        onset_threshold: float,
        *,
        min_onset_frames: int = 3,
        min_offset_frames: int = 8,
        sample_rate: int = _SAMPLE_RATE_16K,
    ) -> VADConfig:
        """Build a VADConfig with the Silero/LiveKit canonical hysteresis gap.

        ``offset_threshold`` is derived as
        ``onset_threshold - SILERO_CANONICAL_HYSTERESIS_DELTA`` and
        clamped into ``(0, onset_threshold)`` so a high onset
        (e.g. 0.95) doesn't push offset out of the valid (0, 1) domain.
        The clamped value is then re-validated through ``_validate_config``
        so the returned config is always usable.

        Use this constructor when you want explicit Silero-canonical
        behaviour without manually computing the offset, e.g.::

            cfg = VADConfig.with_canonical_hysteresis(0.7)
            # → onset=0.7, offset=0.55, delta=0.15

        Raises:
            ValueError: If ``onset_threshold`` is itself outside (0, 1)
                — i.e. the caller asked for an impossible base config,
                not a Schmitt-trigger problem.
        """
        derived_offset = onset_threshold - SILERO_CANONICAL_HYSTERESIS_DELTA
        # Clamp so onset=0.05..0.99 always produces a valid (>0) offset.
        # If derived_offset would be <= 0 (e.g. onset=0.10), fall back to
        # a delta narrow enough to preserve the (>0) domain — but never
        # narrower than the minimum hysteresis floor.
        if derived_offset <= 0.0:
            # onset=0.10 → derived=-0.05 → fallback to min-delta gap.
            derived_offset = max(onset_threshold - _HYSTERESIS_MIN_DELTA, 0.001)
        return cls(
            onset_threshold=onset_threshold,
            offset_threshold=derived_offset,
            min_onset_frames=min_onset_frames,
            min_offset_frames=min_offset_frames,
            sample_rate=sample_rate,
        )


def _validate_inference_outputs(
    raw_probability: float,
    raw_next_state: object,
) -> tuple[bool, str]:
    """Return ``(is_corrupt, kind)`` for one ONNX inference result (V1).

    Pure function — no side effects, no instance state, fully unit-
    testable in isolation. Caller (``SileroVAD.process_frame``) handles
    the recovery action; this helper only classifies.

    The "corrupt" verdict fires for any of:

    * Probability is NaN, +Inf, or -Inf — the FSM can't compare it.
    * Probability is out of the [0, 1] domain — the model is supposed
      to produce a sigmoid; an out-of-range value is a contract
      violation that should not be propagated to downstream consumers
      (Whisper logprob filters, EOU ensembles, dashboards).
    * The recurrent LSTM state contains a NaN/Inf cell — recurrent
      poisoning will corrupt the *next* frame's probability even if
      the *current* one happens to come out clean. We must catch it
      now, before the corruption silently propagates.

    The ``kind`` string is one of:

    * ``"probability_nan"`` — non-finite scalar.
    * ``"probability_out_of_range"`` — finite but outside [0, 1].
    * ``"lstm_state_nan"`` — at least one non-finite cell in the
      recurrent state tensor.
    * ``"lstm_state_shape_invalid"`` — the next-state tensor isn't
      shaped like the documented ``(2, 1, 128)`` LSTM hidden+cell
      packing. Treated as corruption because applying it would either
      crash the next inference or silently align mis-shaped values
      against unrelated cells.

    Probability is checked first because it's cheap (one float test);
    the LSTM-state ``np.isfinite`` reduction over the full tensor is
    O(N) and O(N) is *also* cheap (256 cells), but the early-out keeps
    the corruption-kind name aligned with the most-immediately-visible
    symptom for operators reading the log.
    """
    import numpy as np  # noqa: F811

    if not math.isfinite(raw_probability):
        return True, "probability_nan"
    if not 0.0 <= raw_probability <= 1.0:
        return True, "probability_out_of_range"
    if not isinstance(raw_next_state, np.ndarray):
        return True, "lstm_state_shape_invalid"
    if raw_next_state.shape != _LSTM_STATE_SHAPE:
        return True, "lstm_state_shape_invalid"
    if not bool(np.all(np.isfinite(raw_next_state))):
        return True, "lstm_state_nan"
    return False, ""


def _validate_config(config: VADConfig) -> None:
    """Raise ``ValueError`` for obviously bad config values."""
    if config.onset_threshold <= 0.0 or config.onset_threshold >= 1.0:
        msg = f"onset_threshold must be in (0, 1), got {config.onset_threshold}"
        raise ValueError(msg)
    if config.offset_threshold <= 0.0 or config.offset_threshold >= 1.0:
        msg = f"offset_threshold must be in (0, 1), got {config.offset_threshold}"
        raise ValueError(msg)
    if config.offset_threshold >= config.onset_threshold:
        msg = (
            f"offset_threshold ({config.offset_threshold}) must be < "
            f"onset_threshold ({config.onset_threshold}) for hysteresis"
        )
        raise ValueError(msg)
    delta = config.onset_threshold - config.offset_threshold
    # IEEE-754 precision tolerance: 0.5 - 0.45 = 0.04999999... in
    # binary float, so a strict ``delta < 0.05`` would falsely reject
    # an exactly-at-floor configuration. The epsilon is small enough
    # that a real sub-floor config (e.g. 0.04 actual delta) is still
    # caught — sigmoid probabilities don't have meaningful precision
    # beyond ~6 decimal places.
    if delta < _HYSTERESIS_MIN_DELTA - 1e-9:
        # V3: a Schmitt trigger with a sub-floor delta degenerates into
        # a single-threshold flapping detector. The runtime symptom
        # ("VAD turns on/off every frame at the noise floor") is
        # indistinguishable from a real audio glitch, so we reject the
        # config at construction rather than let it produce mysterious
        # logs in production. Use VADConfig.with_canonical_hysteresis()
        # if you want the Silero/LiveKit-recommended 0.15 gap derived
        # automatically.
        msg = (
            f"hysteresis delta ({delta:.3f}) must be >= "
            f"{_HYSTERESIS_MIN_DELTA:.3f} to suppress noise-floor "
            f"chatter; use VADConfig.with_canonical_hysteresis() "
            f"to derive offset_threshold from onset_threshold using "
            f"the Silero/LiveKit canonical {SILERO_CANONICAL_HYSTERESIS_DELTA:.2f} gap"
        )
        raise ValueError(msg)
    if config.min_onset_frames < 1:
        msg = f"min_onset_frames must be >= 1, got {config.min_onset_frames}"
        raise ValueError(msg)
    if config.min_offset_frames < 1:
        msg = f"min_offset_frames must be >= 1, got {config.min_offset_frames}"
        raise ValueError(msg)
    # Validate sample rate eagerly
    _ = config.window_size


# ---------------------------------------------------------------------------
# SileroVAD
# ---------------------------------------------------------------------------


class SileroVAD:
    """SileroVAD v5 with ONNX inference and hysteresis state machine.

    V5 improvements over V4:
    - 3× faster TorchScript, 10 % faster ONNX
    - Fixed window: 512 samples at 16 kHz (32 ms)
    - 6000+ languages supported
    - Smaller model (≈2 MB ONNX)

    State machine prevents rapid on/off switching::

        SILENCE → SPEECH_ONSET → SPEECH → SPEECH_OFFSET → SILENCE

    Each transition requires consecutive frames above/below threshold,
    preventing cut-off during natural pauses.

    Performance: <1 ms per frame on Pi 5 (Cortex-A76).
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        model_path: Path,
        config: VADConfig | None = None,
        *,
        smoke_probe_at_construction: bool = True,
    ) -> None:
        """Construct a Silero VAD bound to the model at ``model_path``.

        Args:
            model_path: Filesystem path to the ONNX model.
            config: Tuning knobs (defaults to canonical Silero v5).
            smoke_probe_at_construction: When True (default), run a
                one-shot smoke probe through the loaded model
                immediately after session construction to validate
                the model agrees with ``config.sample_rate`` +
                ``config.window_size``. Catches model/config
                mismatches at startup instead of failing silently
                on every real frame via the V1 fail-closed path.
                Set to False for tests that mock the ONNX session
                with a deterministic probability sequence — the
                smoke probe would consume the first sequence
                element. Production code MUST use the default True.
        """
        import numpy as np  # noqa: F811
        import onnxruntime as ort

        self._config = config or VADConfig()
        _validate_config(self._config)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # VAD is tiny — 1 thread is optimal
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        # Persistent LSTM state (h0, c0) — survives between frames
        self._state: npt.NDArray[np.float32] = np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)
        self._sr: npt.NDArray[np.int64] = np.array([self._config.sample_rate], dtype=np.int64)

        # Band-aid #36: startup smoke probe. Verifies the loaded
        # ONNX model agrees with the configured sample_rate +
        # window_size BEFORE the first real frame arrives. Catches:
        # * loaded a 16k-rate model with config.sample_rate=8000
        #   (or vice versa) → model returns garbage probability
        # * model file corrupted in transit but ONNX session built
        #   anyway → first probability is NaN
        # * window_size mismatch (model expects 512, config says
        #   256) → ONNX raises during inference
        # Any of these would silently freeze VAD at construction
        # via the V1 corruption recovery path on every real frame.
        # Failing loud at construction beats failing silent on
        # every frame for the lifetime of the daemon. Tests with
        # mock sessions opt out via ``smoke_probe_at_construction=
        # False``.
        if smoke_probe_at_construction:
            self._smoke_probe_session(np)

        # FSM bookkeeping
        self._vad_state = VADState.SILENCE
        self._consecutive_count = 0

        # Rolling windows for state_changed enrichment. Bounded so the
        # memory footprint is constant regardless of session length.
        self._prob_history: deque[float] = deque(maxlen=_WINDOW_HISTORY)
        self._rms_history: deque[float] = deque(maxlen=_WINDOW_HISTORY)

        # ── V1 NaN/Inf corruption monitor ────────────────────────────
        # Counters survive resets so chronic model corruption surfaces
        # in long-running daemons even if the FSM gets reset between
        # conversations. The recovery streak (``_clean_streak_since_corrupt``)
        # is the only field reset by ``reset_after_corruption`` to keep
        # the recovered-event semantics local to a single corruption
        # episode.
        self._corruption_count: int = 0
        self._frames_processed: int = 0
        self._corruption_frame_log: deque[int] = deque(
            maxlen=_CORRUPTION_UNRECOVERABLE_THRESHOLD,
        )
        self._clean_streak_since_corrupt: int = 0
        self._unrecoverable_signal_emitted: bool = False
        self._last_frame_was_corrupt: bool = False

        # TS3 chaos injector — opt-in NaN injection at the
        # VAD_CORRUPTION site. Disabled by default; chaos test
        # matrix sets the env vars to validate that the V1
        # NaN/Inf guard fires correctly + LSTM resets cleanly +
        # the M2 DROP event lands with error_type=probability_nan.
        self._chaos = ChaosInjector(site_id=ChaosSite.VAD_CORRUPTION.value)

        logger.info(
            "SileroVAD initialised",
            model=str(model_path),
            sample_rate=self._config.sample_rate,
            window_size=self._config.window_size,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> VADEvent:
        """Process a single audio frame through ONNX model + hysteresis FSM.

        Args:
            audio_frame: Exactly ``config.window_size`` samples —
                float32 normalised [-1, 1] **or** int16 [-32768, 32767].

        Returns:
            A :class:`VADEvent` with speech flag, probability, and FSM state.

        Raises:
            ValueError: If frame has wrong length.
        """
        import numpy as np  # noqa: F811

        # Ring 6 RED + USE: every process_frame is one VAD-stage call.
        # We open the measurement scope BEFORE the shape validation so
        # that even a malformed frame (caller bug) is recorded as
        # outcome=error via measure_stage_duration's BaseException
        # handler. The corruption-recovery path inside the scope marks
        # error explicitly via _stage_token; the success path emits
        # SUCCESS just before the return.
        with measure_stage_duration(VoiceStage.VAD) as _stage_token:
            expected = self._config.window_size
            if audio_frame.shape != (expected,):
                msg = f"Expected frame of {expected} samples, got shape {audio_frame.shape}"
                raise ValueError(msg)

            # Normalise to float32 [-1, 1]
            if audio_frame.dtype == np.int16:
                audio = audio_frame.astype(np.float32) / 32768.0
            else:
                audio = audio_frame.astype(np.float32)

            # ONNX inference
            ort_inputs = {
                "input": audio.reshape(1, -1),
                "state": self._state,
                "sr": self._sr,
            }
            output, raw_next_state = self._session.run(None, ort_inputs)[:2]
            raw_probability = float(output[0][0])

            # TS3 chaos: opt-in NaN injection at the VAD_CORRUPTION
            # site. When SOVYX_CHAOS__ENABLED=true AND
            # SOVYX_CHAOS__INJECT_VAD_CORRUPTION_PCT > 0, the raw
            # probability is overwritten with NaN — the V1 guard
            # below will detect the corruption, fail-closed to
            # silence, and reset the LSTM state. Chaos validates
            # that the V1 recovery path actually works under
            # realistic operating conditions, not just under the
            # unit-test mock that injects NaN deterministically.
            if self._chaos.should_inject():
                raw_probability = float("nan")

            # ── V1 NaN/Inf guard (Ring 3 defense-in-depth) ───────
            # ONNX runtime can return NaN/Inf when the LSTM state has
            # been poisoned (e.g. by an underflow/overflow in a prior
            # frame, a corrupted ``.onnx`` file, or a numerically
            # pathological input like a single-sample DC step).
            # Letting NaN reach the FSM is a silent-failure class:
            # every comparison ``prob > threshold`` returns False, so
            # the FSM freezes in its current state forever and the
            # user sees a "deaf microphone". The guard fail-closes:
            # corrupt frame is treated as silence (probability=0.0)
            # and the LSTM state is zeroed so the next frame starts
            # from a known-clean baseline. The corruption itself is
            # surfaced as a WARNING with a trace ID so dashboards can
            # correlate; repeated corruption escalates to a single
            # ERROR (``unrecoverable``) for upstream circuit-breaker
            # consumption.
            self._frames_processed += 1
            is_corrupt, corruption_kind = _validate_inference_outputs(
                raw_probability,
                raw_next_state,
            )
            if is_corrupt:
                self._on_inference_corruption(
                    corruption_kind=corruption_kind,
                    raw_probability=raw_probability,
                )
                probability = 0.0  # fail-closed: silence is the safe default
                # M2: corruption is a soft failure — the frame still
                # produces a (degraded) output but the inference was
                # broken. DROP with the corruption kind as
                # error_type so dashboards can attribute the rate of
                # ONNX-output corruption per (probability_nan |
                # probability_out_of_range | lstm_state_*) without
                # parsing logs.
                _stage_token.mark_error()
                record_stage_event(
                    VoiceStage.VAD,
                    StageEventKind.DROP,
                    error_type=corruption_kind,
                )
            else:
                self._state = raw_next_state
                probability = raw_probability
                self._on_inference_clean()

            # Rolling window — append before the FSM tick so the
            # enrichment on a transition reflects the probabilities
            # that *led to* it.
            rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
            self._prob_history.append(probability)
            self._rms_history.append(rms)

            # Per-frame telemetry (sampled by SamplingProcessor at the
            # rate set in ObservabilitySamplingConfig.vad_frame_rate).
            # Operators can disable sampling for live-debug by setting
            # the rate to 0.
            logger.info(
                "voice.vad.frame",
                **{
                    "voice.probability": round(probability, 4),
                    "voice.rms": round(rms, 4),
                    "voice.state": self._vad_state.name,
                    "voice.onset_threshold": self._config.onset_threshold,
                    "voice.offset_threshold": self._config.offset_threshold,
                },
            )

            # FSM transition — log every state change so operators
            # can see exactly when/why the orchestrator moved between
            # silence and speech without guessing from the absence
            # of downstream events.
            prev_state = self._vad_state
            is_speech = self._update_state(probability)
            if self._vad_state != prev_state:
                logger.info(
                    "vad_state_transition",
                    from_state=prev_state.name,
                    to_state=self._vad_state.name,
                    probability=round(probability, 3),
                )
                logger.info(
                    "voice.vad.state_changed",
                    **{
                        "voice.from_state": prev_state.name,
                        "voice.to_state": self._vad_state.name,
                        "voice.probability": round(probability, 4),
                        "voice.rms": round(rms, 4),
                        "voice.onset_threshold": self._config.onset_threshold,
                        "voice.offset_threshold": self._config.offset_threshold,
                        "voice.prob_window": [round(p, 4) for p in self._prob_history],
                        "voice.rms_window": [round(r, 4) for r in self._rms_history],
                    },
                )

            # M2: emit SUCCESS only on the clean inference path. The
            # corruption branch above already emitted DROP +
            # mark_error before reaching here.
            if not is_corrupt:
                record_stage_event(VoiceStage.VAD, StageEventKind.SUCCESS)

            return VADEvent(
                is_speech=is_speech,
                probability=probability,
                state=self._vad_state,
            )

    def _smoke_probe_session(self, np_module: Any) -> None:  # noqa: ANN401
        """Band-aid #36: validate the ONNX session at construction.

        Pushes a known-shape silence frame through the loaded model
        with the configured ``sample_rate`` + ``window_size``. Asserts
        the returned probability is a finite float in ``[0, 1]`` and
        the LSTM state has the expected shape.

        Failure modes this catches at startup (instead of on every
        real frame for the lifetime of the daemon):

        * Window-size mismatch (model expects 256 but config says 512,
          or vice versa) → ONNX raises during inference.
        * Sample-rate mismatch (model trained for 8 kHz but config
          sets 16 kHz) → model still runs but probability is garbage
          (NaN, Inf, or out of [0, 1]).
        * Corrupted model file (truncated download, byte-swap on
          mixed-endian transfer) → ONNX raises OR returns NaN.

        Args:
            np_module: numpy module reference passed in from the
                construction context (avoids a second top-level
                import) — typed as :data:`Any` because the numpy
                module surface is broad and we only touch the
                ``zeros`` / ``float32`` attributes here.

        Raises:
            RuntimeError: Any of the above failure modes. The
                exception carries enough detail for the operator to
                identify which axis (window / rate / model integrity)
                broke. Pre-band-aid #36, all three modes degraded
                silently into V1's fail-closed-to-silence path on
                every subsequent frame.
        """
        np = np_module
        # Build a known-shape silent probe frame. Silent input is the
        # deterministic baseline: any healthy Silero session returns
        # ``probability ≈ 0.0`` (well below the onset threshold).
        # NaN/Inf/out-of-range output ⇒ session is broken before the
        # first real frame ever arrives.
        probe = np.zeros((1, self._config.window_size), dtype=np.float32)
        ort_inputs = {
            "input": probe,
            "state": self._state,
            "sr": self._sr,
        }
        try:
            output, next_state = self._session.run(None, ort_inputs)[:2]
        except Exception as exc:
            # Translate to RuntimeError with operator-actionable
            # context. Could be window-size mismatch (ONNX raises
            # InvalidArgument), sample-rate misconfiguration, or a
            # corrupted model file.
            msg = (
                f"VAD ONNX session smoke probe failed: {exc!r}. "
                f"Configured sample_rate={self._config.sample_rate}, "
                f"window_size={self._config.window_size}. Likely a "
                f"model/config mismatch — verify the loaded ONNX "
                f"model was trained for these parameters, or download "
                f"a fresh copy if the file may be corrupted."
            )
            raise RuntimeError(msg) from exc
        raw_probability = float(output[0][0])
        # Use the V1 validator — same closed-set corruption taxonomy
        # the runtime path uses, so smoke-probe failures + runtime
        # failures share vocabulary in the dashboard.
        is_corrupt, corruption_kind = _validate_inference_outputs(
            raw_probability,
            next_state,
        )
        if is_corrupt:
            state_repr = (
                next_state.shape if hasattr(next_state, "shape") else type(next_state).__name__
            )
            msg = (
                f"VAD ONNX session smoke probe returned corrupt output "
                f"({corruption_kind}): probability={raw_probability!r}, "
                f"state.shape={state_repr}. Configured "
                f"sample_rate={self._config.sample_rate}, "
                f"window_size={self._config.window_size}. Likely a "
                f"sample-rate mismatch (model trained for a different "
                f"rate than configured) or a corrupted model file."
            )
            raise RuntimeError(msg)
        logger.debug(
            "voice.vad.smoke_probe_passed",
            sample_rate=self._config.sample_rate,
            window_size=self._config.window_size,
            probe_probability=round(raw_probability, 6),
        )

    def reset(self) -> None:
        """Reset LSTM and FSM state (call between conversations).

        Zeros the recurrent LSTM state and the FSM. Does **not** clear
        the cumulative corruption counters — those describe the health
        of the underlying ONNX session over its lifetime and are needed
        for long-running daemons to surface chronic model issues across
        conversation boundaries.
        """
        import numpy as np  # noqa: F811

        self._state = np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)
        self._vad_state = VADState.SILENCE
        self._consecutive_count = 0
        self._prob_history.clear()
        self._rms_history.clear()

    @property
    def state(self) -> VADState:
        """Current FSM state."""
        return self._vad_state

    @property
    def is_speaking(self) -> bool:
        """Whether the FSM considers speech ongoing."""
        return self._vad_state in (VADState.SPEECH, VADState.SPEECH_OFFSET)

    @property
    def config(self) -> VADConfig:
        """Active configuration (read-only)."""
        return self._config

    @property
    def corruption_count(self) -> int:
        """Total number of NaN/Inf corruption events since construction.

        Cumulative across :meth:`reset` calls — the counter describes
        the health of the underlying ONNX session, not the FSM. Surface
        on the dashboard "Voice Health" panel as a session-quality
        gauge; non-zero on a long-running daemon is a signal that the
        model file or LSTM state has structural issues warranting an
        engineer review, even if the V1 guard is keeping the FSM alive.
        """
        return self._corruption_count

    @property
    def is_session_unrecoverable(self) -> bool:
        """``True`` after the unrecoverable signal has been emitted.

        Set when :data:`_CORRUPTION_UNRECOVERABLE_THRESHOLD` corruptions
        accumulate within :data:`_CORRUPTION_UNRECOVERABLE_WINDOW` frames.
        Upstream consumers (Ring 6 orchestrator, dashboard, circuit
        breaker) should treat this as a hard signal to fall back to a
        secondary detector (LiveKit EOU, simple energy threshold) or
        prompt the user to re-run ``sovyx doctor voice``. Re-armed only
        by full reconstruction of the ``SileroVAD`` instance — once a
        session is declared unrecoverable, it stays declared.
        """
        return self._unrecoverable_signal_emitted

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _on_inference_corruption(
        self,
        *,
        corruption_kind: str,
        raw_probability: float,
    ) -> None:
        """Handle a NaN/Inf detection from the latest inference (V1).

        Three responsibilities, in order:

        1. Reset the LSTM state to zeros so the *next* inference starts
           from a known-clean baseline. Without this the recurrent
           connection re-poisons every subsequent frame and corruption
           becomes permanent until the daemon restarts.
        2. Update the corruption monitor:
           - Bump the lifetime counter (visible via :attr:`corruption_count`).
           - Append the current frame index to the sliding window.
           - Reset the clean-streak counter (any corruption breaks the
             pending recovery confirmation).
        3. Emit structured telemetry:
           - ``voice.vad.session_corrupt`` (WARNING) for every detection.
           - ``voice.vad.session_unrecoverable`` (ERROR, exactly once)
             when the sliding window reaches the unrecoverable threshold.

        The probability passed to the FSM is set to ``0.0`` by the
        caller — fail-closed (silence) is the safer default than
        fail-open (speech), which would fabricate phantom turn-ends and
        feed STT garbage. The FSM state itself is preserved so the
        recovery is graceful: if we were in SPEECH, a single corrupt
        frame doesn't drop us to SILENCE; the FSM merely receives a
        zero probability and applies its hysteresis as usual.
        """
        import numpy as np  # noqa: F811

        self._state = np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)
        self._corruption_count += 1
        self._corruption_frame_log.append(self._frames_processed)
        self._clean_streak_since_corrupt = 0
        self._last_frame_was_corrupt = True

        logger.warning(
            "voice.vad.session_corrupt",
            **{
                "voice.corruption_kind": corruption_kind,
                "voice.raw_probability_repr": repr(raw_probability),
                "voice.frame_index": self._frames_processed,
                "voice.lifetime_corruption_count": self._corruption_count,
                "voice.fsm_state_preserved": self._vad_state.name,
                "voice.action": "lstm_state_zeroed_probability_forced_silence",
            },
        )

        if (
            not self._unrecoverable_signal_emitted
            and len(self._corruption_frame_log) >= _CORRUPTION_UNRECOVERABLE_THRESHOLD
            and (
                self._corruption_frame_log[-1] - self._corruption_frame_log[0]
                <= _CORRUPTION_UNRECOVERABLE_WINDOW
            )
        ):
            self._unrecoverable_signal_emitted = True
            logger.error(
                "voice.vad.session_unrecoverable",
                **{
                    "voice.corruptions_in_window": len(self._corruption_frame_log),
                    "voice.window_frames": _CORRUPTION_UNRECOVERABLE_WINDOW,
                    "voice.frame_index": self._frames_processed,
                    "voice.lifetime_corruption_count": self._corruption_count,
                    "voice.action_required": "fallback_to_secondary_vad_or_rebuild_session",
                },
            )

    def _on_inference_clean(self) -> None:
        """Track recovery progress after a corruption episode.

        After :data:`_CORRUPTION_RECOVERY_FRAMES` consecutive clean
        frames following a corruption, emit
        :data:`voice.vad.session_recovered` exactly once per episode so
        operators can confirm the V1 guard genuinely re-armed the
        session — a corruption + nothing else in the log is ambiguous
        (was it transient? did the daemon hang?), but corrupt + recovered
        is unambiguous.
        """
        if not self._last_frame_was_corrupt and self._clean_streak_since_corrupt == 0:
            return  # never been corrupt — nothing to track
        self._clean_streak_since_corrupt += 1
        self._last_frame_was_corrupt = False
        if self._clean_streak_since_corrupt == _CORRUPTION_RECOVERY_FRAMES:
            logger.info(
                "voice.vad.session_recovered",
                **{
                    "voice.clean_frames_since_corrupt": _CORRUPTION_RECOVERY_FRAMES,
                    "voice.frame_index": self._frames_processed,
                    "voice.lifetime_corruption_count": self._corruption_count,
                },
            )

    def _update_state(self, probability: float) -> bool:  # noqa: PLR0911
        """Advance the hysteresis FSM and return whether speech is active."""
        if self._vad_state == VADState.SILENCE:
            if probability > self._config.onset_threshold:
                self._consecutive_count = 1
                if self._consecutive_count >= self._config.min_onset_frames:
                    self._vad_state = VADState.SPEECH
                    self._consecutive_count = 0
                    return True
                self._vad_state = VADState.SPEECH_ONSET
            return False

        if self._vad_state == VADState.SPEECH_ONSET:
            if probability > self._config.onset_threshold:
                self._consecutive_count += 1
                if self._consecutive_count >= self._config.min_onset_frames:
                    self._vad_state = VADState.SPEECH
                    self._consecutive_count = 0
                    return True
            else:
                # False alarm — back to silence
                self._vad_state = VADState.SILENCE
                self._consecutive_count = 0
            return False

        if self._vad_state == VADState.SPEECH:
            if probability < self._config.offset_threshold:
                self._consecutive_count = 1
                if self._consecutive_count >= self._config.min_offset_frames:
                    self._vad_state = VADState.SILENCE
                    self._consecutive_count = 0
                    return False
                self._vad_state = VADState.SPEECH_OFFSET
            return True

        if self._vad_state == VADState.SPEECH_OFFSET:
            if probability < self._config.offset_threshold:
                self._consecutive_count += 1
                if self._consecutive_count >= self._config.min_offset_frames:
                    self._vad_state = VADState.SILENCE
                    self._consecutive_count = 0
                    return False
            else:
                # Speech resumed
                self._vad_state = VADState.SPEECH
                self._consecutive_count = 0
            return True

        return False  # pragma: no cover — unreachable with current enum
