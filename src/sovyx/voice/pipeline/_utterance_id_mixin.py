"""Per-utterance trace ID mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's per-turn utterance-id lifecycle: a fresh UUID4
minted at every utterance boundary (wake-word detected, no-wake
recording start, external ``speak`` without prior context) and cleared
at every terminal back-to-IDLE transition (TTS completed, error path,
false-wake rejection, empty transcription).

Pre-extraction this surface lived as 1 property + 2 methods on the
single-class ``VoicePipeline`` god file. See CLAUDE.md anti-pattern
#16 for the carve-out rationale — third strike of the Phase 5.F.19+
orchestrator split (heartbeat + wake-word were the first two).

The trace ID flows through ``PipelineFrame.utterance_id`` so every
recorded frame in the bounded ring buffer carries the same ID across
a turn — see ``_record_frame`` for the stamping site (still on host).

Anti-pattern #32 contract: zero cross-mixin method calls. Only
attribute reads/writes on host-owned fields. The TYPE_CHECKING block
forward-declares those fields so mypy strict resolves the references
without creating runtime attributes that would interfere with the
host's ``__init__`` order.

State the mixin reads/writes (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_current_utterance_id: str`` — the active utterance's trace ID.
  Empty string ``""`` between turns by construction (cleared at every
  back-to-IDLE transition).
* ``_current_mind_id: str`` — per-turn authoritative mind. Reset to
  the config default at every clear so the next turn's wake-word
  detection re-resolves via the router.
* ``_llm_thinking_start_monotonic: float | None`` — Phase 3.B.1 anchor
  for the next-turn End frame's ``elapsed_ms`` measurement. Cleared at
  every utterance boundary so a barge-in-cancelled chain doesn't leak
  the OLD anchor into the NEW turn.
* ``_config.mind_id`` — read at clear time to seed the next turn's
  ``_current_mind_id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.voice._observability_pii import mint_utterance_id

if TYPE_CHECKING:
    from sovyx.voice.pipeline._config import VoicePipelineConfig


class UtteranceIdentityMixin:
    """Per-turn trace-ID mint + clear lifecycle.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the public read accessor + the
    private mint/clear lifecycle methods.

    See module docstring for the full responsibility carve-out.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads/writes. Declared
        # TYPE_CHECKING so mypy strict resolves the references without
        # creating runtime attributes that would interfere with the
        # host's own initialisation order.
        _current_utterance_id: str
        _current_mind_id: str
        _llm_thinking_start_monotonic: float | None
        _config: VoicePipelineConfig

    @property
    def current_utterance_id(self) -> str:
        """Trace ID of the in-flight utterance, or ``""`` between turns.

        Read-only accessor for downstream components (LLM router, TTS
        engine, observability bridges) that want to stamp the same
        trace context on their own structured logs / spans without
        re-deriving it. Empty string when the pipeline is IDLE — by
        construction, the orchestrator clears the field at every
        terminal back-to-IDLE transition.
        """
        return self._current_utterance_id

    def _mint_new_utterance_id(self) -> str:
        """Mint a fresh UUID4 for the next utterance and stash it.

        Called at every utterance boundary (wake-word detected,
        no-wake recording start, external ``speak`` without prior
        context). Safe to call when an id is already set — the new
        one replaces the previous (covers the barge-in path where
        the prior utterance is being torn down at the same moment
        the new one starts). Returns the minted id for the caller's
        immediate use (event stamping, log emission), avoiding a
        second attribute read on the hot path.
        """
        new_id = mint_utterance_id()
        self._current_utterance_id = new_id
        return new_id

    def _clear_utterance_id(self) -> None:
        """Reset the current utterance id back to the empty sentinel.

        Called at every terminal back-to-IDLE transition (TTS
        completed, error path, false-wake rejection, empty
        transcription) so the next utterance is guaranteed a fresh
        mint instead of re-using the prior trace. Idempotent —
        safe to call when already empty.

        Phase 8 / T8.10 — also resets the per-turn ``_current_mind_id``
        back to the orchestrator's config default. The next IDLE
        path's wake-word detection re-resolves the matched mind via
        the router (if wired) before the next downstream emission.
        """
        self._current_utterance_id = ""
        # Reset per-turn mind context to the config default so the
        # next turn's WakeWordDetectedEvent starts clean.
        self._current_mind_id = self._config.mind_id
        # v0.32.3 Phase 3.B.1 — drop any THINKING anchor that the prior
        # turn left dangling (e.g. a barge-in cancelled the speech chain
        # before ``speak()``/``flush_stream()`` ran their End-frame
        # emit). Without this reset, the next turn's End frame would
        # carry an ``elapsed_ms`` measured against the OLD turn's
        # THINKING start. The next THINKING entry resets the anchor
        # for the next turn, but only if this site clears the leak.
        self._llm_thinking_start_monotonic = None
