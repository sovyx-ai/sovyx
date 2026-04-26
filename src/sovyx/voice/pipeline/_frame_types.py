"""Frame-typed pipeline observability layer (mission §1.1 Hybrid Option C).

Pipecat-inspired typed frames that wrap state-transition + atomic
cancellation events with structured metadata. The frames are an
**observability layer** — the orchestrator's authoritative state still
lives in :class:`VoicePipelineState` + the boolean flags that have
30 days of production validation. Frames are emitted at the same
points where state mutates, recorded in a bounded ring buffer, and
exposed via the :meth:`PipelineStateMachine.frame_history` accessor
(Step 12) and the ``GET /api/voice/frame-history`` endpoint (Step 15).

Why typed frames matter:

* **Trace ID propagation** — every frame carries the per-utterance
  ``utterance_id`` minted at wake-word fire, so dashboards can
  reconstruct the full capture → VAD → STT → LLM → TTS span set with
  one filter (Mission §2.6 Ring 6 contract).
* **Atomic cancellation context** — :class:`BargeInInterruptionFrame`
  captures all 5 step verdicts of the T1 cancellation chain in one
  frozen object, so post-incident forensics can answer "what failed
  during the barge-in" without crawling 5 separate log lines.
* **Pipecat alignment** — frame names + semantics mirror the canonical
  Pipecat reference set (UserStartedSpeakingFrame, TranscriptionFrame,
  LLMFullResponseStartFrame, OutputAudioRawFrame, EndFrame). Future
  v0.24.0+ refactor to a full Pipecat state-machine rewrite finds the
  vocabulary already in place.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 11.
Pipecat frame docs: https://reference-server.pipecat.ai/en/stable/api/pipecat.frames.frames.html
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PipelineFrame:
    """Base class for every frame.

    Every frame carries:

    * ``frame_type`` — string discriminator. Subclasses set this to
      a stable Pipecat-aligned label (``"UserStartedSpeaking"``,
      ``"TranscriptionFrame"``, …). Used by dashboards + JSON
      serialisation; pinned by tests so a future rename is loud.
    * ``timestamp_monotonic`` — :func:`time.monotonic` at frame
      construction. Monotonic so frames can be ordered relative to
      each other even across NTP corrections / DST boundaries. The
      :meth:`time.time` wall-clock equivalent lives in dashboards
      via the structured-log timestamp; the monotonic one lives in
      the frame so post-incident forensics can compute exact deltas
      without a clock-skew compensation step.
    * ``utterance_id`` — UUID4 minted at the utterance boundary
      (wake-word fire OR no-wake recording start OR external
      proactive ``speak``). Empty between utterances. Mirrors the
      :attr:`VoicePipeline._current_utterance_id` field that already
      flows through the structured-log namespace.

    Frozen + slotted because the orchestrator emits frames into a
    bounded ring buffer that must be safe to share across the
    state-machine lock + the dashboard read path. Mutation would
    invalidate observers' assumptions.
    """

    frame_type: str
    timestamp_monotonic: float
    utterance_id: str = ""


@dataclass(frozen=True, slots=True)
class UserStartedSpeakingFrame(PipelineFrame):
    """Wake-word fire OR barge-in onset.

    Emitted from :meth:`VoicePipeline._handle_wake_detected` (line 727
    of orchestrator) and :meth:`_transition_to_recording_from_barge_in`.
    The dashboard's call-flow widget renders this as the "user
    started speaking" timeline event.

    The frame_type label matches Pipecat's canonical
    ``UserStartedSpeakingFrame`` (https://reference-server.pipecat.ai/
    en/stable/api/pipecat.frames.frames.html#UserStartedSpeakingFrame)
    so future v0.24.0+ Pipecat-state-machine refactors find the
    vocabulary already in place.
    """

    source: str = ""
    """Either ``"wake_word"`` or ``"barge_in"``."""


@dataclass(frozen=True, slots=True)
class UserStoppedSpeakingFrame(PipelineFrame):
    """VAD silence threshold met after a recording window.

    Emitted from the orchestrator's recording → transcribing
    transition. Carries the Silero probability snapshot at the
    moment silence was declared so dashboards can correlate the
    transition timing with the VAD probability curve.
    """

    silero_prob_snapshot: float = 0.0
    """Last VAD probability before the silence threshold fired."""


@dataclass(frozen=True, slots=True)
class TranscriptionFrame(PipelineFrame):
    """STT output post-validation.

    Emitted AFTER the S1+S2 hallucination + logprob + timeout guards
    have run. Carries the validated transcript + the confidence /
    language metadata that the rejection guards used.
    """

    text: str = ""
    confidence: float = 0.0
    language: str = ""


@dataclass(frozen=True, slots=True)
class LLMFullResponseStartFrame(PipelineFrame):
    """LLM dispatch boundary (state IDLE/RECORDING/THINKING transition).

    Emitted from the orchestrator's transcribing → thinking
    transition. Carries the model identifier + the request-id so
    dashboards can correlate the frame with the LLM router's own
    structured logs.
    """

    model: str = ""
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class LLMFullResponseEndFrame(PipelineFrame):
    """LLM finished generating (state THINKING → SPEAKING transition).

    Carries the rough output length + the elapsed time so dashboards
    can render LLM-side latency without correlating against the
    router log.
    """

    output_chars: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class OutputAudioRawFrame(PipelineFrame):
    """One TTS chunk emitted to the output queue.

    Emitted per-chunk during streaming TTS synthesis. Each frame
    carries the chunk index + the PCM byte count + the synthesis
    health verdict so dashboards can render real-time TTS chunk
    progress. The PCM bytes themselves are NOT carried in the frame
    (would balloon the bounded ring buffer); they live in the
    output queue.
    """

    chunk_index: int = 0
    pcm_bytes: int = 0
    sample_rate: int = 0
    synthesis_health: str = "ok"


@dataclass(frozen=True, slots=True)
class BargeInInterruptionFrame(PipelineFrame):
    """T1 atomic cancellation chain context.

    The most semantically important frame: it captures the contract
    of :meth:`VoicePipeline.cancel_speech_chain` (mission §3.4 T1
    refactor) in one frozen object. ``step_results`` carries one
    entry per chain step (``output_flush``, ``tts_tasks_cancel``,
    ``llm_cancel``, ``filler_and_gate``, ``text_buffer_cleanup``)
    with the verdict (``"ok"``, ``"failed"``, ``"timeout"``,
    ``"no_hook_registered"``).

    Emitted at chain entry with the trigger ``reason`` (typically
    ``"barge_in"`` from :meth:`_handle_speaking`, but also
    ``"shutdown"`` and ``"manual_cancel"`` per the chain's docstring).
    The terminal frame is emitted at chain exit with all 5 step
    verdicts populated.

    Post-incident forensics can answer "what failed during the
    barge-in" without crawling 5 separate ``voice.tts.cancellation_*``
    log lines — every step's outcome is in one place, in one order,
    on one frame.
    """

    reason: str = ""
    step_results: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EndFrame(PipelineFrame):
    """Terminal IDLE transition (utterance complete).

    Emitted at every ``self._state = VoicePipelineState.IDLE``
    site after a recording → transcribing → thinking → speaking
    cycle (or any error path that returns to IDLE). The terminal
    frame closes the trace ID's span set so dashboards know to
    finalise the per-utterance timeline.
    """

    reason: str = ""
    """Terminal reason. Typical values: ``"tts_finished"``,
    ``"stt_error"``, ``"stt_timeout"``, ``"stt_confidence_reject"``,
    ``"llm_error"``, ``"llm_cancel"``, ``"output_error"``,
    ``"empty_recording"``, ``"reset"``."""


__all__ = [
    "BargeInInterruptionFrame",
    "EndFrame",
    "LLMFullResponseEndFrame",
    "LLMFullResponseStartFrame",
    "OutputAudioRawFrame",
    "PipelineFrame",
    "TranscriptionFrame",
    "UserStartedSpeakingFrame",
    "UserStoppedSpeakingFrame",
]


def _frame_to_dict(frame: PipelineFrame) -> dict[str, Any]:
    """Serialise a frame to a JSON-safe dict for dashboard transport.

    The orchestrator's :meth:`frame_history` accessor (Step 15) calls
    this to produce the ``GET /api/voice/frame-history`` response
    payload. Lives in this module so the encoding rule is colocated
    with the frame definitions.

    The dict shape is:
    ``{frame_type: str, timestamp_monotonic: float, utterance_id: str,
       ...subclass-specific fields...}``
    """
    from dataclasses import asdict

    return asdict(frame)
