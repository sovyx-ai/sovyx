"""Pipeline lifecycle mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's :meth:`VoicePipeline.start` /
:meth:`VoicePipeline.stop` lifecycle methods — the entry/exit points
the factory + dashboard call to bring the pipeline up + down cleanly.

Pre-extraction this surface lived as 2 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — tenth strike of the Phase 5.F.19+ orchestrator
split.

Lifecycle contract:

* ``start`` — Mission Phase 1 / T1.11 idempotent. Pre-caches Jarvis
  fillers, flips ``_running=True``, transitions to IDLE, captures the
  heartbeat anchor, spawns the wall-clock heartbeat loop (CR3 timer
  decoupled from the per-frame trigger), and registers runtime
  listeners. Double-start logs a structured no-op rather than orphaning
  the prior session's tasks.
* ``stop`` — Mission Phase 1 / T1.10 drain-before-return. Sequence:
  emit stop_begin → flip ``_running=False`` → cancel + drain cogloop
  bridge tasks (PIPELINE-5 — the upstream producers, quiesced FIRST
  so nothing streams into the surfaces being torn down) → cancel +
  drain filler → cancel + drain heartbeat → interrupt output →
  cancel + drain the streaming background drainer + clear
  ``_speech_session_active`` → cancel + drain TTS tasks (snapshot
  under ``_task_tracking_lock``) → reset state → release
  self-feedback gate → unregister listeners → emit stop_complete
  with drain counters.

Anti-pattern #32 contract: the mixin makes 4 cross-mixin method
calls — all forward-declared in the TYPE_CHECKING block so MRO
resolves the real implementations on sibling mixins at runtime:

* ``self._heartbeat_loop()`` — HeartbeatMixin.
* ``self._register_listeners()`` / ``self._unregister_listeners()``
  — ListenerWireupMixin.
* ``self._cancel_filler()`` — TtsCancelChainMixin.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_running: bool`` — pipeline-active gate.
* ``_state`` — VoicePipelineState property+setter on host.
* ``_jarvis: JarvisIllusion`` — pre_cache() target.
* ``_last_heartbeat_monotonic: float`` — heartbeat anchor.
* ``_heartbeat_task: asyncio.Task[None] | None`` — wall-clock
  heartbeat task handle (CR3).
* ``_filler_task: asyncio.Task[bool] | None`` — pending filler task.
* ``_output: AudioOutputQueue`` — interrupt() target on stop.
* ``_utterance_frames`` — accumulator cleared on stop.
* ``_self_feedback_gate: SelfFeedbackGate | None`` — duck release
  on stop.
* ``_task_tracking_lock: asyncio.Lock`` — guards TTS-set snapshot.
* ``_in_flight_tts_tasks: set[asyncio.Task[Any]]`` — drain target.
* ``_in_flight_cogloop_tasks: set[asyncio.Task[Any]]`` — cogloop
  bridge tasks cancelled + drained on stop (PIPELINE-5).
* ``_config: VoicePipelineConfig`` — read for mind_id +
  wake_word_enabled log attribution.
* ``_speech_session_active: bool`` — turn-ownership flag cleared on
  stop so the next session starts without a stale open turn.
* ``_stream_drain_task: asyncio.Task[None] | None`` — streaming
  background playback drainer; cancelled + drained on stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._tts_cancel_chain_mixin import _CANCELLATION_TASK_TIMEOUT_S

if TYPE_CHECKING:
    from sovyx.voice.health._self_feedback import SelfFeedbackGate
    from sovyx.voice.jarvis import JarvisIllusion
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._output_queue import AudioOutputQueue

logger = get_logger(__name__)


class LifecycleMixin:
    """Pipeline start + stop lifecycle.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the start/stop entry/exit points.

    See module docstring for the full responsibility carve-out + the
    anti-pattern #32 cross-mixin reference contract.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes.
        _running: bool
        _state: VoicePipelineState
        _jarvis: JarvisIllusion
        _last_heartbeat_monotonic: float
        _heartbeat_task: asyncio.Task[None] | None
        _filler_task: asyncio.Task[bool] | None
        _output: AudioOutputQueue
        _utterance_frames: list[Any]
        _self_feedback_gate: SelfFeedbackGate | None
        _task_tracking_lock: asyncio.Lock
        _in_flight_tts_tasks: set[asyncio.Task[Any]]
        _in_flight_cogloop_tasks: set[asyncio.Task[Any]]
        _config: VoicePipelineConfig
        _speech_session_active: bool
        _stream_drain_task: asyncio.Task[None] | None

        # Cross-mixin host-resident methods (resolved via MRO at
        # runtime). Anti-pattern #32 case (b) forward declarations —
        # TYPE_CHECKING-only so MRO falls through to the real
        # implementations on sibling mixins.
        async def _heartbeat_loop(self) -> None: ...
        def _register_listeners(self) -> None: ...
        def _unregister_listeners(self) -> None: ...
        def _cancel_filler(self) -> None: ...

    async def start(self) -> None:
        """Initialize the pipeline and pre-cache fillers.

        Call this before feeding frames. Double-start is a no-op
        — every existing in-flight task / pre-cached filler / state
        from the prior :meth:`start` is preserved and the second
        invocation logs ``voice.pipeline.start_already_running_ignored``
        so dashboards see the misuse without a crash. Mission Phase 1
        T1.11 — guards against orphaned filler tasks + duplicated
        pre-cache work that the spec's "start() called twice orphans
        first saga + tasks" finding documented.
        """
        if self._running:
            logger.info(
                "voice.pipeline.start_already_running_ignored",
                mind_id=self._config.mind_id,
                state=self._state.name,
            )
            return
        await self._jarvis.pre_cache()
        self._running = True
        self._state = VoicePipelineState.IDLE
        self._last_heartbeat_monotonic = time.monotonic()

        # v0.31.7 CR3 — spawn the wall-clock heartbeat loop. Decouples
        # ``voice_pipeline_heartbeat`` emission from ``feed_frame`` so
        # dashboards keep seeing liveness signals during STT / LLM /
        # TTS parking (the consumer loop blocks on those awaits and
        # would otherwise stop calling per-frame heartbeat trigger).
        # See :meth:`_heartbeat_loop` for the full rationale.
        self._heartbeat_task = spawn(
            self._heartbeat_loop(),
            name="voice-pipeline-heartbeat",
        )

        # Mission Phase 1b — register runtime listeners (MM notification
        # + driver-update). Each listener registers in its own
        # try/except via ``_register_listeners`` so one failing doesn't
        # block the other. Failed registrations are not added to
        # ``self._listeners`` so the symmetric ``_unregister_listeners``
        # in ``stop()`` only sees successful registrations.
        self._register_listeners()

        logger.info(
            "VoicePipeline started",
            mind_id=self._config.mind_id,
            wake_word=self._config.wake_word_enabled,
        )

    async def stop(self) -> None:
        """Stop the pipeline and drain in-flight work before returning.

        Mission Phase 1 T1.10 — pre-fix the call set ``_running=False``
        and returned immediately, leaving any in-flight TTS synthesis
        task to push audio onto a closed pipeline (the user heard
        stale audio after explicit stop). Post-fix sequence:

        1. Emit ``voice.pipeline.stop_begin`` so dashboards see the
           tear-down boundary.
        2. Set ``_running=False`` so :meth:`feed_frame` short-circuits
           with ``"not_running"`` for any concurrent producer.
        2.5. (PIPELINE-5, 2026-07-02) Snapshot + cancel + drain
           ``_in_flight_cogloop_tasks`` — the cogloop bridge tasks are
           the UPSTREAM producers of speak/stream_text calls. Pre-fix
           stop() never touched them, so a stop during an active turn
           left the LLM bridge streaming into the stopped pipeline:
           re-opening SPEAKING (a dual-writer against step 7's IDLE),
           re-ducking the mic the gate release below had just
           released, and enqueuing audio post-stop. Quiesced FIRST —
           before the output/drainer/TTS teardown — so nothing
           re-populates the surfaces the later steps tear down. The
           speak/stream_text ``_running`` guards are the companion
           belt for a task that ignores cancellation within budget.
        3. Snapshot ``_filler_task`` BEFORE :meth:`_cancel_filler`
           nulls it out, then await the cancellation with a
           ``_CANCELLATION_TASK_TIMEOUT_S`` budget.
        4. Interrupt the output queue (idempotent).
        5. Cancel + drain the streaming background drainer
           (``_stream_drain_task``) with the same bounded budget and
           clear ``_speech_session_active`` so a mid-stream stop
           doesn't leave the next session claiming an open turn.
        6. Snapshot ``_in_flight_tts_tasks``, cancel each, await with
           the same per-task budget. ``CancelledError`` + ``TimeoutError``
           both count as "drained" so a wedged TTS backend doesn't
           stall :meth:`stop`; unexpected exceptions log a structured
           WARN but don't propagate.
        7. Reset state, release the self-feedback duck, emit
           ``voice.pipeline.stop_complete`` with drain counters.
        """
        logger.info("voice.pipeline.stop_begin", mind_id=self._config.mind_id)

        self._running = False

        # Step 2.5 (PIPELINE-5) — cancel + drain in-flight cogloop
        # bridge tasks first; see the docstring for the full rationale.
        # The set is mutated only on the loop thread (see
        # register_cogloop_task) so a plain snapshot needs no lock.
        # The bridge converts its own CancelledError into a sentinel
        # result, so `await shield(task)` typically returns normally;
        # CancelledError/TimeoutError are equally fine — either way we
        # proceed with teardown (stop must never raise or stall).
        cogloop_snapshot = tuple(self._in_flight_cogloop_tasks)
        cogloop_drained = 0
        for cogloop_task in cogloop_snapshot:
            if cogloop_task.done():
                cogloop_drained += 1
                continue
            cogloop_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(cogloop_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
                cogloop_drained += 1
            except (asyncio.CancelledError, TimeoutError):
                cogloop_drained += 1
            except Exception as exc:  # noqa: BLE001 — stop must never raise
                logger.warning(
                    "voice.pipeline.stop_cogloop_task_unexpected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Snapshot the filler task BEFORE _cancel_filler() nulls it.
        filler_task = self._filler_task
        filler_was_active = filler_task is not None and not filler_task.done()
        self._cancel_filler()
        if filler_was_active and filler_task is not None:
            # CancelledError is the expected outcome of cancel();
            # TimeoutError means the filler ignored cancellation within
            # budget — tracked via filler_was_active so the
            # stop_complete log surfaces it. Both terminate the wait
            # without propagating; see AP-27 for the suppress pattern.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(filler_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
            logger.debug(
                "voice.pipeline.stop_filler_drain_attempted",
                reason="best-effort wait for filler cancellation",
            )

        # v0.31.7 CR3 — cancel + drain the wall-clock heartbeat task.
        # Same drain pattern as the filler task above: cancel, await
        # with bounded timeout, treat CancelledError + TimeoutError as
        # success (a wedged heartbeat must NOT stall pipeline stop).
        # The task self-exits when ``_running`` flips False at the
        # next sleep boundary, but we still cancel for liveness:
        # without cancel a 2 s sleep would hold stop for up to one
        # full heartbeat interval.
        heartbeat_task = self._heartbeat_task
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(heartbeat_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
        self._heartbeat_task = None

        # Interrupt active playback (idempotent).
        self._output.interrupt()

        # Cancel + drain the streaming background drainer (same
        # bounded pattern as the heartbeat task above). The interrupt
        # just issued makes it exit at the next slice boundary; the
        # cancel covers a drainer parked before its first slice.
        drain_task = self._stream_drain_task
        self._stream_drain_task = None
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(drain_task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
        self._speech_session_active = False

        # Snapshot the in-flight TTS set so iteration is stable while
        # tasks self-remove via _untrack_tts_task in their finally
        # blocks (same pattern as cancel_speech_chain step 2).
        #
        # T1.13 — snapshot acquires ``_task_tracking_lock`` briefly to
        # serialize against concurrent ``_track_tts_task``; iteration
        # outside the lock for the same reason as cancel_speech_chain.
        async with self._task_tracking_lock:
            tts_snapshot = tuple(self._in_flight_tts_tasks)
        tts_drained = 0
        for task in tts_snapshot:
            if task.done():
                tts_drained += 1
                continue
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_CANCELLATION_TASK_TIMEOUT_S,
                )
                tts_drained += 1
            except (asyncio.CancelledError, TimeoutError):
                # Both count as drained — CancelledError is the
                # success path; TimeoutError means we asked nicely
                # within budget and the task didn't honour it, but
                # we still leave the orchestrator in a quiesced state.
                tts_drained += 1
            except Exception as exc:  # noqa: BLE001 — stop must never raise
                logger.warning(
                    "voice.pipeline.stop_tts_task_unexpected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        self._state = VoicePipelineState.IDLE
        self._utterance_frames.clear()
        if self._self_feedback_gate is not None:
            # Release the duck so a mid-TTS stop doesn't leave the
            # capture normalizer attenuated for the next session.
            self._self_feedback_gate.on_tts_end()

        # Mission Phase 1b — unregister runtime listeners. Each
        # unregister is best-effort + logged on failure so a wedged
        # WMI service or COM marshalling glitch doesn't block pipeline
        # shutdown.
        self._unregister_listeners()

        logger.info(
            "voice.pipeline.stop_complete",
            mind_id=self._config.mind_id,
            tts_tasks_drained=tts_drained,
            tts_tasks_total=len(tts_snapshot),
            cogloop_tasks_drained=cogloop_drained,
            cogloop_tasks_total=len(cogloop_snapshot),
            filler_was_active=filler_was_active,
        )
        logger.info("VoicePipeline stopped", mind_id=self._config.mind_id)
