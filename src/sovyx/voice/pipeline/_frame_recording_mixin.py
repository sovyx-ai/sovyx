"""Frame-recording observability mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's frame-stamping + bounded-ring-buffer record
path. Every ``PipelineFrame`` flowing through the orchestrator gets
its ``utterance_id`` stamped from the active turn's trace ID and is
recorded into the state machine's bounded ring for post-incident
forensics + dashboard ``GET /api/voice/frame-history``.

Pre-extraction this surface lived as 2 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 for the
carve-out rationale — fourth strike of the Phase 5.F.19+ orchestrator
split.

Mission §1.1 Hybrid Option C contract: frame recording is
**observability-only** — it does NOT replace the boolean-flag +
``VoicePipelineState`` authoritative state (anti-pattern #25 + #29).
Every recording is wrapped in a best-effort try/except that absorbs
state-machine lock contention or ring overflow under chaos injection,
so the orchestrator's authoritative state mutation path is NEVER
blocked by observability work.

The mixin currently has 11 callers inside the orchestrator (state
transitions, end-recording, transcription, speak/flush_stream
end-frame emission, etc.) plus 1 external caller in
``voice/capture/_restart_mixin.py`` (T32 cross-component channel for
:class:`CaptureRestartFrame`). All `self._record_frame(...)` calls
resolve through MRO; the public ``record_capture_restart`` surface
keeps its narrow ``CaptureRestartFrame``-only signature so the
cross-component contract stays minimal.

Anti-pattern #32 contract: zero cross-mixin method calls. Only
attribute reads on host-owned fields (``_current_utterance_id`` +
``_state_machine``). The TYPE_CHECKING block forward-declares those
fields so mypy strict resolves the references without creating
runtime attributes that would interfere with the host's ``__init__``
order.

State the mixin reads (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_current_utterance_id: str`` — stamped on every recorded frame so
  the bounded ring buffer aligns by utterance for forensic queries.
  Owned by ``UtteranceIdentityMixin`` but initialised on the host
  (mixin contract — see ``UtteranceIdentityMixin`` docstring).
* ``_state_machine: PipelineStateMachine`` — the bounded ring buffer
  owner. ``record_frame`` is the public API on that class.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice.pipeline._frame_types import CaptureRestartFrame, PipelineFrame
    from sovyx.voice.pipeline._state_machine import PipelineStateMachine

logger = get_logger(__name__)


class FrameRecordingMixin:
    """Frame-stamping + bounded-ring-buffer recording.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the stamp + record path.

    See module docstring for the full responsibility carve-out.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads. Declared TYPE_CHECKING
        # so mypy strict resolves the references without creating
        # runtime attributes that would interfere with the host's own
        # initialisation order.
        _current_utterance_id: str
        _state_machine: PipelineStateMachine

    def _record_frame(self, frame: PipelineFrame) -> None:
        """Stamp utterance_id + record the frame on the state machine.

        Mission §1.1 Hybrid Option C: observability-only. Frame
        recording is best-effort — any exception during recording
        (e.g. state machine lock contention under chaos injection) is
        absorbed so the orchestrator's authoritative state mutation
        path is never blocked.

        Pre-condition: ``frame.timestamp_monotonic`` is set by the
        caller (typically ``time.monotonic()`` at the call site so the
        timestamp matches the real transition moment, not this helper's
        invocation moment).
        """
        try:
            # Frames are frozen dataclasses — to set utterance_id we
            # construct a copy via dataclasses.replace. The cost is one
            # allocation per recording, well below the bounded ring's
            # heartbeat budget.
            stamped = replace(frame, utterance_id=self._current_utterance_id)
            self._state_machine.record_frame(stamped)
        except Exception as exc:  # noqa: BLE001 — observability isolation
            logger.debug(
                "voice.pipeline.frame_record_skipped",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def record_capture_restart(self, frame: CaptureRestartFrame) -> None:
        """Public cross-component channel for :class:`CaptureRestartFrame`.

        T32 — the capture-task restart methods (``request_*_restart``
        on :class:`RestartMixin`) live OUTSIDE the orchestrator but
        emit observability frames into the same bounded ring buffer
        the orchestrator owns. Per CLAUDE.md anti-pattern #29 the
        frame is observability-only — does NOT replace the
        boolean-flag + ``VoicePipelineState`` authoritative state.

        Method is kept narrow on purpose: it accepts ONLY
        :class:`CaptureRestartFrame` instances rather than the
        general ``PipelineFrame`` parent class, so the public
        cross-component surface stays minimal. Other frame types
        continue to flow through the orchestrator-internal
        :meth:`_record_frame` path.

        Best-effort recording per :meth:`_record_frame`'s contract —
        any exception during state-machine record (lock contention,
        ring overflow under chaos injection) is absorbed so the
        capture-task restart path is never blocked by observability.

        Args:
            frame: The :class:`CaptureRestartFrame` to record. Caller
                MUST set ``timestamp_monotonic`` at the actual
                transition moment (typically just before the
                ring-buffer epoch increment in the restart method).
        """
        self._record_frame(frame)
