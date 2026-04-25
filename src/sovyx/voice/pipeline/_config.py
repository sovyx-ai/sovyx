"""Voice pipeline orchestrator configuration + bounds-validated invariants.

Re-exported from :mod:`sovyx.voice.pipeline.__init__`.

Mission band-aids #11 / #37 / #38 (Appendix A) flagged the pre-hardening
:func:`validate_config` for accepting only lower bounds — a runaway
``max_recording_frames=99999`` (5+ minutes of capture before forced
end), an absurd ``filler_delay_ms=300000`` (5 minutes of filler
silence before the daemon thinks anything's wrong), or an empty
``filler_phrases`` tuple with ``fillers_enabled=True`` would all pass
validation and produce mysterious user-facing failures at runtime.
The hardened validator enforces upper-bound sanity ceilings sourced
from perceptual + Sovyx-empirical thresholds and rejects the
"impossible-but-not-explicitly-checked" config classes at construction
time.
"""

from __future__ import annotations

from dataclasses import dataclass

from sovyx.voice.pipeline._constants import (
    _BARGE_IN_THRESHOLD_FRAMES,
    _FILLER_DELAY_MS,
    _MAX_RECORDING_FRAMES,
    _SILENCE_FRAMES_END,
)

# ---------------------------------------------------------------------------
# Bounds-validated invariants (mission band-aids #11 / #37 / #38)
# ---------------------------------------------------------------------------
#
# Upper bounds sourced from perceptual + product constraints:
# * filler_delay_ms <= 10s — beyond 10s of silence the user assumes
#   the daemon froze; this is the SLO ceiling for time-to-filler.
# * silence_frames_end <= 250 — at 32 ms/frame = 8s of trailing
#   silence before utterance ends; longer would let a single
#   utterance span minutes of dead air.
# * max_recording_frames <= 1875 — 60s of speech per utterance.
#   Voice-assistant turns longer than 60s are pathological (the
#   user is dictating, not conversing); cap with a hard ceiling so
#   a stuck VAD can't capture forever.
# * barge_in_threshold <= 50 — at 32 ms/frame = 1.6s of sustained
#   speech to trigger barge-in. Higher would mean the user's
#   interruption only registers after they've spoken for >1.5s,
#   which defeats the purpose of barge-in.

_FILLER_DELAY_MS_MAX = 10_000
"""10 seconds — the SLO ceiling for time-to-filler. Beyond this the
user assumes the daemon froze regardless of what's actually running
upstream. Sourced from voice-assistant perceptual research (Hello
Magenta UX, Speechmatics 2026 latency benchmark)."""

_SILENCE_FRAMES_END_MAX = 250
"""250 frames * 32 ms = 8 s. End-of-utterance budget: longer trailing
silence means a single utterance can span minutes of dead air —
pathological, surfaces as "the daemon's stuck" to the user."""

_MAX_RECORDING_FRAMES_MAX = 1_875
"""1875 frames * 32 ms = 60 s. Per-utterance recording ceiling: voice
assistant turns longer than 60 s are dictation, not conversation —
a stuck VAD would otherwise capture forever (memory growth, garbage
STT)."""

_BARGE_IN_THRESHOLD_MAX = 50
"""50 frames * 32 ms = 1.6 s. Sustained-speech ceiling for barge-in:
higher would mean the user's interruption only registers after >1.5s
of continuous speech, defeating barge-in latency budget."""

_FILLER_PHRASES_MAX = 50
"""Catalog ceiling for ``filler_phrases``. Larger catalogs are a
config smell — the orchestrator picks one phrase at random per
filler firing, so 50 distinct phrases is already a 2% per-phrase
selection probability (effectively unbounded variation) without
the catalog itself becoming a maintenance burden."""


@dataclass(frozen=True, slots=True)
class VoicePipelineConfig:
    """Configuration for the VoicePipeline orchestrator.

    Attributes:
        mind_id: Owning mind identifier.
        wake_word_enabled: Whether to require wake word before recording.
        barge_in_enabled: Whether user can interrupt TTS by speaking.
        fillers_enabled: Whether to play filler phrases during LLM thinking.
        filler_delay_ms: Milliseconds to wait before playing a filler.
        silence_frames_end: Consecutive silent frames to end utterance (~32ms each).
        max_recording_frames: Maximum frames before force-ending recording.
        barge_in_threshold: Consecutive speech frames to trigger barge-in.
        confirmation_tone: Type of tone on wake word (``"beep"`` or ``"none"``).
        filler_phrases: Phrases used during LLM thinking time.
    """

    mind_id: str = "default"
    wake_word_enabled: bool = True
    barge_in_enabled: bool = True
    fillers_enabled: bool = True
    filler_delay_ms: int = _FILLER_DELAY_MS
    silence_frames_end: int = _SILENCE_FRAMES_END
    max_recording_frames: int = _MAX_RECORDING_FRAMES
    barge_in_threshold: int = _BARGE_IN_THRESHOLD_FRAMES
    confirmation_tone: str = "beep"
    filler_phrases: tuple[str, ...] = (
        "Let me think about that...",
        "Hmm...",
        "One moment...",
        "Let me check...",
        "Sure, let me look into that...",
    )


def validate_config(config: VoicePipelineConfig) -> None:
    """Validate pipeline configuration with both lower AND upper bounds.

    Pre-hardening this function only enforced lower bounds — a
    runaway ``max_recording_frames=99999`` (5+ minutes per utterance)
    or ``filler_delay_ms=300000`` (5 minutes of dead air before any
    user-perceptible signal) would silently pass and produce
    pathological runtime behaviour. The hardened validator rejects
    impossible-but-not-explicitly-checked configurations at
    construction so the failure is loud (ValueError on instantiation)
    rather than mysterious (the daemon just sits there).

    Raises:
        ValueError: If any parameter is out of range. The error
            message names the offending field, the value, and the
            permitted range so operators can correct without
            consulting the source.
    """
    # mind_id sanity — the orchestrator stamps it on every event;
    # an empty string defeats dashboards' per-mind aggregation.
    if not config.mind_id or not config.mind_id.strip():
        msg = "mind_id must be a non-empty string"
        raise ValueError(msg)

    # Numeric bounds — both floor (always existed) AND ceiling (new).
    if config.filler_delay_ms < 0:
        msg = f"filler_delay_ms must be >= 0, got {config.filler_delay_ms}"
        raise ValueError(msg)
    if config.filler_delay_ms > _FILLER_DELAY_MS_MAX:
        msg = (
            f"filler_delay_ms must be <= {_FILLER_DELAY_MS_MAX} "
            f"(SLO ceiling for time-to-filler), got {config.filler_delay_ms}"
        )
        raise ValueError(msg)
    if config.silence_frames_end < 1:
        msg = f"silence_frames_end must be >= 1, got {config.silence_frames_end}"
        raise ValueError(msg)
    if config.silence_frames_end > _SILENCE_FRAMES_END_MAX:
        msg = (
            f"silence_frames_end must be <= {_SILENCE_FRAMES_END_MAX} "
            f"(end-of-utterance ceiling), got {config.silence_frames_end}"
        )
        raise ValueError(msg)
    if config.max_recording_frames < 1:
        msg = f"max_recording_frames must be >= 1, got {config.max_recording_frames}"
        raise ValueError(msg)
    if config.max_recording_frames > _MAX_RECORDING_FRAMES_MAX:
        msg = (
            f"max_recording_frames must be <= {_MAX_RECORDING_FRAMES_MAX} "
            f"(per-utterance ceiling = 60s), got {config.max_recording_frames}"
        )
        raise ValueError(msg)
    if config.barge_in_threshold < 1:
        msg = f"barge_in_threshold must be >= 1, got {config.barge_in_threshold}"
        raise ValueError(msg)
    if config.barge_in_threshold > _BARGE_IN_THRESHOLD_MAX:
        msg = (
            f"barge_in_threshold must be <= {_BARGE_IN_THRESHOLD_MAX} "
            f"(barge-in latency budget = 1.6s), got {config.barge_in_threshold}"
        )
        raise ValueError(msg)

    # Closed-set enums.
    if config.confirmation_tone not in ("beep", "none"):
        msg = f"confirmation_tone must be 'beep' or 'none', got {config.confirmation_tone!r}"
        raise ValueError(msg)

    # filler_phrases consistency — fillers_enabled with an empty
    # catalog is incoherent: the orchestrator would attempt to pick
    # a random phrase and crash on the empty sequence.
    if config.fillers_enabled and not config.filler_phrases:
        msg = "fillers_enabled=True requires a non-empty filler_phrases catalog"
        raise ValueError(msg)
    if len(config.filler_phrases) > _FILLER_PHRASES_MAX:
        msg = (
            f"filler_phrases catalog must contain <= {_FILLER_PHRASES_MAX} entries, "
            f"got {len(config.filler_phrases)} (catalog smell — selection "
            f"probability already vanishingly small at the cap)"
        )
        raise ValueError(msg)
    # Per-phrase sanity — empty / whitespace-only phrases would render
    # as silence to the TTS, defeating the filler's purpose.
    for idx, phrase in enumerate(config.filler_phrases):
        if not phrase or not phrase.strip():
            msg = (
                f"filler_phrases[{idx}] must be a non-empty / non-whitespace "
                f"string (empty fillers render as silent TTS)"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# AudioOutputQueue — managed playback with interruption
# ---------------------------------------------------------------------------
