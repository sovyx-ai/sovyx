"""TTS task tracking + atomic cancellation chain mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's TTS-task lifecycle + the T1 transactional
barge-in cancellation chain. The chain coordinates 5 cleanup steps
under :attr:`_cancellation_lock` so concurrent barge-ins serialise
and each chain run produces a single auditable
``voice.tts.cancellation_chain`` event with per-step verdicts:

1. Output queue flush (idempotent ``interrupt()``).
2. In-flight TTS task cancellation under bounded budget.
3. (T3.2 / M5) Cogloop bridge task cancellation — fallback when
   ``_llm_cancel_hook`` is None during the CR1 race window.
4. Upstream LLM cancellation via the registered hook.
5. Filler + self-feedback gate cleanup.
6. Text-buffer cleanup (band-aid #15 final fix — pre-step-6 left
   ``_text_buffer`` populated, leaking residue into the next turn).

A terminal :class:`BargeInInterruptionFrame` is recorded on the
bounded ring buffer at chain exit so post-incident forensics can read
the full chain outcome from one frozen object.

Pre-extraction this surface lived as 9 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — eighth strike of the Phase 5.F.19+ orchestrator
split.

Anti-pattern #32 contract: the mixin calls ``self._record_frame(...)``
which lives on :class:`FrameRecordingMixin` (also in the
``VoicePipeline`` host's bases). MRO resolves the call as long as
this mixin is mounted alongside FrameRecordingMixin on the same host
— Python's C3 linearisation finds the real method even though this
mixin's ``if TYPE_CHECKING:`` block only sees the forward declaration.

Constants moved with the methods (re-exported from ``_orchestrator``
for back-compat with tests + start/stop's existing usage):

* ``_CANCELLATION_TASK_TIMEOUT_S`` — per-task timeout for the
  cancellation chain's await budget (T1 / Mission Phase 1 / T1.21).
* ``_SPEAKER_DRIFT_RATIO_THRESHOLD`` — T1.39 spectral-centroid drift
  threshold (read by ``_observe_speaker_drift``).
* ``_SPEAKER_DRIFT_WINDOW_SIZE`` — T1.39 rolling-window depth.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_event_bus`` — optional event bus for ``_emit``.
* ``_filler_task: asyncio.Task[Any] | None`` — pending filler task
  cancelled by ``_cancel_filler``.
* ``_first_token_event: asyncio.Event`` — set by ``_cancel_filler``
  to unblock filler-await callers.
* ``_speaker_consistency`` — optional T1.39 monitor (None when
  ``_SPEAKER_CONSISTENCY_ENABLED`` is False at init).
* ``_current_utterance_id`` — read by ``_observe_speaker_drift`` for
  log attribution.
* ``_config.mind_id`` — read for log attribution.
* ``_tts: TTSEngine`` — read by ``_synthesize_tracked``.
* ``_in_flight_tts_tasks: set[asyncio.Task[Any]]`` — task tracking
  set; mutated under ``_task_tracking_lock``.
* ``_in_flight_cogloop_tasks: set[asyncio.Task[Any]]`` — bridge task
  tracking set; mutated by ``register_cogloop_task`` + the done-
  callback.
* ``_task_tracking_lock: asyncio.Lock`` — guards mutations of
  ``_in_flight_tts_tasks``.
* ``_cancellation_lock: asyncio.Lock`` — serialises concurrent
  ``cancel_speech_chain`` invocations.
* ``_llm_cancel_hook: Callable[[], Awaitable[None]] | None`` —
  upstream LLM cancellation hook.
* ``_self_feedback_gate`` — optional mic-ducking gate.
* ``_output: AudioOutputQueue`` — read by step 1 of the chain.
* ``_text_buffer: str`` — wiped by step 6 of the chain.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice._speaker_consistency import compute_spectral_centroid
from sovyx.voice.pipeline._events import PipelineErrorEvent
from sovyx.voice.pipeline._frame_types import BargeInInterruptionFrame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice._speaker_consistency import SpeakerConsistencyMonitor
    from sovyx.voice.health._self_feedback import SelfFeedbackGate
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._frame_types import PipelineFrame
    from sovyx.voice.pipeline._output_queue import AudioOutputQueue
    from sovyx.voice.tts_piper import TTSEngine

logger = get_logger(__name__)

# ── Constants moved from _orchestrator.py with the methods ──────────
_CANCELLATION_TASK_TIMEOUT_S = _VoiceTuning().pipeline_cancellation_task_timeout_seconds
"""T1 atomic-cancellation chain — per-task timeout for cancelled
in-flight TTS tasks. See
``VoiceTuningConfig.pipeline_cancellation_task_timeout_seconds``."""

_SPEAKER_DRIFT_WINDOW_SIZE = _VoiceTuning().pipeline_speaker_drift_window_size
"""T1.39 — rolling-window depth for spectral-centroid drift
detection. See
``VoiceTuningConfig.pipeline_speaker_drift_window_size``."""

_SPEAKER_DRIFT_RATIO_THRESHOLD = _VoiceTuning().pipeline_speaker_drift_ratio_threshold
"""T1.39 — drift firing threshold (centroid_now / baseline). See
``VoiceTuningConfig.pipeline_speaker_drift_ratio_threshold``."""


class TtsCancelChainMixin:
    """TTS task tracking + T1 atomic cancellation chain.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the public + private TTS lifecycle
    surface + the chain orchestration.

    See module docstring for the full responsibility carve-out + the
    anti-pattern #32 cross-mixin reference contract.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes. Declared
        # TYPE_CHECKING so mypy strict resolves the references without
        # creating runtime attributes that would interfere with the
        # host's own initialisation order.
        _event_bus: Any
        _filler_task: asyncio.Task[Any] | None
        _first_token_event: asyncio.Event
        _speaker_consistency: SpeakerConsistencyMonitor | None
        _current_utterance_id: str
        _config: VoicePipelineConfig
        _tts: TTSEngine
        _in_flight_tts_tasks: set[asyncio.Task[Any]]
        _in_flight_cogloop_tasks: set[asyncio.Task[Any]]
        _task_tracking_lock: asyncio.Lock
        _cancellation_lock: asyncio.Lock
        _llm_cancel_hook: Callable[[], Awaitable[None]] | None
        _self_feedback_gate: SelfFeedbackGate | None
        _output: AudioOutputQueue
        _text_buffer: str
        _speech_session_active: bool
        _stream_drain_task: asyncio.Task[None] | None

        # Cross-mixin host-resident method (lives on FrameRecordingMixin,
        # resolved via MRO at runtime). Anti-pattern #32 case (b)
        # forward declaration — TYPE_CHECKING-only so MRO falls through
        # to the real implementation on FrameRecordingMixin.
        def _record_frame(self, frame: PipelineFrame) -> None: ...

    async def _emit(self, event: object) -> None:
        """Emit an event via the event bus (if available)."""
        if self._event_bus is not None:
            try:
                await self._event_bus.emit(event)
            except Exception:  # noqa: BLE001 — event bus emission isolation
                logger.warning("Event emission failed", event_type=type(event).__name__)

    def _cancel_filler(self) -> None:
        """Cancel pending filler task."""
        if self._filler_task is not None and not self._filler_task.done():
            self._filler_task.cancel()
            self._filler_task = None
        self._first_token_event.set()

    async def _observe_speaker_drift(self, chunk: Any) -> None:  # noqa: ANN401 — TTS chunk type varies by engine
        """Observe spectral-centroid drift on the freshly-emitted chunk.

        T1.39 — runs the centroid DSP in a worker thread (CLAUDE.md
        anti-pattern #14) and observes the result against the per-
        session rolling-window baseline. On drift exceeding
        :data:`_SPEAKER_DRIFT_RATIO_THRESHOLD` emits a structured
        WARN + :class:`PipelineErrorEvent` and continues — no
        automatic voice swap (too disruptive without operator opt-in;
        operators wanting fallback wire it via the existing
        ``synthesis_health`` field per T1.36).

        No-op when the speaker-consistency gate is disabled, when the
        chunk has no audio (zero-energy synthesis already covered by
        T1.36's ``synthesis_health="zero_energy"`` path), or when the
        rolling window is still warming up (first
        ``window_size - 1`` chunks of every session).

        The chunk type is engine-specific (``AudioChunk`` from
        ``tts_kokoro`` / ``tts_piper``; the orchestrator works with
        any value that has ``audio: npt.NDArray[np.int16]`` +
        ``sample_rate: int``).
        """
        if self._speaker_consistency is None:
            return
        audio = getattr(chunk, "audio", None)
        sample_rate = getattr(chunk, "sample_rate", 0)
        if audio is None or sample_rate <= 0:
            return
        centroid = await asyncio.to_thread(
            compute_spectral_centroid,
            audio,
            sample_rate,
        )
        drift, baseline, ratio = self._speaker_consistency.observe(centroid)
        if not drift:
            return
        logger.warning(
            "voice.tts.speaker_drift_detected",
            **{
                "voice.centroid_hz": round(centroid, 1),
                "voice.baseline_hz": round(baseline, 1),
                "voice.drift_ratio": round(ratio, 3),
                "voice.threshold_ratio": _SPEAKER_DRIFT_RATIO_THRESHOLD,
                "voice.window_size": _SPEAKER_DRIFT_WINDOW_SIZE,
                "voice.utterance_id": self._current_utterance_id,
                "voice.action_required": (
                    "Spectral centroid drifted >"
                    f"{int(_SPEAKER_DRIFT_RATIO_THRESHOLD * 100)}% from the "
                    "rolling-window baseline. Likely causes: voice file "
                    "partial download, ONNX session corruption, or a "
                    "buggy caller passing a different voice_id mid-"
                    "session. Check the TTS engine logs and run "
                    "`sovyx doctor voice` to verify model integrity."
                ),
            },
        )
        await self._emit(
            PipelineErrorEvent(
                mind_id=self._config.mind_id,
                error=(f"speaker_drift_detected (ratio={ratio:.3f}, baseline={baseline:.1f})"),
                utterance_id=self._current_utterance_id,
            )
        )

    async def _synthesize_tracked(self, text: str) -> Any:  # noqa: ANN401 — TTS chunk type varies
        """Synthesise ``text`` via a tracked task so T1 can cancel it.

        The pre-T1 pattern was ``await self._tts.synthesize(text)`` —
        the calling coroutine WAS the synthesis task, so an external
        observer (the barge-in path) had no way to cancel just the
        synth without cancelling its caller. T1 wraps each call in
        ``asyncio.create_task`` and registers it into
        :attr:`_in_flight_tts_tasks` so :meth:`cancel_speech_chain`
        can iterate and cancel transactionally.

        The task self-removes from the in-flight set in its own
        ``finally`` so the set stays bounded. CancelledError
        propagates so the caller (speak / stream_text / flush_stream)
        sees the cancellation and can take its own cleanup path.
        """
        task: asyncio.Task[Any] = asyncio.create_task(
            self._tts.synthesize(text),
            name=f"voice-tts-synth-{id(self) & 0xFFFF}",
        )
        await self._track_tts_task(task)
        try:
            return await task
        finally:
            await self._untrack_tts_task(task)

    # -- T1 atomic cancellation chain ---------------------------------------

    def register_llm_cancel_hook(
        self,
        hook: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Wire (or unwire) the upstream LLM cancellation hook (T1).

        The cognitive layer registers an awaitable that signals its LLM
        client to stop generating tokens. Called by the orchestrator's
        :meth:`cancel_speech_chain` (step 3 of the transactional chain)
        so a barge-in stops not just the audio output and TTS work but
        also the LLM upstream that's still producing the rest of the
        utterance.

        Pass ``None`` to unwire (e.g. when the cognitive layer tears
        down). Replacing a non-``None`` hook with another non-``None``
        hook is allowed (one cognitive layer hands off to another).

        The hook MUST be idempotent — :meth:`cancel_speech_chain` may
        invoke it multiple times across barge-in events and the chain
        contract requires the hook to never raise (catch + log
        internally) so chain-step accounting stays meaningful.
        """
        self._llm_cancel_hook = hook

    async def _track_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Register an in-flight TTS synthesis task for T1 cancellation.

        Called by :meth:`speak`, :meth:`stream_text`, and
        :meth:`flush_stream` whenever they spawn a TTS coroutine. The
        task removes itself in its own ``finally`` via
        :meth:`_untrack_tts_task` so the set stays bounded by the
        in-flight set, not the lifetime of the daemon.

        T1.13 — async + lock-guarded. The mutation itself is GIL-atomic
        in CPython, but the lock makes the atomicity guarantee
        explicit + survives a future refactor that would introduce an
        await between read-and-write. Same lock as
        :meth:`cancel_speech_chain`'s step-2 snapshot.
        """
        async with self._task_tracking_lock:
            self._in_flight_tts_tasks.add(task)

    async def _untrack_tts_task(self, task: asyncio.Task[Any]) -> None:
        """Remove ``task`` from the in-flight set. Safe to call multiple times.

        T1.13 — async + lock-guarded; same lock as :meth:`_track_tts_task`
        and :meth:`cancel_speech_chain`'s step-2 snapshot.
        """
        async with self._task_tracking_lock:
            self._in_flight_tts_tasks.discard(task)

    def register_cogloop_task(self, task: asyncio.Task[Any]) -> None:
        """Register an in-flight cogloop bridge task for T3.2 cancellation.

        v0.31.7 T3.2 (M5) — called by
        ``dashboard/routes/voice.py::_on_perception`` immediately after
        ``asyncio.create_task(_run_bridge_isolated())`` so the
        orchestrator's :meth:`cancel_speech_chain` (step 2.5) can fall
        back to task cancellation when ``_llm_cancel_hook`` is None.
        Without this, a barge-in during the CR1 race window
        (turn N's bridge ``finally`` already nulled the hook before
        turn N+1's ``register`` ran) had no fallback to stop the bridge.

        The task removes itself on completion via a done-callback so
        the set stays bounded by the in-flight count, not the daemon
        lifetime. Synchronous — no lock needed (the set is mutated
        only from the loop thread + the done-callback runs on the
        loop thread too).
        """
        self._in_flight_cogloop_tasks.add(task)
        task.add_done_callback(self._in_flight_cogloop_tasks.discard)

    async def cancel_speech_chain(self, *, reason: str = "barge_in") -> None:
        """Run the four-step transactional cancellation chain (T1).

        Steps in order, each recorded with a ``"ok"`` / ``"failed"`` /
        ``"timeout"`` verdict on the structured
        ``voice.tts.cancellation_chain`` event:

        1. **Output queue flush** — interrupt active playback so the
           user hears barge-in immediately. Synchronous + always
           succeeds (idempotent ``interrupt()``).
        2. **In-flight TTS task cancellation** — every task in
           :attr:`_in_flight_tts_tasks` is cancelled and awaited with
           :data:`_CANCELLATION_TASK_TIMEOUT_S` budget. A wedged task
           that doesn't honour CancelledError within the budget is
           recorded as ``cancellation_timeout`` so operators can spot
           a buggy TTS backend.
        3. **Upstream LLM cancellation** — the registered
           :attr:`_llm_cancel_hook` is awaited if present. Without
           this step, the LLM keeps producing tokens that flow into
           the next turn (the pre-T1 silent failure mode).
        4. **Filler + self-feedback gate cleanup** — cancel the
           pending filler task and release the mic-ducking gate.

        The entire chain runs under :attr:`_cancellation_lock` so
        concurrent barge-ins serialise; the second acquirer observes
        the post-first-chain state (empty in-flight set, output
        already stopped) and short-circuits naturally with all-ok
        verdicts.

        ``reason`` is recorded on the event so the dashboard can
        attribute the chain to its trigger (``"barge_in"`` from
        :meth:`_handle_speaking`, or future callers like
        ``"shutdown"``, ``"manual_cancel"``).
        """
        async with self._cancellation_lock:
            chain_started = time.monotonic()
            step_results: dict[str, str] = {}

            # Step 1: output queue flush. Idempotent — calling
            # interrupt() on a quiescent queue is a no-op.
            try:
                self._output.interrupt()
                step_results["output_flush"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                step_results["output_flush"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="output_flush",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            # Step 1.5: drain-task hand-off. The streaming background
            # drainer observes the step-1 interrupt at its next slice
            # boundary (~50 ms) and exits; await it briefly so playback
            # is genuinely silent before the caller transitions to
            # RECORDING (otherwise the tail of the assistant's own
            # audio can leak into the barged-in utterance's capture).
            drainer = self._stream_drain_task
            self._stream_drain_task = None
            if drainer is not None and not drainer.done():
                drainer.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(drainer),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as exc:  # noqa: BLE001 — chain shield
                    logger.warning(
                        "voice.tts.cancellation_step_failed",
                        step="stream_drainer",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            # The speech session is over regardless of which surface
            # opened it — release _handle_speaking's IDLE fallback.
            self._speech_session_active = False

            # Step 2: cancel + await in-flight TTS tasks. Snapshot the
            # set so iteration is stable while tasks remove themselves
            # via _untrack_tts_task in their own finally blocks.
            #
            # T1.13 — snapshot acquires ``_task_tracking_lock`` briefly
            # so a concurrent ``_track_tts_task`` cannot mutate the set
            # mid-snapshot. Iteration runs OUTSIDE the lock so the
            # awaits below don't block new TTS tasks indefinitely (the
            # residual race — new tasks created during iteration are
            # caught by the cognitive layer's LLM-cancel hook in
            # step 3, not by this snapshot).
            async with self._task_tracking_lock:
                tasks_snapshot = tuple(self._in_flight_tts_tasks)
            cancelled_count = 0
            timeout_count = 0
            for task in tasks_snapshot:
                if task.done():
                    continue
                task.cancel()
                cancelled_count += 1
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    # CancelledError is the EXPECTED outcome of
                    # cancelling — don't treat it as failure. Timeout
                    # means the task didn't honour the cancellation
                    # within budget; record separately so dashboards
                    # can spot wedged TTS backends.
                    if not task.done():
                        timeout_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "voice.tts.cancellation_task_unexpected_exception",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            step_results["tts_tasks_cancel"] = "ok" if timeout_count == 0 else "timeout"

            # Step 2.5 (v0.31.7 T3.2 / M5): cogloop bridge task cancel.
            # Belt-and-suspenders against the CR1 race window — when
            # ``_llm_cancel_hook`` is None (turn N's bridge ``finally``
            # ran after turn N+1's hook register, OR a future
            # regression), step 3 below records ``no_hook_registered``
            # and the LLM keeps producing tokens. Cancelling the
            # cogloop task here propagates CancelledError into the
            # cancel-hook-aware bridge body, which awaits the LLM
            # client's stream-cancellation in its CancelledError
            # handler. Snapshot to a tuple so the iteration is stable
            # while the done-callback removes tasks from the set.
            cogloop_snapshot = tuple(self._in_flight_cogloop_tasks)
            cogloop_cancelled = 0
            cogloop_timeout = 0
            for task in cogloop_snapshot:
                if task.done():
                    continue
                task.cancel()
                cogloop_cancelled += 1
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    if not task.done():
                        cogloop_timeout += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "voice.tts.cancellation_cogloop_unexpected_exception",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            step_results["cogloop_tasks_cancel"] = "ok" if cogloop_timeout == 0 else "timeout"

            # Step 3: upstream LLM cancellation. Best-effort — the hook
            # contract says "never raise", but we shield anyway so a
            # buggy hook can't take down the chain.
            if self._llm_cancel_hook is None:
                step_results["llm_cancel"] = "no_hook_registered"
            else:
                try:
                    await asyncio.wait_for(
                        self._llm_cancel_hook(),
                        timeout=_CANCELLATION_TASK_TIMEOUT_S,
                    )
                    step_results["llm_cancel"] = "ok"
                except TimeoutError:
                    step_results["llm_cancel"] = "timeout"
                except Exception as exc:  # noqa: BLE001 — hook isolation
                    step_results["llm_cancel"] = "failed"
                    logger.warning(
                        "voice.tts.cancellation_step_failed",
                        step="llm_cancel",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )

            # Step 4: filler + self-feedback gate cleanup. Synchronous
            # + idempotent, so wrap defensively but expect success.
            try:
                self._cancel_filler()
                if self._self_feedback_gate is not None:
                    self._self_feedback_gate.on_tts_end()
                step_results["filler_and_gate"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                step_results["filler_and_gate"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="filler_and_gate",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            # Step 5: text-buffer cleanup (band-aid #15 final fix).
            # Pre-step-5 the cancel_speech_chain left ``_text_buffer``
            # untouched on barge-in. If the LLM had streamed
            # "Hello, this is a long respo" before the user barged in,
            # the buffer kept "Hello, this is a long respo" and the
            # NEXT utterance's stream_text would prepend that residue
            # — the user heard "Hello, this is a long respo[NEW
            # TURN]" instead of the new turn cleanly. The T1 commit
            # cleaned the buffer in stream_text's CancelledError path,
            # but the broader cancel_speech_chain (called from
            # barge_in, shutdown, manual_cancel) never touched it.
            # This step closes that gap unconditionally — buffer is
            # always empty after a chain run, regardless of which
            # path triggered the chain.
            try:
                buffer_chars_dropped = len(self._text_buffer)
                self._text_buffer = ""
                step_results["text_buffer_cleanup"] = "ok"
            except Exception as exc:  # noqa: BLE001 — chain shield
                buffer_chars_dropped = 0
                step_results["text_buffer_cleanup"] = "failed"
                logger.warning(
                    "voice.tts.cancellation_step_failed",
                    step="text_buffer_cleanup",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            chain_duration_ms = (time.monotonic() - chain_started) * 1000.0
            logger.info(
                "voice.tts.cancellation_chain",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.reason": reason,
                    "voice.chain_duration_ms": round(chain_duration_ms, 2),
                    "voice.tasks_cancelled": cancelled_count,
                    "voice.tasks_timed_out": timeout_count,
                    "voice.has_llm_hook": self._llm_cancel_hook is not None,
                    "voice.text_buffer_chars_dropped": buffer_chars_dropped,
                    "voice.cogloop_tasks_cancelled": cogloop_cancelled,
                    "voice.cogloop_tasks_timed_out": cogloop_timeout,
                    "voice.step_output_flush": step_results["output_flush"],
                    "voice.step_tts_tasks_cancel": step_results["tts_tasks_cancel"],
                    "voice.step_cogloop_tasks_cancel": step_results["cogloop_tasks_cancel"],
                    "voice.step_llm_cancel": step_results["llm_cancel"],
                    "voice.step_filler_and_gate": step_results["filler_and_gate"],
                    "voice.step_text_buffer_cleanup": step_results["text_buffer_cleanup"],
                },
            )
            # Step 14 frame emission — the most semantically important
            # frame in the entire mission. Captures the T1 atomic
            # cancellation chain contract in one frozen object so
            # post-incident forensics can answer "what failed during
            # the barge-in" without crawling 5 separate
            # voice.tts.cancellation_step_failed log lines.
            #
            # Recorded AT CHAIN EXIT with all 5 step verdicts populated.
            # The frame is recorded INSIDE the cancellation lock so
            # observers see a consistent (chain-complete, frame-emitted)
            # state — concurrent barge-ins serialise on the lock + each
            # produces its own terminal frame.
            self._record_frame(
                BargeInInterruptionFrame(
                    frame_type="BargeInInterruption",
                    timestamp_monotonic=time.monotonic(),
                    reason=reason,
                    step_results=dict(step_results),
                ),
            )
