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

if TYPE_CHECKING:
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._state import VoicePipelineState

logger = get_logger(__name__)

# ── Heartbeat tuning (moved from _orchestrator.py) ────────────────
_HEARTBEAT_INTERVAL_S = _VoiceTuning().pipeline_heartbeat_interval_seconds
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

        # Host-owned method invoked from the mixin (anti-pattern #32
        # contract — TYPE_CHECKING-only stub so MRO falls through to
        # the real implementation on ``VoicePipeline``).
        def _maybe_trigger_bypass_coordinator(self) -> None: ...

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
            logger.warning(
                "voice_pipeline_deaf_warning",
                mind_id=self._config.mind_id,
                state=self._state.name,
                max_vad_probability=round(self._max_vad_prob_since_heartbeat, 3),
                frames_processed=self._vad_frames_since_heartbeat,
                vad_max_threshold=_DEAF_VAD_MAX_THRESHOLD,
                consecutive_deaf_warnings=self._deaf_warnings_consecutive,
                voice_clarity_active=self._voice_clarity_active,
                hint=(
                    "Orchestrator received frames but VAD probability stayed "
                    "below threshold — check FrameNormalizer source_rate/channels "
                    "and audio_capture_resample_active logs."
                ),
            )
            self._maybe_trigger_bypass_coordinator()
        else:
            # Reset the consecutive counter so a single healthy heartbeat
            # between two deaf ones does not trigger the auto-bypass.
            self._deaf_warnings_consecutive = 0
        self._last_heartbeat_monotonic = now
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0

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
        except asyncio.CancelledError:
            # Cleanly cancellable by stop() — re-raise so the spawn
            # telemetry records "cancelled" rather than "exited
            # normally" (which would be misleading for forensic
            # timeline reconstruction).
            raise
