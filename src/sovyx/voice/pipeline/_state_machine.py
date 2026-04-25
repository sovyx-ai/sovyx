"""Voice pipeline state-machine validator + watchdog (Ring 6 — O1).

Wraps the existing :class:`VoicePipelineState` enum with a
**canonical transition table** + a per-state **dwell-time
watchdog**, both surfaced as observability primitives the
orchestrator can adopt incrementally.

Why a separate validator (not a refactor of the orchestrator's
``self._state = ...`` assignments)?

The orchestrator currently has 25+ direct state-mutation sites
across legitimate and edge-case paths. Forcing every site to call
``transition()`` in one commit would either:

* Break the pipeline on any pre-existing edge case the strict
  table didn't anticipate, OR
* Force the table to accept everything (defeating the purpose).

Enterprise sequencing: **observe → confirm → enforce**.

* Phase 1 (this commit): build the validator + watchdog as standalone
  primitives. Strict mode is off by default — invalid transitions
  log a structured WARN (``pipeline.state.invalid_transition``) so
  the orchestrator can be wired to ``record_transition()`` without
  any behavioural risk.
* Phase 2 (follow-up): wire ``record_transition()`` into every
  ``self._state = ...`` site in the orchestrator. Audit any WARN
  fires; either the table or the offending site is corrected.
* Phase 3 (follow-up): flip ``strict=True`` so future invalid
  transitions raise :class:`InvalidTransitionError` at write time.

The watchdog is independent of validation — it tracks
``time_in_current_state`` and exposes
:meth:`PipelineStateMachine.is_watchdog_expired` for the
orchestrator's existing heartbeat to query. A 30-second dwell in
e.g. ``THINKING`` is the canonical "stuck pipeline" symptom and
already triggers other recovery paths in the orchestrator; this
makes the dwell metric explicit + queryable.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6), §3.10 O1; GStreamer state-machine spec; Pipecat frame
model; CLAUDE.md anti-pattern #22 (use ``time.monotonic``, beware
Windows 15.6 ms tick).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sovyx.observability.logging import get_logger
from sovyx.voice.pipeline._state import VoicePipelineState

logger = get_logger(__name__)


_DEFAULT_WATCHDOG_THRESHOLD_S = 30.0
"""Default per-state dwell ceiling. 30 s matches the existing
orchestrator heartbeat watchdog window (mission §2.6 Ring 6) and
is comfortably above legitimate THINKING / SPEAKING durations
(LLM round-trip + TTS streaming) while catching truly stuck
states (deadlock, lost barge-in cancel, watchdog never fired)."""


_MIN_WATCHDOG_THRESHOLD_S = 0.5
_MAX_WATCHDOG_THRESHOLD_S = 600.0
"""Loud-fail bounds. < 0.5 s thrashes; > 10 min is effectively no
watchdog at all and a stuck pipeline blocks every utterance for a
user-visible duration."""


# ── Canonical transition table ─────────────────────────────────────


_CANONICAL_TRANSITIONS: dict[VoicePipelineState, frozenset[VoicePipelineState]] = {
    VoicePipelineState.IDLE: frozenset(
        {
            VoicePipelineState.IDLE,  # idempotent reset
            VoicePipelineState.WAKE_DETECTED,
            # Direct IDLE → RECORDING is legitimate when a user-initiated
            # capture starts without a wake word (e.g. push-to-talk via
            # dashboard or barge-in handoff during speech).
            VoicePipelineState.RECORDING,
        },
    ),
    VoicePipelineState.WAKE_DETECTED: frozenset(
        {
            VoicePipelineState.RECORDING,  # canonical happy path
            VoicePipelineState.IDLE,  # false-positive cancel
        },
    ),
    VoicePipelineState.RECORDING: frozenset(
        {
            VoicePipelineState.TRANSCRIBING,  # utterance ended, decode begins
            VoicePipelineState.IDLE,  # silence timeout / user cancel
        },
    ),
    VoicePipelineState.TRANSCRIBING: frozenset(
        {
            VoicePipelineState.THINKING,  # transcript ready, LLM dispatch
            VoicePipelineState.IDLE,  # empty/rejected transcript
        },
    ),
    VoicePipelineState.THINKING: frozenset(
        {
            VoicePipelineState.SPEAKING,  # LLM response ready, TTS begins
            VoicePipelineState.IDLE,  # LLM error / cancellation
            # THINKING → THINKING is legitimate when the orchestrator
            # restarts the LLM call after a transient failure inside
            # the same utterance.
            VoicePipelineState.THINKING,
        },
    ),
    VoicePipelineState.SPEAKING: frozenset(
        {
            VoicePipelineState.IDLE,  # TTS finished, return to listening
            VoicePipelineState.RECORDING,  # barge-in: skip wake, capture next
            # SPEAKING → SPEAKING is legitimate when the orchestrator
            # streams a follow-up TTS chunk inside the same utterance.
            VoicePipelineState.SPEAKING,
        },
    ),
}
"""Allowed (from, to) transitions for the voice pipeline.

Every state must be a key. The set of allowed targets is the union
of all observed-legitimate transitions in the v0.22 orchestrator
plus the mission §2.6 canonical happy-path graph. Self-loops are
included where the orchestrator legitimately reassigns the same
state inside one utterance (THINKING re-dispatch, SPEAKING chunk
stream, IDLE idempotent reset).

The table is a ``dict[State, frozenset[State]]`` so look-ups are
O(1) and the contents are immutable at module load time."""


def _validate_table_complete() -> None:
    """Sanity check at import time — every enum value has an entry.

    Catches the failure mode where a new state is added to
    :class:`VoicePipelineState` but the transition table isn't
    updated — without this, the validator would silently treat
    every transition out of the new state as invalid.
    """
    missing = set(VoicePipelineState) - set(_CANONICAL_TRANSITIONS.keys())
    if missing:
        names = sorted(s.name for s in missing)
        msg = (
            f"_CANONICAL_TRANSITIONS missing entries for: {names}. "
            f"Every VoicePipelineState value must declare its allowed "
            f"successors so transitions out of it are not silently "
            f"flagged invalid."
        )
        raise RuntimeError(msg)


_validate_table_complete()


# ── Exceptions + record types ──────────────────────────────────────


class InvalidTransitionError(Exception):
    """Raised when a strict-mode validator rejects a transition.

    Carries the offending ``(from, to)`` pair plus the canonical
    set of allowed targets so the operator can see WHICH legal
    transition the call site should have used instead.
    """

    def __init__(
        self,
        *,
        from_state: VoicePipelineState,
        to_state: VoicePipelineState,
        allowed: frozenset[VoicePipelineState],
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed
        allowed_names = sorted(s.name for s in allowed)
        msg = (
            f"invalid pipeline transition: {from_state.name} → "
            f"{to_state.name}. Allowed from {from_state.name}: "
            f"{allowed_names}"
        )
        super().__init__(msg)


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One observed transition — what happened, when, was it valid."""

    from_state: VoicePipelineState
    to_state: VoicePipelineState
    monotonic_at: float
    valid: bool


@dataclass(slots=True)
class _MachineState:
    """Mutable bookkeeping. Module-private — touched only via lock."""

    current: VoicePipelineState = VoicePipelineState.IDLE
    entered_monotonic: float = 0.0
    transition_count: int = 0
    invalid_transition_count: int = 0
    history: list[TransitionRecord] = field(default_factory=list)


# ── Public surface ─────────────────────────────────────────────────


def is_transition_allowed(
    from_state: VoicePipelineState,
    to_state: VoicePipelineState,
) -> bool:
    """Pure function — does the canonical table allow this transition?

    Used by the validator AND by call sites that want to test
    feasibility before attempting (e.g. an early-return check). No
    side effects, no telemetry.
    """
    return to_state in _CANONICAL_TRANSITIONS[from_state]


class PipelineStateMachine:
    """Observability-grade state machine for the voice pipeline.

    Wraps the bare :class:`VoicePipelineState` enum with:

    * Canonical transition table validation (logging WARN by default,
      raising in ``strict=True`` mode).
    * Per-state dwell timestamp + :meth:`is_watchdog_expired` query.
    * Bounded transition history for forensics
      (:attr:`history_capacity` newest records).
    * Thread-safe mutation via internal :class:`threading.Lock` —
      the orchestrator's state mutations come from both the event
      loop and the dashboard's RPC thread, so the machine cannot
      assume single-threaded access.

    The machine does NOT *own* the orchestrator's state — it
    OBSERVES it. The orchestrator continues to assign
    ``self._state = X``; alongside, it calls
    ``machine.record_transition(old, X)`` so the validator + watchdog
    + history can keep up. This decoupling is what makes incremental
    adoption safe (anti-pattern #11/#12).
    """

    def __init__(
        self,
        *,
        watchdog_threshold_s: float = _DEFAULT_WATCHDOG_THRESHOLD_S,
        history_capacity: int = 256,
        strict: bool = False,
    ) -> None:
        """Construct the machine bound to its initial state (IDLE).

        Args:
            watchdog_threshold_s: Per-state dwell ceiling for
                :meth:`is_watchdog_expired`. Must be in
                ``[0.5, 600]`` — loud-fail per anti-pattern #11.
            history_capacity: Newest-N transition records retained
                for :meth:`history`. ``>= 1`` (loud-fail).
            strict: When True, an invalid transition raises
                :class:`InvalidTransitionError` instead of logging
                a WARN. Default False so adoption is safe; flip
                only after the orchestrator's transition sites are
                fully migrated.
        """
        if not (
            _MIN_WATCHDOG_THRESHOLD_S <= watchdog_threshold_s <= _MAX_WATCHDOG_THRESHOLD_S
        ):
            msg = (
                f"watchdog_threshold_s must be in "
                f"[{_MIN_WATCHDOG_THRESHOLD_S}, {_MAX_WATCHDOG_THRESHOLD_S}], "
                f"got {watchdog_threshold_s}"
            )
            raise ValueError(msg)
        if history_capacity < 1:
            msg = f"history_capacity must be >= 1, got {history_capacity}"
            raise ValueError(msg)
        self._watchdog_threshold_s = watchdog_threshold_s
        self._history_capacity = history_capacity
        self._strict = strict
        self._lock = threading.Lock()
        self._monotonic = time.monotonic
        self._state = _MachineState(
            current=VoicePipelineState.IDLE,
            entered_monotonic=self._monotonic(),
        )

    # ── Read-only state surface ────────────────────────────────

    @property
    def current_state(self) -> VoicePipelineState:
        with self._lock:
            return self._state.current

    @property
    def transition_count(self) -> int:
        with self._lock:
            return self._state.transition_count

    @property
    def invalid_transition_count(self) -> int:
        with self._lock:
            return self._state.invalid_transition_count

    @property
    def watchdog_threshold_s(self) -> float:
        return self._watchdog_threshold_s

    @property
    def history_capacity(self) -> int:
        return self._history_capacity

    @property
    def strict(self) -> bool:
        return self._strict

    def time_in_current_state_s(self) -> float:
        """Wall-clock seconds since entering the current state.

        Uses ``time.monotonic()`` so it is immune to wall-clock
        adjustments. Note CLAUDE.md anti-pattern #22: on Windows
        the tick is ~15.6 ms, so dwell readings under that
        threshold round to 0.
        """
        with self._lock:
            return self._monotonic() - self._state.entered_monotonic

    def is_watchdog_expired(self) -> bool:
        """Has the current state outlived its dwell ceiling?

        Inclusive boundary (``>=``) per anti-pattern #24 so coarse
        clocks do not silently miss the deadline.
        """
        return self.time_in_current_state_s() >= self._watchdog_threshold_s

    def history(self) -> list[TransitionRecord]:
        """Snapshot of the bounded transition history (oldest first)."""
        with self._lock:
            return list(self._state.history)

    # ── Mutation surface ───────────────────────────────────────

    def record_transition(
        self,
        from_state: VoicePipelineState,
        to_state: VoicePipelineState,
    ) -> TransitionRecord:
        """Observe a state transition the caller has just performed.

        Validates against the canonical table; logs/raises on
        invalid (per :attr:`strict`). Always updates the dwell
        clock + history regardless of validity (a state we don't
        recognise is still a real state we entered).

        Args:
            from_state: State the caller just left.
            to_state: State the caller just entered.

        Returns:
            The :class:`TransitionRecord` appended to history.

        Raises:
            InvalidTransitionError: Only when :attr:`strict` is
                True and the transition is not in
                :data:`_CANONICAL_TRANSITIONS`.
        """
        valid = is_transition_allowed(from_state, to_state)
        now = self._monotonic()
        record = TransitionRecord(
            from_state=from_state,
            to_state=to_state,
            monotonic_at=now,
            valid=valid,
        )
        with self._lock:
            self._state.current = to_state
            self._state.entered_monotonic = now
            self._state.transition_count += 1
            self._state.history.append(record)
            if len(self._state.history) > self._history_capacity:
                # Drop oldest — bounded buffer (anti-pattern #15
                # equivalent for bounded telemetry buffers).
                self._state.history = self._state.history[-self._history_capacity :]
            if not valid:
                self._state.invalid_transition_count += 1

        if not valid:
            allowed = _CANONICAL_TRANSITIONS[from_state]
            if self._strict:
                raise InvalidTransitionError(
                    from_state=from_state,
                    to_state=to_state,
                    allowed=allowed,
                )
            logger.warning(
                "pipeline.state.invalid_transition",
                from_state=from_state.name,
                to_state=to_state.name,
                allowed=sorted(s.name for s in allowed),
                action_required=(
                    "either fix the offending transition site or extend "
                    "_CANONICAL_TRANSITIONS to legitimise this edge case"
                ),
            )
        else:
            logger.debug(
                "pipeline.state.transition",
                from_state=from_state.name,
                to_state=to_state.name,
            )
        return record

    def reset(self) -> None:
        """Force back to IDLE and clear history. Test-only helper.

        Production code should not need this — IDLE is reachable
        via the canonical table from every state. Provided so
        tests can establish a known-clean baseline without
        constructing a fresh machine.
        """
        with self._lock:
            self._state = _MachineState(
                current=VoicePipelineState.IDLE,
                entered_monotonic=self._monotonic(),
            )

    def fire_watchdog(
        self,
        *,
        recover_to: VoicePipelineState = VoicePipelineState.IDLE,
    ) -> TransitionRecord:
        """Forcibly transition to ``recover_to`` (default IDLE).

        Called by the orchestrator's heartbeat when
        :meth:`is_watchdog_expired` returns True. Emits a
        ``pipeline.state.watchdog_fired`` event with the dwell
        duration so dashboards can attribute the recovery to the
        pipeline-level watchdog vs other recovery paths.
        """
        with self._lock:
            from_state = self._state.current
            dwell_s = self._monotonic() - self._state.entered_monotonic
        logger.warning(
            "pipeline.state.watchdog_fired",
            from_state=from_state.name,
            recover_to=recover_to.name,
            dwell_s=round(dwell_s, 3),
            threshold_s=self._watchdog_threshold_s,
        )
        return self.record_transition(from_state, recover_to)


__all__ = [
    "InvalidTransitionError",
    "PipelineStateMachine",
    "TransitionRecord",
    "is_transition_allowed",
]
