"""Speech streaming + TTS-out mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's outbound speech surface — the public APIs
the cognitive layer uses to deliver an LLM response to the user:

* ``speak`` — proactive single-shot TTS (full text in one call).
* ``stream_text`` — token-streaming with sentence-boundary
  segmentation + per-segment failure resilience (Mission Phase 1 /
  T1.21 consecutive-failure abort).
* ``flush_stream`` — finalize streaming (emit the remaining buffer,
  drain the output queue, transition back to IDLE).
* ``start_thinking`` — THINKING-state entry that arms the filler
  task (Jarvis Illusion §3 — fill the pre-LLM-token gap).
* ``set_stream_segment_guard`` — wire/unwire the per-segment safety
  guard the cognitive bridge registers so streamed sentence segments
  are filtered BEFORE synthesis.
* ``_emit_llm_full_response_end_frame`` — observability helper that
  pairs every THINKING-Start frame with a matching End frame on the
  bounded ring buffer (v0.32.3 Phase 3.B.1 — closes audit gap P0.B2).

Pre-extraction this surface lived as 5 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — ninth strike of the Phase 5.F.19+ orchestrator
split.

Anti-pattern #32 contract: the mixin makes 5 cross-mixin method
calls — all forward-declared in the TYPE_CHECKING block so MRO
resolves the real implementations on sibling mixins at runtime:

* ``self._record_frame(...)`` — FrameRecordingMixin.
* ``self._mint_new_utterance_id()`` — UtteranceIdentityMixin.
* ``self._clear_utterance_id()`` — UtteranceIdentityMixin.
* ``self._emit(...)`` — TtsCancelChainMixin.
* ``self._synthesize_tracked(...)`` — TtsCancelChainMixin.
* ``self._observe_speaker_drift(...)`` — TtsCancelChainMixin.
* ``self._cancel_filler()`` — TtsCancelChainMixin.

Constants moved with the methods + re-exported from ``_orchestrator``
for back-compat with tests that import them via the orchestrator
module path:

* ``_CONSECUTIVE_TTS_FAILURE_THRESHOLD`` — Mission Phase 1 / T1.21
  streaming TTS abort threshold.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_state`` — VoicePipelineState property+setter on host (state
  machine transitions go through host's saga lifecycle).
* ``_self_feedback_gate: SelfFeedbackGate | None`` — mic-ducking
  gate.
* ``_current_utterance_id: str`` — read for log attribution.
* ``_text_buffer: str`` — accumulator for stream_text segments.
* ``_consecutive_tts_segment_failures: int`` — T1.21 counter.
* ``_tts_segment_failure_lock: asyncio.Lock`` — guards the counter.
* ``_first_token_event: asyncio.Event`` — filler-await synchronizer.
* ``_filler_task: asyncio.Task[bool] | None`` — pending filler task.
* ``_jarvis: JarvisIllusion`` — filler player.
* ``_output: AudioOutputQueue`` — playback target.
* ``_config: VoicePipelineConfig`` — read for mind_id + fillers_enabled.
* ``_llm_thinking_start_monotonic: float | None`` — THINKING-Start
  anchor for the End-frame elapsed_ms computation.
* ``_speech_session_active: bool`` — turn-ownership flag so the
  surface that opened the speech session (speak vs stream) is the
  one that closes it / falls back to IDLE.
* ``_stream_drain_task: asyncio.Task[None] | None`` — background
  playback drainer so the first synthesized segment plays while the
  LLM is still generating.
* ``_stream_segment_guard: Callable[[str], str] | None`` — per-
  segment safety hook (regex-tier output/PII guard) applied before
  synthesis; fail-closed on guard errors.
* ``_running: bool`` — pipeline-active gate; ``speak`` /
  ``stream_text`` refuse to open a session on a stopped pipeline
  (PIPELINE-5, 2026-07-02).
* ``_barge_in: BargeInDetector`` — sustain counter, reset at every
  session open so a run accumulated in a previous SPEAKING window
  never leaks into the next turn's gate (PIPELINE-2 redesign).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.jarvis import split_at_boundaries
from sovyx.voice.pipeline._events import (
    PipelineErrorEvent,
    TTSCompletedEvent,
    TTSStartedEvent,
)
from sovyx.voice.pipeline._frame_types import (
    LLMFullResponseEndFrame,
    OutputAudioRawFrame,
)
from sovyx.voice.pipeline._state import VoicePipelineState

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.voice.health._self_feedback import SelfFeedbackGate
    from sovyx.voice.jarvis import JarvisIllusion
    from sovyx.voice.pipeline._barge_in import BargeInDetector
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._frame_types import PipelineFrame
    from sovyx.voice.pipeline._output_queue import AudioOutputQueue

logger = get_logger(__name__)


_CONSECUTIVE_TTS_FAILURE_THRESHOLD = _VoiceTuning().pipeline_consecutive_tts_failure_threshold
"""Mission Phase 1 / T1.21 — streaming TTS abort threshold. See
``VoiceTuningConfig.pipeline_consecutive_tts_failure_threshold``."""


class SpeechStreamingMixin:
    """Outbound speech delivery — speak / stream_text / flush_stream.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the public + private outbound
    speech surface.

    See module docstring for the full responsibility carve-out + the
    anti-pattern #32 cross-mixin reference contract.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes.
        _state: VoicePipelineState
        _self_feedback_gate: SelfFeedbackGate | None
        _current_utterance_id: str
        _text_buffer: str
        _consecutive_tts_segment_failures: int
        _tts_segment_failure_lock: asyncio.Lock
        _first_token_event: asyncio.Event
        _filler_task: asyncio.Task[bool] | None
        _jarvis: JarvisIllusion
        _output: AudioOutputQueue
        _config: VoicePipelineConfig
        _llm_thinking_start_monotonic: float | None
        _speech_session_active: bool
        _stream_drain_task: asyncio.Task[None] | None
        _stream_segment_guard: Callable[[str], str] | None
        _running: bool
        _barge_in: BargeInDetector

        # Cross-mixin host-resident methods (resolved via MRO at
        # runtime). Anti-pattern #32 case (b) forward declarations —
        # TYPE_CHECKING-only so MRO falls through to the real
        # implementations on sibling mixins.
        def _record_frame(self, frame: PipelineFrame) -> None: ...
        def _mint_new_utterance_id(self) -> str: ...
        def _clear_utterance_id(self) -> None: ...
        async def _emit(self, event: object) -> None: ...
        async def _synthesize_tracked(self, text: str) -> Any: ...  # noqa: ANN401 — TTS chunk type varies by engine
        async def _observe_speaker_drift(self, chunk: Any) -> None: ...  # noqa: ANN401 — TTS chunk type varies by engine
        def _cancel_filler(self) -> None: ...

    def _emit_llm_full_response_end_frame(self, output_chars: int) -> None:
        """Emit :class:`LLMFullResponseEndFrame` at the THINKING → SPEAKING boundary.

        v0.32.3 Phase 3.B.1 — closes audit gap P0.B2. Pre-fix the End
        frame was defined in :mod:`_frame_types` but had zero emit
        sites; dashboards never saw the THINKING-span close so LLM-
        side latency couldn't be rendered without correlating against
        the LLM router's own logs.

        Emit semantics:
            * ``output_chars`` — rough character length of the LLM
              response handed to TTS (full text for ``speak()``;
              ``len(text_buffer)`` for ``flush_stream()``).
            * ``elapsed_ms`` — clock-monotonic delta between the
              matching :class:`LLMFullResponseStartFrame` (set by
              ``_llm_thinking_start_monotonic`` at the THINKING entry)
              and the End frame's emission. ``0`` when no THINKING
              phase preceded the speak (proactive cogloop initiative).
            * Suppressed entirely when ``_llm_thinking_start_monotonic``
              is ``None`` to avoid emitting a misleading "End" frame
              for a turn that had no observable "Start" — matches the
              ring buffer's start-end-frame pairing contract that
              dashboards rely on for the SPEAKING-after-THINKING span.

        Once emitted, ``_llm_thinking_start_monotonic`` is cleared so
        a follow-up speak/flush in the same turn doesn't double-emit.
        The next THINKING entry resets the anchor for the next turn.
        """
        if self._llm_thinking_start_monotonic is None:
            return
        now_monotonic = time.monotonic()
        elapsed_ms = max(
            0,
            int((now_monotonic - self._llm_thinking_start_monotonic) * 1000),
        )
        self._record_frame(
            LLMFullResponseEndFrame(
                frame_type="LLMFullResponseEnd",
                timestamp_monotonic=now_monotonic,
                output_chars=output_chars,
                elapsed_ms=elapsed_ms,
            ),
        )
        self._llm_thinking_start_monotonic = None

    def set_stream_segment_guard(
        self,
        guard: Callable[[str], str] | None,
    ) -> None:
        """Wire (or unwire) the per-segment safety guard for streaming TTS.

        The cognitive bridge registers the loop's regex-tier output/PII
        guard here so streamed segments are filtered BEFORE synthesis.
        Pre-fix the streaming path structurally bypassed the safety
        controls ActPhase applies on the batch path (raw LLM text was
        spoken; the guarded text existed only on the text surface).

        The guard MUST be sync and cheap (<1 ms regex-tier by
        contract); it receives one sentence segment and returns the
        guarded text (empty string drops the segment). Pass ``None``
        to unwire (bridge teardown).
        """
        self._stream_segment_guard = guard

    def _apply_segment_guard(self, segment: str) -> str:
        """Return ``segment`` filtered through the registered guard.

        Fail-closed: a guard exception drops the segment (returns "")
        with an ERROR log — for a safety control, speaking unguarded
        text on a guard bug is worse than skipping the segment. No-op
        pass-through when no guard is registered (voice-only
        deployments without a cognitive bridge).
        """
        guard = self._stream_segment_guard
        if guard is None:
            return segment
        try:
            return guard(segment)
        except Exception:  # noqa: BLE001 — safety guard must fail closed
            logger.error(
                "voice.tts.segment_guard_failed_segment_dropped",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.segment_chars": len(segment),
                    "voice.utterance_id": self._current_utterance_id,
                },
                exc_info=True,
            )
            return ""

    def _ensure_stream_drainer(self) -> None:
        """Start the background playback drainer if none is running.

        Called after every successful ``enqueue`` in the streaming
        path so the first synthesized segment starts playing while the
        LLM is still generating (the advertised ~300 ms perceived
        latency). ``drain()`` exits when the queue momentarily empties
        between segments; the next enqueue re-arms it. Single-flight:
        at most one drainer exists, so it can never pop the queue
        concurrently with ``flush_stream``'s final drain (which awaits
        this task first).
        """
        task = self._stream_drain_task
        if task is not None and not task.done():
            return
        self._stream_drain_task = spawn(
            self._output.drain(),
            name="voice-stream-drain",
        )

    async def speak(self, text: str) -> None:
        """Synthesize and play text (called by CogLoop.act).

        PIPELINE-5 (2026-07-02): refuses to run on a stopped pipeline
        — a bridge task surviving ``stop()`` must not re-open a speech
        session, re-duck the mic, or enqueue audio into the torn-down
        output surface.

        Args:
            text: Text to speak.
        """
        if not self._running:
            logger.debug(
                "voice.tts.speak_ignored_not_running",
                mind_id=self._config.mind_id,
                text_chars=len(text),
            )
            return
        # v0.32.3 Phase 3.B.1 — emit the LLMFullResponseEndFrame BEFORE
        # the SPEAKING transition so the frame ring buffer reflects
        # THINKING-end → SPEAKING-start in temporal order. Helper is a
        # no-op when no preceding THINKING phase fired (proactive
        # cogloop speak), preserving the start-end pairing contract.
        self._emit_llm_full_response_end_frame(output_chars=len(text))
        # Session boundary — zero the barge-in sustain counter so a
        # speech run accumulated in a previous SPEAKING window can't
        # trip the gate on this turn's first frame (PIPELINE-2).
        self._barge_in.reset()
        # Open the speech session BEFORE the state write so a frame
        # arriving between the SPEAKING transition and playback start
        # (synthesis takes hundreds of ms) can't misread "not yet
        # playing" as "finished" and flap the state back to IDLE.
        self._speech_session_active = True
        self._state = VoicePipelineState.SPEAKING
        # Step 13 frame emission — TTS speak boundary. Per-chunk
        # OutputAudioRawFrame frames will be emitted as chunks land
        # in the output queue (subsequent commit). Here we record
        # the speak entry as a chunk_index=0 marker so the
        # frame_history reflects the full SPEAKING span.
        self._record_frame(
            OutputAudioRawFrame(
                frame_type="OutputAudioRaw",
                timestamp_monotonic=time.monotonic(),
                chunk_index=0,
                pcm_bytes=0,
                sample_rate=0,
                synthesis_health="speak_started",
            ),
        )
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_start()
        # External proactive ``speak`` (e.g. cognitive layer's
        # initiative) without a preceding wake/recording mints its
        # own trace id so dashboards still get a per-turn span set;
        # an existing id from the wake → STT → think chain is
        # preserved (single logical utterance).
        utterance_id = self._current_utterance_id or self._mint_new_utterance_id()
        await self._emit(
            TTSStartedEvent(
                mind_id=self._config.mind_id,
                utterance_id=utterance_id,
            )
        )

        try:
            chunk = await self._synthesize_tracked(text)
            await self._output.play_immediate(chunk)
        except (VoiceError, RuntimeError, OSError) as exc:
            # TTS backends (Piper, Kokoro, cloud) share the same
            # failure profile as STT — typed subsystem errors, ONNX
            # runtime failures, and I/O. Emit a pipeline error event
            # so the cognitive loop knows the utterance didn't speak.
            logger.error(
                "TTS failed",
                error=str(exc),
                exc_info=True,
                **{"voice.utterance_id": utterance_id},
            )
            await self._emit(
                PipelineErrorEvent(
                    mind_id=self._config.mind_id,
                    error=f"TTS failed: {exc}",
                    utterance_id=utterance_id,
                )
            )
        finally:
            self._speech_session_active = False
            if self._state is not VoicePipelineState.RECORDING:
                # A barge-in during playback hands the turn to
                # RECORDING via ``_handle_speaking``; a late finally
                # must not clobber it back to IDLE (the barged-in
                # utterance would be silently dropped).
                self._state = VoicePipelineState.IDLE
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_end()
            await self._emit(
                TTSCompletedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=utterance_id,
                )
            )
            self._clear_utterance_id()

    async def stream_text(self, text_chunk: str) -> None:
        """Stream text from LLM to TTS for speculative synthesis.

        Called by CogLoop as LLM tokens arrive.  Accumulates text and
        synthesizes at sentence boundaries (Jarvis Illusion §3).

        2026-07-02 (PIPELINE-1/5) entry guards:

        * Stopped pipeline — same refusal as :meth:`speak`; a bridge
          task surviving ``stop()`` must not stream into the torn-down
          surfaces.
        * RECORDING ownership — when ``_handle_speaking`` has handed
          the turn to the USER via barge-in (state = RECORDING), a
          late/rogue LLM chunk must NOT re-assert SPEAKING, re-open
          the session, or re-duck the mic over the user's recording
          (the #69 dual-writer class). The chunk belongs to a
          discarded utterance — drop it with a structured WARN.

        Args:
            text_chunk: Partial LLM output text.
        """
        if not self._running:
            logger.debug(
                "voice.tts.stream_text_ignored_not_running",
                mind_id=self._config.mind_id,
                chunk_chars=len(text_chunk),
            )
            return
        if self._state is VoicePipelineState.RECORDING:
            logger.warning(
                "voice.tts.stream_text_dropped_recording_owns_turn",
                **{
                    "voice.mind_id": self._config.mind_id,
                    "voice.chunk_chars": len(text_chunk),
                    "voice.utterance_id": self._current_utterance_id,
                    "voice.action_required": (
                        "An LLM stream delivered a chunk after barge-in "
                        "handed the turn to the user's RECORDING. The "
                        "chunk was dropped. If this repeats, the "
                        "upstream cancellation chain (llm_cancel / "
                        "cogloop_tasks_cancel) is not stopping the LLM "
                        "stream — check voice.tts.cancellation_chain "
                        "verdicts."
                    ),
                },
            )
            return
        self._speech_session_active = True
        if self._state != VoicePipelineState.SPEAKING:
            # Session boundary — zero the barge-in sustain counter
            # (same rationale as speak(); PIPELINE-2).
            self._barge_in.reset()
            self._state = VoicePipelineState.SPEAKING
            if self._self_feedback_gate is not None:
                self._self_feedback_gate.on_tts_start()
            # Streaming path mints a trace id only when the cognitive
            # layer fed text without a preceding wake/STT chain. The
            # common path is wake → STT → THINKING → stream_text,
            # where ``_current_utterance_id`` is already set from
            # the wake-word mint.
            utterance_id = self._current_utterance_id or self._mint_new_utterance_id()
            await self._emit(
                TTSStartedEvent(
                    mind_id=self._config.mind_id,
                    utterance_id=utterance_id,
                )
            )

        # Cancel filler if still pending
        if not self._first_token_event.is_set():
            self._first_token_event.set()

        self._text_buffer += text_chunk
        segments = split_at_boundaries(self._text_buffer)

        # Synthesize all complete segments
        for segment in segments[:-1]:
            guarded_segment = self._apply_segment_guard(segment)
            if not guarded_segment.strip():
                continue
            try:
                chunk = await self._synthesize_tracked(guarded_segment)
                await self._output.enqueue(chunk)
                # Start playback immediately — do not wait for
                # flush_stream's end-of-response drain.
                self._ensure_stream_drainer()
                # T1.39 — observe the spectral-centroid drift on every
                # successfully-emitted chunk. The DSP runs in a worker
                # thread (CLAUDE.md anti-pattern #14 — keep CPU-bound
                # work off the asyncio loop, even sub-millisecond
                # bursts). On drift the WARN + PipelineErrorEvent
                # mirror the T1.36 / T1.19 / T1.20 pattern; no
                # automatic fallback (operator-disruptive without
                # explicit opt-in). Skipped entirely when the gate is
                # disabled so resource-constrained deployments pay
                # zero DSP cost.
                await self._observe_speaker_drift(chunk)
                # Mission Phase 1 / T1.21 — successful segment resets
                # the consecutive-failure counter so a transient
                # hiccup mid-stream doesn't poison the rest of the
                # response. Inlined here (rather than in a try-else
                # clause) because the try block also has an
                # ``except asyncio.CancelledError`` clause and Python
                # forbids ``else`` between ``except`` clauses.
                # v0.31.7 T3.5 (LOW.4) — guard with the failure-counter
                # lock so a future parallel stream_text caller can't
                # race the read+write across awaits.
                async with self._tts_segment_failure_lock:
                    self._consecutive_tts_segment_failures = 0
            except (VoiceError, RuntimeError, OSError) as exc:
                # Per-segment resilience during streaming: skip the
                # bad segment, keep speaking the rest. Traceback
                # preserved so persistent TTS failures don't hide.
                logger.warning(
                    "Stream TTS failed",
                    error=str(exc),
                    exc_info=True,
                )
                # Mission Phase 1 / T1.21 — track consecutive failures
                # and abort the stream when the TTS backend is wedged
                # (model corrupt, runtime OOM, infinite-loop bug).
                # Pre-T1.21 the loop kept iterating forever burning
                # compute on every incoming LLM segment with no
                # audible output. ``_consecutive_tts_segment_failures``
                # resets on the first successful segment below.
                # v0.31.7 T3.5 (LOW.4) — guard increment + threshold
                # check with the failure-counter lock so a future
                # parallel stream_text caller can't race the read+write.
                async with self._tts_segment_failure_lock:
                    self._consecutive_tts_segment_failures += 1
                    threshold_crossed = (
                        self._consecutive_tts_segment_failures
                        >= _CONSECUTIVE_TTS_FAILURE_THRESHOLD
                    )
                if threshold_crossed:
                    buffered_chars = len(self._text_buffer)
                    self._text_buffer = ""
                    logger.error(
                        "voice.tts.stream_aborted_consecutive_failures",
                        **{
                            "voice.mind_id": self._config.mind_id,
                            "voice.consecutive_failures": (self._consecutive_tts_segment_failures),
                            "voice.threshold": _CONSECUTIVE_TTS_FAILURE_THRESHOLD,
                            "voice.last_error": str(exc)[:200],
                            "voice.last_error_type": type(exc).__name__,
                            "voice.buffered_text_chars_dropped": buffered_chars,
                            "voice.action_required": (
                                "TTS backend produced consecutive errors. "
                                "Check the engine state (Piper model file "
                                "integrity, Kokoro ONNX session, or cloud "
                                "endpoint reachability via `sovyx doctor "
                                "voice`). Stream aborted to release the "
                                "cognitive layer; the next utterance will "
                                "rebuild from a clean state."
                            ),
                        },
                    )
                    await self._emit(
                        PipelineErrorEvent(
                            mind_id=self._config.mind_id,
                            error=(
                                f"stream_aborted_consecutive_failures "
                                f"(count={self._consecutive_tts_segment_failures}, "
                                f"last={type(exc).__name__})"
                            ),
                            utterance_id=self._current_utterance_id,
                        )
                    )
                    # Reset counter so the next stream_text call
                    # starts clean — the abort already broke the
                    # current stream's contract with the caller.
                    # v0.31.7 T3.5 (LOW.4) — same lock as above.
                    async with self._tts_segment_failure_lock:
                        self._consecutive_tts_segment_failures = 0
                    return
            except asyncio.CancelledError:
                # Two distinct cancellation sources land here as the
                # same exception, at the same awaits:
                #
                # 1. INNER synth-task cancel — cancel_speech_chain
                #    step 2 cancelled the tracked TTS task while this
                #    coroutine awaited it (``_synthesize_tracked`` →
                #    ``await task``). The stream task itself was NOT
                #    cancelled: swallow, clean the buffer, and return
                #    so the caller survives the barged-in segment.
                # 2. TASK-level cancel — THIS task (the cogloop /
                #    bridge task) is being cancelled: chain step 2.5
                #    (cogloop task cancel), step 3's _llm_cancel_hook,
                #    pipeline.stop(), or loop teardown. MUST re-raise
                #    (PIPELINE-1, 2026-07-02): pre-fix the swallow ate
                #    the cancellation, the LLM kept streaming, and the
                #    next stream_text call re-asserted SPEAKING over
                #    the user's RECORDING and re-opened the
                #    session/duck the chain had just closed (the exact
                #    #69 dual-writer class the VTI mission fixed).
                #
                # py3.11+ discriminator: ``Task.cancel()`` bumps the
                # target task's ``cancelling()`` count before
                # delivering the exception; cancelling an INNER task
                # never touches the OUTER task's count. So
                # ``current_task().cancelling() > 0`` is True exactly
                # for source 2. No ``uncancel()`` bookkeeping is
                # needed: path 2 re-raises (cancellation completes
                # normally) and path 1 never had a pending cancel.
                #
                # Mission Phase 1 / T1.15 — clear ``_text_buffer`` in
                # BOTH paths (this handler can be reached without the
                # chain running; chain step 5 stays as the belt-and-
                # suspenders cleanup for paths that never touch
                # ``stream_text`` at all).
                buffered_chars = len(self._text_buffer)
                self._text_buffer = ""
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    logger.info(
                        "voice.tts.stream_text_task_cancelled",
                        mind_id=self._config.mind_id,
                        buffered_text_chars=buffered_chars,
                    )
                    raise
                logger.info(
                    "voice.tts.stream_text_cancelled",
                    mind_id=self._config.mind_id,
                    buffered_text_chars=buffered_chars,
                )
                return

        # Keep incomplete segment in buffer
        self._text_buffer = segments[-1] if segments else ""

    async def flush_stream(self, *, discard_buffer: bool = False) -> None:
        """Flush remaining buffered text to TTS.

        Call when the LLM stream ends to synthesize the last segment.

        Args:
            discard_buffer: When True, drop the residual text buffer
                instead of synthesizing it. The bridge's barge-in
                cancellation path uses this — pre-fix the cancelled
                bridge called the normal flush, which SYNTHESIZED and
                ENQUEUED the interrupted response's tail, so the user
                heard a fragment of the cancelled utterance after
                barging in.

        T1.34 — every cancellation path in this method now interrupts
        the output queue before exiting. Pre-T1.34 the
        ``except asyncio.CancelledError`` in the synthesize block
        cleared the text buffer but left any audio already enqueued by
        prior chunks of the streaming session sitting in the output
        queue, and a cancellation landing on the final
        ``await self._output.drain()`` likewise leaked queued audio.
        ``cancel_speech_chain`` always interrupts the output queue at
        step 1 BEFORE it cancels in-flight tasks (step 2), so the
        normal barge-in path was already covered transitively. T1.34
        closes the off-path cases — asyncio loop teardown during
        daemon shutdown, an external task cancelling the flush via
        ``task.cancel()`` without going through ``cancel_speech_chain``
        — by making the interrupt explicit here. Belt + suspenders;
        ``interrupt()`` is idempotent so the upstream
        ``cancel_speech_chain`` path is unaffected.

        v0.32.3 Phase 3.B.1 — emit :class:`LLMFullResponseEndFrame`
        on entry. This is the canonical "LLM done generating" signal
        for the streaming path: ``stream_text`` is called per-chunk
        and can't tell which chunk is the last, so the cognitive
        bridge invokes ``flush_stream`` once the LLM router signals
        completion. The frame's ``output_chars`` reports the residual
        buffer length (the chunks already synthesised landed in the
        per-chunk ``OutputAudioRawFrame`` ring); dashboards correlate
        the End frame's ``elapsed_ms`` with the matching Start frame
        to render LLM-side latency.
        """
        # v0.32.3 Phase 3.B.1 — close the THINKING-span observability
        # by emitting the matching End frame even if the residual
        # buffer is empty (LLM streamed to clean sentence boundaries).
        # Helper is a no-op when no THINKING phase preceded
        # (proactive flush from cogloop initiative).
        self._emit_llm_full_response_end_frame(
            output_chars=len(self._text_buffer),
        )
        if discard_buffer:
            self._text_buffer = ""
        guarded_tail = (
            self._apply_segment_guard(self._text_buffer) if self._text_buffer.strip() else ""
        )
        if guarded_tail.strip():
            try:
                chunk = await self._synthesize_tracked(guarded_tail)
                await self._output.enqueue(chunk)
            except asyncio.CancelledError:
                # T1: cancelled by cancel_speech_chain mid-flush —
                # discard the tail (the user already barged in) and
                # let the next turn rebuild from a clean buffer.
                self._text_buffer = ""
                # T1.34 — clear any audio already enqueued during this
                # flush_stream call so the next utterance starts with
                # an empty output queue. ``interrupt()`` is idempotent
                # against the cancel_speech_chain step-1 interrupt that
                # routed us here in the barge-in case; on off-path
                # cancellations (loop teardown, direct task.cancel())
                # this is the ONLY interrupt that runs.
                with contextlib.suppress(Exception):
                    self._output.interrupt()
                logger.info(
                    "voice.tts.flush_cancelled",
                    mind_id=self._config.mind_id,
                )
                return
            except (VoiceError, RuntimeError, OSError) as exc:
                # Final-segment flush — losing this tail means the
                # user hears an abrupt cut, but the loop advances.
                # Traceback on warning so a broken TTS config surfaces.
                logger.warning(
                    "Flush TTS failed",
                    error=str(exc),
                    exc_info=True,
                )
        self._text_buffer = ""

        # Hand-off from the background drainer: await it BEFORE the
        # final drain so two drain() calls never pop the queue
        # concurrently. The drainer exits on its own once the queue
        # empties (or immediately after a barge-in interrupt), so this
        # await is bounded by remaining playback.
        drainer = self._stream_drain_task
        self._stream_drain_task = None
        if drainer is not None and not drainer.done():
            try:
                await drainer
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    self._output.interrupt()
                raise
            except Exception:  # noqa: BLE001 — drainer failure must not lose the tail
                logger.warning(
                    "voice.tts.stream_drainer_failed",
                    mind_id=self._config.mind_id,
                    exc_info=True,
                )

        # Drain all queued audio
        try:
            await self._output.drain()
        except asyncio.CancelledError:
            # T1.34 — drain was cancelled mid-flight. Clear remaining
            # audio (drain WAITS for playback to finish; it does not
            # itself empty the queue, so a cancel here leaves the
            # queue non-empty). Interrupt + re-raise so the cancellation
            # still propagates to the caller.
            with contextlib.suppress(Exception):
                self._output.interrupt()
            raise

        self._speech_session_active = False
        if self._state is VoicePipelineState.RECORDING:
            # A barge-in handed the turn to RECORDING while this flush
            # was in flight (slow cogloop cancellation past the chain's
            # await budget). Writing IDLE here would silently drop the
            # barged-in utterance — leave the state alone; the chain
            # already emitted the barge-in observability.
            return
        completed_utterance_id = self._current_utterance_id
        self._state = VoicePipelineState.IDLE
        if self._self_feedback_gate is not None:
            self._self_feedback_gate.on_tts_end()
        await self._emit(
            TTSCompletedEvent(
                mind_id=self._config.mind_id,
                utterance_id=completed_utterance_id,
            )
        )
        self._clear_utterance_id()

    async def start_thinking(self) -> None:
        """Start the thinking phase — initiate filler timer.

        Call this when CogLoop begins processing (before LLM tokens arrive).
        If the LLM doesn't respond within ``filler_delay_ms``, a filler
        phrase is played.

        v0.31.7 T3.1 (M4) — cancels any previous ``_filler_task`` BEFORE
        spawning the new one. Pre-T3.1 a rapid double-call (proactive
        cogloop initiative racing a wake-driven turn) overwrote the
        attribute without cancelling the prior task; the orphan kept
        running and its ``play_filler_after_delay`` could enqueue audio
        during the next turn. ``_cancel_filler`` is idempotent — safe
        to call when no task is in flight.
        """
        self._state = VoicePipelineState.THINKING
        self._first_token_event.clear()

        # v0.31.7 T3.1 — guard against a rapid second start_thinking
        # before the prior filler completed. The cancel is sync + cheap
        # (just task.cancel()); the cancelled task drains on the next
        # event loop tick.
        self._cancel_filler()

        if self._config.fillers_enabled:
            self._filler_task = spawn(
                self._jarvis.play_filler_after_delay(self._output, self._first_token_event),
                name="voice-pipeline-filler",
            )
