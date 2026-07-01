"""Heartbeat emission mixin (extracted from ``_orchestrator.py``).

Owns the wall-clock periodic emission of the
``voice_pipeline_heartbeat`` event + the per-frame VAD aggregation
that feeds it. Pre-extraction this surface lived as 3 methods
(:meth:`HeartbeatMixin._track_vad_for_heartbeat`,
:meth:`HeartbeatMixin._emit_heartbeat`,
:meth:`HeartbeatMixin._heartbeat_loop`) on the single 3953-LOC
``VoicePipeline`` class — see CLAUDE.md anti-pattern #16 (god files
> 500 LOC mixed responsibilities) for the carve-out rationale.

This mixin is the FIRST extraction step of Finding 5 of
``docs-internal/missions/voice-zero-defect/PHASE-4-D-AUDIT.md``
(orchestrator god-file split). Same multi-mixin host pattern as
``voice/capture/`` mixins (anti-pattern #16 worked example: capture
2785 → 760 LOC across 12 commits via 5 mixins).

Anti-pattern #32 contract (mixin method-via-MRO stub shadowing): the
heartbeat calls ``self._maybe_trigger_bypass_coordinator()`` which
lives on the HOST class (``VoicePipeline``, AFTER this mixin in MRO).
The cross-mixin reference is declared inside ``if TYPE_CHECKING:``
so the stub body is type-check-only and erased at runtime, letting
MRO fall through to the real method. Putting a real ``def`` stub
here would silently shadow the host method and the heartbeat
deaf-warning path would no-op.

Heartbeat constants moved with the methods: every
``_HEARTBEAT_INTERVAL_S`` / ``_DEAF_*`` / ``_SNR_LOW_ALERT_*`` /
``_NOISE_FLOOR_DRIFT_*`` reference inside the orchestrator was
inside the heartbeat methods themselves, so the constants travel
with their consumers (single-source-of-truth preserved within the
new module). The ``record_snr_low_alert`` /
``record_noise_floor_drift_alert`` imports moved for the same reason.

State the mixin reads/writes (initialized on the HOST in
``VoicePipeline.__init__`` because init order matters for the rest
of the pipeline):

* ``_max_vad_prob_since_heartbeat`` / ``_vad_frames_since_heartbeat``
  — per-window VAD aggregation
* ``_last_heartbeat_monotonic`` — wall-clock idempotence guard
* ``_last_vad_probability_snapshot`` /
  ``_last_vad_probability_snapshot_at`` — freshness fields (CR3)
* ``_snr_low_consecutive_heartbeats`` / ``_snr_low_alert_active`` —
  SNR low-alert latch (Phase 4 / T4.35)
* ``_noise_floor_drift_consecutive_heartbeats`` /
  ``_noise_floor_drift_alert_active`` — noise-floor-drift latch
  (Phase 4 / T4.38)
* ``_deaf_warnings_consecutive`` — deaf-warning counter consumed by
  ``_maybe_trigger_bypass_coordinator`` (host-owned)

Host-owned dependencies the mixin reads (all forward-declared in
the TYPE_CHECKING block):

* ``_config: VoicePipelineConfig`` — ``mind_id`` for log fields +
  per-mind aggregator drain key (Phase 5.A.2 contract)
* ``_state: VoicePipelineState`` — ``state.name`` for log fields
* ``_running: bool`` — loop exit gate
* ``_voice_clarity_active: bool`` — Windows Voice Clarity APO flag
  on the deaf-warning event
* ``_maybe_trigger_bypass_coordinator()`` — host method invoked
  when the deaf-warning fires (anti-pattern #32 contract above)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_noise_floor_drift_alert,
    record_snr_low_alert,
)
from sovyx.voice.pipeline._events import PipelineErrorEvent
from sovyx.voice.pipeline._state import VoicePipelineState

if TYPE_CHECKING:
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._state_machine import PipelineStateMachine

logger = get_logger(__name__)

# ── Heartbeat tuning (moved from _orchestrator.py) ────────────────
_HEARTBEAT_INTERVAL_S = _VoiceTuning().pipeline_heartbeat_interval_seconds
_DWELL_WATCHDOG_S = _VoiceTuning().pipeline_dwell_watchdog_seconds
"""Dwell ceiling for transient states (0 disables). See
``VoiceTuningConfig.pipeline_dwell_watchdog_seconds``."""

_DWELL_WATCHDOG_STATES = frozenset(
    {
        VoicePipelineState.WAKE_DETECTED,
        VoicePipelineState.RECORDING,
        VoicePipelineState.TRANSCRIBING,
        VoicePipelineState.THINKING,
    },
)
"""States that must be transient. SPEAKING is exempt — long TTS
playback is legitimate and its exit is owned by the TTS-out surfaces
(speak/flush_stream, guarded by ``_speech_session_active``); IDLE is
the recovery target."""
_DEAF_MIN_FRAMES = _VoiceTuning().pipeline_deaf_min_frames
_DEAF_VAD_MAX_THRESHOLD = _VoiceTuning().pipeline_deaf_vad_max_threshold

_SNR_LOW_ALERT_ENABLED = _VoiceTuning().voice_snr_low_alert_enabled
_SNR_LOW_ALERT_THRESHOLD_DB = _VoiceTuning().voice_snr_low_alert_threshold_db
_SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS = _VoiceTuning().voice_snr_low_alert_consecutive_heartbeats
"""Phase 4 / T4.35 — see :class:`VoiceTuningConfig` for full
docstrings. Module-level capture mirrors the existing pipeline
tuning pattern so the orchestrator's hot path doesn't re-parse
the config on every heartbeat."""

_NOISE_FLOOR_DRIFT_ALERT_ENABLED = _VoiceTuning().voice_noise_floor_drift_alert_enabled
_NOISE_FLOOR_DRIFT_THRESHOLD_DB = _VoiceTuning().voice_noise_floor_drift_threshold_db
_NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS = (
    _VoiceTuning().voice_noise_floor_drift_consecutive_heartbeats
)
"""Phase 4 / T4.38 — same module-capture rationale as the SNR
trio above."""


def _safe_failover_terminal_interval_s(self_obj: object) -> float:
    """Mission C3 §T2.7 — read the terminal deaf-warn throttle interval.

    Best-effort with a sane default (60.0 s). Resolves via
    ``self._config.failover_terminal_deaf_warn_min_interval_s`` when
    that config field exists; falls back to a fresh
    :class:`VoiceTuningConfig` instance otherwise (matches the SNR-
    constant module-capture pattern above).
    """
    try:
        config = getattr(self_obj, "_config", None)
        explicit = getattr(config, "failover_terminal_deaf_warn_min_interval_s", None)
        if explicit is not None:
            return float(explicit)
    except Exception:  # noqa: BLE001 — observability hygiene only
        pass
    try:
        return float(_VoiceTuning().failover_terminal_deaf_warn_min_interval_s)
    except Exception:  # noqa: BLE001
        return 60.0


def _safe_governor_knob(self_obj: object, name: str, default: float) -> float:
    """Mission C4 §Phase 2 — read a soft-recovery governor knob.

    Mirrors :func:`_safe_failover_terminal_interval_s` for the four
    governor tuning knobs (``supervisor_auto_recovery_n_consecutive_deaf``,
    ``supervisor_auto_recovery_max_retries_per_session``,
    ``supervisor_auto_recovery_cooldown_s``, plus the Phase 3-read
    ``degraded_banner_ack_default_ttl_sec``). Best-effort with a sane
    default — knob unavailability cannot block the heartbeat path.
    """
    try:
        config = getattr(self_obj, "_config", None)
        explicit = getattr(config, name, None)
        if explicit is not None:
            return float(explicit)
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(getattr(_VoiceTuning(), name, default))
    except Exception:  # noqa: BLE001
        return default


class HeartbeatMixin:
    """Periodic ``voice_pipeline_heartbeat`` emission + per-frame VAD aggregation.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance-state
    initialisation in ``__init__`` (init order matters for the rest of
    the pipeline) and this mixin owns the read+write logic on those
    fields.

    See module docstring for the full responsibility carve-out + the
    anti-pattern #32 cross-mixin reference contract.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes. These are
        # initialised on ``VoicePipeline.__init__`` and accessed via
        # ``self.<attr>`` from the methods below. The TYPE_CHECKING
        # block makes mypy strict happy without creating runtime
        # attributes that would interfere with the host's own
        # initialisation.
        _config: VoicePipelineConfig
        _state: VoicePipelineState
        _running: bool
        _voice_clarity_active: bool
        _max_vad_prob_since_heartbeat: float
        _vad_frames_since_heartbeat: int
        _last_heartbeat_monotonic: float
        _last_vad_probability_snapshot: float
        _last_vad_probability_snapshot_at: float
        _snr_low_consecutive_heartbeats: int
        _snr_low_alert_active: bool
        _noise_floor_drift_consecutive_heartbeats: int
        _noise_floor_drift_alert_active: bool
        _deaf_warnings_consecutive: int
        _state_machine: PipelineStateMachine
        _current_utterance_id: str

        # Host-owned method invoked from the mixin (anti-pattern #32
        # contract — TYPE_CHECKING-only stub so MRO falls through to
        # the real implementation on ``VoicePipeline``).
        def _maybe_trigger_bypass_coordinator(self) -> None: ...

        # Cross-mixin host-resident methods (anti-pattern #32 case (b)
        # forward declarations — resolved via MRO at runtime).
        def _cancel_filler(self) -> None: ...
        def _clear_utterance_id(self) -> None: ...
        async def _emit(self, event: object) -> None: ...

    def _track_vad_for_heartbeat(self, probability: float) -> None:
        """Accumulate per-frame VAD stats for the periodic heartbeat.

        v0.31.7 CR3 — pre-CR3 this method ALSO triggered emission
        when the heartbeat interval had elapsed. Emission is now
        SOLELY driven by the wall-clock :meth:`_heartbeat_loop` task
        spawned at :meth:`VoicePipeline.start` (cancelled at
        :meth:`VoicePipeline.stop`). This method retains the per-frame
        call from :meth:`VoicePipeline.feed_frame` so the emission
        body always reads fresh window stats — but it no longer fires
        the emission itself.

        Motivating bug: pre-CR3 ``feed_frame`` was awaited serially
        by ``_consume_loop``; when ``_handle_recording → _end_recording``
        parked on STT (Moonshine ONNX, 200-2000 ms) or
        ``_on_perception → bridge.process`` parked on the LLM
        (1-30 s), no further frames were drained, so the heartbeat
        STOPPED for the whole parking window. Operators saw a
        healthy pipeline as wedged. The timer-driven emission fires
        regardless of consumer-loop progress.

        Per-window stats updated here:

        * ``_max_vad_prob_since_heartbeat`` — highest VAD probability
          observed since the last emission. Read by ``_end_recording``
          for the ``UserStoppedSpeakingFrame.silero_prob_snapshot`` and
          by the deaf-warning detector inside :meth:`_emit_heartbeat`.
        * ``_vad_frames_since_heartbeat`` — number of frames seen in the
          current window; the deaf-warning detector requires
          ``>= _DEAF_MIN_FRAMES`` before considering a window
          "starved-but-not-quiet".
        * ``_last_vad_probability_snapshot`` /
          ``_last_vad_probability_snapshot_at`` — freshest per-frame
          observation. The timer-driven emit reads them so it can
          carry the latest VAD curve point even when the window-max
          is stale (e.g. STT parked for 5 s; the timer fires 2.5x and
          reports the snapshot from the last frame fed before parking).
        """
        if probability > self._max_vad_prob_since_heartbeat:
            self._max_vad_prob_since_heartbeat = probability
        self._vad_frames_since_heartbeat += 1
        # v0.31.7 CR3 — freshness fields. Always overwritten with the
        # latest per-frame observation; the timer-driven emit reads
        # them so it carries the latest VAD curve point even when no
        # frame arrives in the window (STT/LLM parked).
        self._last_vad_probability_snapshot = probability
        self._last_vad_probability_snapshot_at = time.monotonic()

    def _emit_heartbeat(self, now: float) -> None:
        """Emit a single ``voice_pipeline_heartbeat`` (idempotent within a window).

        Called from two sites:

        * :meth:`_heartbeat_loop` — primary contract, wall-clock
          timer regardless of consumer-loop progress (v0.31.7 CR3).
        * :meth:`_track_vad_for_heartbeat` — legacy per-frame fallback
          when the interval has elapsed AND a frame happens to be in
          flight; converges on the same idempotent body.

        Both call sites reset ``_last_heartbeat_monotonic = now`` at
        the bottom so a subsequent caller observes "interval has not
        elapsed" and short-circuits.

        The emission carries the same fields the pre-CR3 inline code
        emitted PLUS the new freshness snapshot fields:

        * ``mind_id``, ``state``, ``max_vad_probability``,
          ``frames_processed`` — pre-CR3 schema (preserved for
          dashboard back-compat).
        * ``last_vad_probability``, ``last_vad_probability_age_s`` —
          new freshness fields (CR3). ``age_s`` is computed against
          ``now``; a value > ``_HEARTBEAT_INTERVAL_S`` means the
          consumer loop hasn't fed a frame in the current window.
        * ``snr_p50_db`` / ``snr_p95_db`` / ``snr_sample_count`` —
          conditionally included when the SNR aggregator drained
          non-empty (Phase 4 T4.34 contract).
        """
        # Phase 4 / T4.34 — drain the per-window SNR buffer for
        # the heartbeat. The fields are conditionally added to the
        # log: when count == 0 (sustained silence or pre-first-
        # speech) we OMIT them rather than emit synthetic zeros so
        # dashboards don't graph misleading floor values.
        # v0.32.6 Phase 5.A — per-mind keying FOUNDATION; v0.32.16 Phase
        # 5.A.2 — PRODUCER thread-through closure. ``FrameNormalizer``
        # now records under the configured-at-startup ``_config.mind_id``
        # (see ``_capture_task.py`` + ``_restart_mixin.py`` construction
        # sites). The drain MUST match the producer key, so we read
        # under the same configured mind_id (heartbeat + producer share
        # the pipeline's lifetime). Per-turn ``_current_mind_id`` is
        # NOT used here because audio-quality samples are hardware-level
        # (the FrameNormalizer's lifetime is the audio session, not the
        # turn). Closes PHASE-4-D-AUDIT.md Finding 6.
        from sovyx.voice.health._snr_heartbeat import drain_window_stats

        snr_window = drain_window_stats(mind_id=self._config.mind_id)
        heartbeat_extra: dict[str, object] = {}
        if snr_window.count > 0:
            heartbeat_extra["snr_p50_db"] = round(snr_window.p50_db, 2)
            heartbeat_extra["snr_p95_db"] = round(snr_window.p95_db, 2)
            heartbeat_extra["snr_sample_count"] = snr_window.count
        # v0.31.7 CR3 — freshness fields. Age is computed against
        # ``now`` (the heartbeat tick time) rather than a fresh
        # ``time.monotonic()`` so timer-driven emission with mocked
        # clocks reports a stable, test-friendly age.
        if self._last_vad_probability_snapshot_at > 0.0:
            heartbeat_extra["last_vad_probability"] = round(self._last_vad_probability_snapshot, 3)
            heartbeat_extra["last_vad_probability_age_s"] = round(
                max(0.0, now - self._last_vad_probability_snapshot_at), 3
            )
        logger.info(
            "voice_pipeline_heartbeat",
            mind_id=self._config.mind_id,
            state=self._state.name,
            max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
            frames_processed=self._vad_frames_since_heartbeat,
            **heartbeat_extra,
        )
        # Phase 4 / T4.35 — SNR low-alert. Only consider windows
        # with real SNR samples (count > 0); a count==0 window
        # means sustained silence, where SNR is undefined and a
        # "low" verdict would be a false alarm.
        if _SNR_LOW_ALERT_ENABLED and snr_window.count > 0:
            if snr_window.p50_db < _SNR_LOW_ALERT_THRESHOLD_DB:
                self._snr_low_consecutive_heartbeats += 1
                if (
                    not self._snr_low_alert_active
                    and self._snr_low_consecutive_heartbeats
                    >= _SNR_LOW_ALERT_CONSECUTIVE_HEARTBEATS
                ):
                    self._snr_low_alert_active = True
                    record_snr_low_alert(state="warned")
                    logger.warning(
                        "voice_pipeline_snr_low_alert",
                        mind_id=self._config.mind_id,
                        state=self._state.name,
                        snr_p50_db=round(snr_window.p50_db, 2),
                        snr_p95_db=round(snr_window.p95_db, 2),
                        snr_sample_count=snr_window.count,
                        threshold_db=_SNR_LOW_ALERT_THRESHOLD_DB,
                        consecutive_heartbeats=(self._snr_low_consecutive_heartbeats),
                        hint=(
                            "Sustained low SNR p50 below the configured "
                            "floor (Moonshine STT degrades sharply <9 dB). "
                            "Likely causes: ambient room noise raised, "
                            "speaker moved further from mic, fan / HVAC "
                            "started, or in-process NS disabled while "
                            "OS NS bypassed. Check `voice.audio.snr_db` "
                            "histogram + `voice.ns.suppression_db` for "
                            "the underlying signal."
                        ),
                    )
            else:
                # Clean heartbeat — reset the de-flap counter and,
                # if the alert was active, emit a single CLEARED
                # event so dashboards close the incident.
                if self._snr_low_alert_active:
                    self._snr_low_alert_active = False
                    record_snr_low_alert(state="cleared")
                    logger.info(
                        "voice_pipeline_snr_low_alert_cleared",
                        mind_id=self._config.mind_id,
                        state=self._state.name,
                        snr_p50_db=round(snr_window.p50_db, 2),
                        snr_sample_count=snr_window.count,
                        threshold_db=_SNR_LOW_ALERT_THRESHOLD_DB,
                    )
                self._snr_low_consecutive_heartbeats = 0

        # Phase 4 / T4.38 — noise-floor drift alert. Reads the
        # rolling buffer without clearing (drain pattern is for
        # SNR; the noise-floor trend wants a stable horizon).
        if _NOISE_FLOOR_DRIFT_ALERT_ENABLED:
            # Phase 5.A.2 — same configured-at-startup ``_config.mind_id``
            # the producer writes under. See SNR drain above for the
            # rationale.
            from sovyx.voice.health._noise_floor_trending import compute_drift

            drift = compute_drift(mind_id=self._config.mind_id)
            if drift.ready:
                if drift.drift_db > _NOISE_FLOOR_DRIFT_THRESHOLD_DB:
                    self._noise_floor_drift_consecutive_heartbeats += 1
                    if (
                        not self._noise_floor_drift_alert_active
                        and self._noise_floor_drift_consecutive_heartbeats
                        >= _NOISE_FLOOR_DRIFT_CONSECUTIVE_HEARTBEATS
                    ):
                        self._noise_floor_drift_alert_active = True
                        record_noise_floor_drift_alert(state="warned")
                        logger.warning(
                            "voice_pipeline_noise_floor_drift_warning",
                            mind_id=self._config.mind_id,
                            state=self._state.name,
                            short_avg_db=round(drift.short_avg_db, 2),
                            long_avg_db=round(drift.long_avg_db, 2),
                            drift_db=round(drift.drift_db, 2),
                            short_sample_count=drift.short_count,
                            long_sample_count=drift.long_count,
                            threshold_db=_NOISE_FLOOR_DRIFT_THRESHOLD_DB,
                            consecutive_heartbeats=(
                                self._noise_floor_drift_consecutive_heartbeats
                            ),
                            hint=(
                                "Sustained noise-floor rise vs the rolling "
                                "5-min baseline. Likely causes: HVAC / fan "
                                "started, occupancy increased, mic moved "
                                "closer to a noise source, or capture "
                                "device gain auto-raised. Check "
                                "voice.audio.snr_db to see if speech-band "
                                "SNR also dropped (room actually got noisier "
                                "for speech) vs stayed flat (only the "
                                "non-speech floor changed)."
                            ),
                        )
                else:
                    if self._noise_floor_drift_alert_active:
                        self._noise_floor_drift_alert_active = False
                        record_noise_floor_drift_alert(state="cleared")
                        logger.info(
                            "voice_pipeline_noise_floor_drift_cleared",
                            mind_id=self._config.mind_id,
                            state=self._state.name,
                            short_avg_db=round(drift.short_avg_db, 2),
                            long_avg_db=round(drift.long_avg_db, 2),
                            drift_db=round(drift.drift_db, 2),
                            threshold_db=_NOISE_FLOOR_DRIFT_THRESHOLD_DB,
                        )
                    self._noise_floor_drift_consecutive_heartbeats = 0

        is_deaf = (
            self._vad_frames_since_heartbeat >= _DEAF_MIN_FRAMES
            and self._max_vad_prob_since_heartbeat < _DEAF_VAD_MAX_THRESHOLD
        )
        if is_deaf:
            # Audio is arriving but VAD is stuck near zero — this is the
            # canonical fingerprint of "frames are not 16 kHz mono" *or*
            # a capture APO (e.g. Windows Voice Clarity / VocaEffectPack)
            # zeroing out the signal before it reaches the engine.
            self._deaf_warnings_consecutive += 1

            # Mission C3 §T2.7 — post-ladder-exhaustion throttle. After
            # the runtime-failover ladder reports
            # ``state.ladder_exhausted=True`` AND the coordinator has
            # latched terminal (``_coordinator_terminated=True``), the
            # deaf-warning signal is operator-actionable ONCE per ladder
            # cycle, not every 5 s. Without throttling the operator's
            # v0.43.1 session emitted 29 redundant warnings over 12 min
            # (H7 amplification). Throttle to 1 emission per
            # ``failover_terminal_deaf_warn_min_interval_s`` (default
            # 60 s) and tag every emission with
            # ``coordinator_terminal`` (True/False) so dashboards split.
            now_terminal = time.monotonic()
            coordinator_terminal = bool(
                getattr(self, "_coordinator_terminated", False)
                and getattr(self, "_failover_ladder_exhausted", False),
            )
            if coordinator_terminal:
                tuning_terminal_interval = _safe_failover_terminal_interval_s(self)
                last_terminal_emit = getattr(
                    self,
                    "_last_terminal_deaf_warn_monotonic",
                    0.0,
                )
                if (
                    last_terminal_emit > 0.0
                    and (now_terminal - last_terminal_emit) < tuning_terminal_interval
                ):
                    # Throttled — increment counter silently + maybe
                    # trigger coordinator (heartbeat path stays alive,
                    # log noise suppressed).
                    self._maybe_trigger_bypass_coordinator()
                    self._last_heartbeat_monotonic = now
                    self._max_vad_prob_since_heartbeat = 0.0
                    self._vad_frames_since_heartbeat = 0
                    return
                self._last_terminal_deaf_warn_monotonic = now_terminal

            logger.warning(
                "voice_pipeline_deaf_warning",
                mind_id=self._config.mind_id,
                state=self._state.name,
                max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
                frames_processed=self._vad_frames_since_heartbeat,
                vad_max_threshold=_DEAF_VAD_MAX_THRESHOLD,
                consecutive_deaf_warnings=self._deaf_warnings_consecutive,
                voice_clarity_active=self._voice_clarity_active,
                coordinator_terminal=coordinator_terminal,
                hint=(
                    "Orchestrator received frames but VAD probability stayed "
                    "below threshold — check FrameNormalizer source_rate/channels "
                    "and audio_capture_resample_active logs."
                ),
            )
            self._maybe_trigger_bypass_coordinator()

            # Mission H4 §8 T4.6 — Phase 1.D heap-snapshot trigger.
            # When the deaf-cluster signature matches v0.43.1 forensic
            # anchor (consecutive deaf-warnings ≥ 5 + coordinator
            # terminal + ladder exhausted), emit a structured event
            # that the ResourceCohortGovernor's RSS_GROWTH path will
            # honour via tracemalloc.take_snapshot persistence (when
            # the feature flag is on). Throttle is provided by C3's
            # post-ladder-exhaustion 60 s window above — this trigger
            # fires at most once per ladder cycle.
            failover_ladder_in_progress = bool(
                getattr(self, "_failover_ladder_in_progress", False)
            )
            if (
                coordinator_terminal
                and failover_ladder_in_progress
                and self._deaf_warnings_consecutive >= 5
            ):
                logger.warning(
                    "voice.deaf_cluster.heap_snapshot_requested",
                    mind_id=self._config.mind_id,
                    cohort="voice_failover",
                    consecutive_deaf_warnings=self._deaf_warnings_consecutive,
                    hint=(
                        "Forensic-anchor signature matched (Mission H4 §H4). "
                        "Set SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC=true "
                        "+ restart daemon to capture allocator-level "
                        "forensics on the next cluster."
                    ),
                )
                # Mission H4 §8 T4.6 — direct wire into ResourceCohortGovernor's
                # snapshot persistence path. When tracemalloc is enabled the
                # governor writes ~/.sovyx/diagnostics/heap-snapshot-<ts>.json
                # immediately; when disabled it emits
                # engine.resources.heap_snapshot_skipped (single line, hint
                # included) instead. Best-effort: governor unavailability
                # cannot block the heartbeat path.
                try:
                    from sovyx.observability._resource_cohort_governor import (  # noqa: PLC0415 — lazy
                        get_default_resource_cohort_governor,
                    )

                    get_default_resource_cohort_governor().request_heap_snapshot(
                        cohort="voice_failover_deaf_cluster",
                        cohort_observed=int(self._deaf_warnings_consecutive),
                        cohort_budget=5,
                        extra_metadata={
                            "mind_id": self._config.mind_id,
                            "state": self._state.name,
                            "trigger": "heartbeat_deaf_cluster_n5",
                        },
                    )
                except Exception:  # noqa: BLE001 — observability isolation
                    logger.debug(
                        "voice.deaf_cluster.heap_snapshot_dispatch_failed",
                        mind_id=self._config.mind_id,
                        exc_info=True,
                    )

            # Mission C4 §Phase 2 — Soft Recovery Governor.
            # After N consecutive deaf-warnings while BOTH
            # ``_coordinator_terminated`` AND ``_failover_ladder_exhausted``
            # are True, request a soft recovery (state reset, not full
            # restart). Bounded retry budget + cooldown prevent thrash.
            # On budget exhaustion, escalate the voice axis severity to
            # ``critical`` so the dashboard banner pulses.
            if coordinator_terminal:
                self._maybe_run_soft_recovery_governor(now=now_terminal)
        else:
            # Reset the consecutive counter so a single healthy heartbeat
            # between two deaf ones does not trigger the auto-bypass.
            self._deaf_warnings_consecutive = 0
        self._last_heartbeat_monotonic = now
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0

    def _maybe_run_soft_recovery_governor(self, *, now: float) -> None:
        """Mission C4 §Phase 2 — soft-recovery governor.

        Pre-conditions (all must hold):

        1. ``_deaf_warnings_consecutive >= supervisor_auto_recovery_n_consecutive_deaf``
           — the operator-observable window has elapsed (default N=3).
        2. ``_supervisor_recovery_attempts < supervisor_auto_recovery_max_retries_per_session``
           — retry budget not exhausted (default 3 attempts).
        3. ``now - _supervisor_recovery_last_attempt_monotonic >=
           supervisor_auto_recovery_cooldown_s`` — cooldown elapsed
           (default 300 s).

        Caller (``_emit_heartbeat``) has already verified
        ``coordinator_terminal=True`` (both ``_coordinator_terminated``
        AND ``_failover_ladder_exhausted`` are True).

        On pre-condition satisfaction: spawn a background task that
        calls :meth:`SupervisorMixin.request_soft_recovery`. The
        background-task pattern prevents the heartbeat loop from
        blocking on the async recovery sequence. Bump retry counter
        + last-attempt timestamp synchronously so subsequent heartbeat
        ticks see the in-flight recovery as "attempted".

        On budget exhaustion: emit ``voice.supervisor.escalation_required``
        ONCE (idempotent via the ``_supervisor_escalation_logged`` flag)
        and upgrade the voice axis in :class:`EngineDegradedStore` to
        ``severity=critical``.

        Anti-pattern compliance:
        * #15 — counter state is bounded by max_retries (≤ 10 per
          knob's documented upper bound).
        * #24 — every monotonic-deadline comparison uses ``>=`` for
          Windows coarse-clock safety.
        * #34 — governor is default-ON (knob ``max_retries=3 > 0``)
          per ADR-D3; setting max_retries=0 disables completely.
        * #35 — all governor-state attribute reads via
          ``getattr(..., default)`` so a pre-Phase-2 host (during
          rollback) sees default-zero values.
        """
        from sovyx.observability.tasks import spawn

        n_threshold = int(
            _safe_governor_knob(
                self,
                "supervisor_auto_recovery_n_consecutive_deaf",
                3.0,
            ),
        )
        if self._deaf_warnings_consecutive < n_threshold:
            return

        max_retries = int(
            _safe_governor_knob(
                self,
                "supervisor_auto_recovery_max_retries_per_session",
                3.0,
            ),
        )
        # max_retries == 0 disables the governor entirely (escape hatch).
        if max_retries <= 0:
            return

        attempts = int(getattr(self, "_supervisor_recovery_attempts", 0))
        last_attempt = float(
            getattr(self, "_supervisor_recovery_last_attempt_monotonic", 0.0),
        )
        cooldown_s = _safe_governor_knob(
            self,
            "supervisor_auto_recovery_cooldown_s",
            300.0,
        )

        # Cooldown gate: skip if a prior attempt is too recent.
        #
        # Anti-pattern #24 — use ``>=`` semantics on monotonic-deadline
        # comparisons (inclusive + coarse-clock safe). We use the
        # ``now < last_attempt + cooldown_s`` formulation rather than
        # ``(now - last_attempt) < cooldown_s`` to avoid float-precision
        # loss in the subtraction: on some hosts (macOS-arm64, some
        # Linux Python 3.12 builds) ``time.monotonic()`` returns a
        # float whose representation precision is ~1e-7 s. Subtracting
        # two close values amplifies the error so ``(T + 300.0) - T``
        # may yield ``299.99999...``, triggering a spurious early-
        # return when ``elapsed == cooldown_s`` was expected. The
        # rewritten form keeps both operands as wall-clock-magnitude
        # numbers so the comparison is exact for the test's intent.
        if last_attempt > 0.0 and now < last_attempt + cooldown_s:
            return

        if attempts >= max_retries:
            # Budget exhausted — emit escalation ONCE per session +
            # upgrade voice-axis severity to critical so the banner
            # pulses. Idempotent via the _supervisor_escalation_logged
            # flag (defaults False per anti-pattern #35).
            if not getattr(self, "_supervisor_escalation_logged", False):
                self._supervisor_escalation_logged = True
                logger.error(
                    "voice.supervisor.escalation_required",
                    **{
                        "voice.mind_id": self._config.mind_id,
                        "voice.attempts_so_far": attempts,
                        "voice.max_retries": max_retries,
                        "voice.action_required": (
                            "Auto-recovery governor exhausted its retry "
                            "budget. Manual operator intervention required: "
                            "check the dashboard banner for actionable "
                            "chips, or run `sovyx restart`."
                        ),
                    },
                )
                self._escalate_voice_axis_to_critical()
            return

        # All gates passed — bump counter + spawn recovery.
        self._supervisor_recovery_attempts = attempts + 1
        self._supervisor_recovery_last_attempt_monotonic = now
        logger.warning(
            "voice.supervisor.soft_recovery_triggered",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.deaf_warnings_consecutive": self._deaf_warnings_consecutive,
                "voice.attempt_index": attempts + 1,
                "voice.max_retries": max_retries,
                "voice.n_threshold": n_threshold,
                "voice.cooldown_s": cooldown_s,
            },
        )

        # ``request_soft_recovery`` is mounted via SupervisorMixin on
        # VoicePipeline. Spawn so the heartbeat loop is not blocked on
        # the async recovery sequence. Best-effort: a missing
        # request_soft_recovery (pre-Phase-2 host during rollback) is
        # caught + logged DEBUG.
        request_fn = getattr(self, "request_soft_recovery", None)
        if request_fn is None:
            logger.debug(
                "voice.supervisor.soft_recovery_unavailable",
                **{"voice.mind_id": self._config.mind_id},
            )
            return
        spawn(
            request_fn(reason="auto_recovery_governor"),
            name="voice-supervisor-soft-recovery",
        )

    def _escalate_voice_axis_to_critical(self) -> None:
        """Mission C4 §Phase 2 — upgrade voice axis severity to critical
        in the cross-axis :class:`EngineDegradedStore`.

        Called when the soft-recovery governor exhausts its retry
        budget. The dashboard banner reads ``composite_severity`` from
        the cross-axis store; severity escalation per ADR-D6 yields
        ``critical`` when ≥ 3 axes are degraded OR when an explicit
        critical severity is recorded on any axis.

        Best-effort: store unavailability cannot block the heartbeat.
        """
        try:
            from sovyx.engine._degraded_store import (
                DegradedEntry,
                get_default_degraded_store,
                make_action_chip,
                now_monotonic,
            )

            _now = now_monotonic()
            get_default_degraded_store().record(
                DegradedEntry(
                    axis="voice",
                    reason="failover_ladder_exhausted",
                    severity="critical",
                    title_token="degraded.voice.ladderExhausted.title",
                    body_token="degraded.voice.ladderExhausted.body",
                    action_chips=(
                        make_action_chip(
                            "degraded.voice.ladderExhausted.viewHistory",
                            "navigate",
                            "/voice/health",
                            style="primary",
                        ),
                        make_action_chip(
                            "degraded.voice.ladderExhausted.reconnectUsb",
                            "external_link",
                            "https://sovyx.dev/docs/voice/troubleshooting",
                        ),
                    ),
                    metadata={
                        "auto_recovery_exhausted": True,
                        "retries_attempted": int(
                            getattr(self, "_supervisor_recovery_attempts", 0),
                        ),
                    },
                    first_observed_monotonic=_now,
                    last_observed_monotonic=_now,
                    occurrence_count=1,
                ),
            )
        except Exception:  # noqa: BLE001 — observability only
            logger.debug(
                "voice.supervisor.degraded_store_escalation_failed",
                **{"voice.mind_id": self._config.mind_id},
            )

    async def _check_dwell_watchdog(self) -> None:
        """Force-recover a transient state that outlived its dwell ceiling.

        Wires the previously-unconnected dwell machinery on
        :class:`PipelineStateMachine` (``is_watchdog_expired`` /
        ``fire_watchdog`` were built "Phase 1: observe" and never
        called) into the wall-clock heartbeat. Concrete zombies this
        closes:

        * **THINKING latch** — the cogloop task died (batch-path LLM
          exception, guardrail-filtered empty response, cancellation
          before any TTS-out call) and nothing ever wrote the state
          back; the pipeline passed frames through THINKING forever —
          permanently deaf until process restart.
        * **RECORDING/WAKE_DETECTED stall** — capture stopped
          delivering frames mid-utterance (device unplug, consumer
          wedge); the frame-count exits in ``_handle_recording`` are
          frame-driven and never fire without frames.
        * **TRANSCRIBING park** — STT wedged past its own timeout.

        Recovery is performed on the AUTHORITATIVE state (the host
        ``_state`` property, which mirrors into the observe-only
        machine and closes the voice-turn saga) rather than via
        :meth:`PipelineStateMachine.fire_watchdog` — calling both
        would double-record the transition. The machine's dwell clock
        is authoritative because every ``_state`` write mirrors into
        ``record_transition`` (which resets ``entered_monotonic``).

        Disabled when ``pipeline_dwell_watchdog_seconds == 0``
        (kill-switch; recovery defaults ON — inverse anti-pattern #34).
        """
        if _DWELL_WATCHDOG_S <= 0:
            return
        state = self._state
        if state not in _DWELL_WATCHDOG_STATES:
            return
        dwell_s = self._state_machine.time_in_current_state_s()
        if dwell_s < _DWELL_WATCHDOG_S:
            return
        stuck_utterance_id = self._current_utterance_id
        logger.warning(
            "pipeline.state.watchdog_fired",
            **{
                "voice.mind_id": self._config.mind_id,
                "voice.from_state": state.name,
                "voice.dwell_s": round(dwell_s, 1),
                "voice.threshold_s": _DWELL_WATCHDOG_S,
                "voice.utterance_id": stuck_utterance_id,
                "voice.action_required": (
                    "A transient pipeline state exceeded its dwell "
                    "ceiling and was force-recovered to IDLE. One "
                    "occurrence usually means a turn died mid-flight "
                    "(LLM/STT error or cancellation); recurring "
                    "occurrences indicate a wedged engine — check "
                    "`sovyx doctor voice` and the LLM provider health."
                ),
            },
        )
        self._cancel_filler()
        self._state = VoicePipelineState.IDLE
        self._clear_utterance_id()
        await self._emit(
            PipelineErrorEvent(
                mind_id=self._config.mind_id,
                error=(f"dwell_watchdog_fired (state={state.name}, dwell_s={dwell_s:.1f})"),
                utterance_id=stuck_utterance_id,
            )
        )

    async def _heartbeat_loop(self) -> None:
        """Wall-clock heartbeat task — fires regardless of consumer-loop progress.

        v0.31.7 CR3 — pre-CR3 the heartbeat was emitted ONLY from
        :meth:`_track_vad_for_heartbeat` (called from
        :meth:`VoicePipeline.feed_frame`). ``feed_frame`` is awaited
        serially by the capture-side ``_consume_loop`` — one frame at
        a time. When ``_handle_recording → _end_recording → await
        stt.transcribe`` parked on STT (Moonshine ONNX, 200-2000 ms)
        or ``_on_perception → bridge.process`` parked on the LLM
        (1-30 s), no further frames were drained, so the heartbeat
        STOPPED for the whole parking window. Operators interpreted a
        healthy pipeline as wedged.

        This loop fixes the contract: heartbeat fires every
        ``_HEARTBEAT_INTERVAL_S`` regardless of consumer-loop progress.
        Per-frame ``_track_vad_for_heartbeat`` continues to update
        the window stats (and is a SECONDARY trigger — see that
        method's docstring) so dashboards see fresh VAD probability
        in every emit even during parking.

        Cancellation contract:

        * The loop exits cleanly on :exc:`asyncio.CancelledError`
          (raised when :meth:`VoicePipeline.stop` cancels the task).
        * The loop also self-exits when ``_running`` flips False at
          the next sleep boundary (defensive — :meth:`stop` always
          cancels, but a future refactor that flips ``_running`` from
          a different code path would still terminate the loop).

        Per CLAUDE.md anti-pattern #15 + the spec's lifecycle rules,
        the task is stored as ``_heartbeat_task`` (single field, not a
        dict) and :meth:`VoicePipeline.stop` cancels + drains it with
        the same bounded ``_CANCELLATION_TASK_TIMEOUT_S`` budget the
        filler and TTS drains use.
        """
        try:
            while self._running:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                if not self._running:
                    return
                # Best-effort emit — any exception raised inside
                # _emit_heartbeat would otherwise terminate the loop
                # and silence heartbeats forever for this pipeline
                # lifetime. Logged at WARN so the regression is
                # visible without crashing the daemon.
                try:
                    self._emit_heartbeat(time.monotonic())
                except Exception as exc:  # noqa: BLE001 — observability isolation
                    logger.warning(
                        "voice.pipeline.heartbeat_emit_failed",
                        mind_id=self._config.mind_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                # Dwell watchdog — same isolation contract as the emit
                # above: a recovery failure must not kill the loop.
                try:
                    await self._check_dwell_watchdog()
                except Exception as exc:  # noqa: BLE001 — recovery isolation
                    logger.warning(
                        "voice.pipeline.dwell_watchdog_failed",
                        mind_id=self._config.mind_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            # Cleanly cancellable by stop() — re-raise so the spawn
            # telemetry records "cancelled" rather than "exited
            # normally" (which would be misleading for forensic
            # timeline reconstruction).
            raise
