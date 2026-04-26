"""Tests for :mod:`sovyx.voice.pipeline._state_machine`.

Covers O1's three responsibilities:

* ``is_transition_allowed`` — pure-function membership check
  against the canonical table.
* :class:`PipelineStateMachine.record_transition` — validates +
  updates dwell clock + history (WARN by default, raise in strict).
* Per-state dwell + watchdog firing.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.10
O1.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import (
    _CANONICAL_TRANSITIONS,
    InvalidTransitionError,
    PipelineStateMachine,
    TransitionRecord,
    is_transition_allowed,
)

# ── Canonical table integrity ───────────────────────────────────────


class TestCanonicalTable:
    def test_every_state_has_entry(self) -> None:
        """Sanity guard against a future enum addition that forgets
        the table."""
        assert set(_CANONICAL_TRANSITIONS.keys()) == set(VoicePipelineState)

    def test_idle_can_reach_wake_detected(self) -> None:
        assert is_transition_allowed(
            VoicePipelineState.IDLE,
            VoicePipelineState.WAKE_DETECTED,
        )

    def test_wake_detected_to_recording_allowed(self) -> None:
        assert is_transition_allowed(
            VoicePipelineState.WAKE_DETECTED,
            VoicePipelineState.RECORDING,
        )

    def test_speaking_to_recording_allowed_for_barge_in(self) -> None:
        """Mission §2.6 — barge-in skips wake."""
        assert is_transition_allowed(
            VoicePipelineState.SPEAKING,
            VoicePipelineState.RECORDING,
        )

    def test_idle_to_thinking_rejected(self) -> None:
        """Mission §2.6 — no IDLE → THINKING shortcut."""
        assert not is_transition_allowed(
            VoicePipelineState.IDLE,
            VoicePipelineState.THINKING,
        )

    def test_recording_to_speaking_rejected(self) -> None:
        """Skipping TRANSCRIBING + THINKING is illegal."""
        assert not is_transition_allowed(
            VoicePipelineState.RECORDING,
            VoicePipelineState.SPEAKING,
        )

    def test_every_state_can_reach_idle(self) -> None:
        """IDLE must be reachable from everywhere — recovery anchor."""
        for state in VoicePipelineState:
            allowed = _CANONICAL_TRANSITIONS[state]
            assert VoicePipelineState.IDLE in allowed, (
                f"{state.name} cannot reach IDLE — recovery would be impossible"
            )


# ── Constructor bound enforcement ──────────────────────────────────


class TestPipelineStateMachineInit:
    def test_default_state_is_idle(self) -> None:
        m = PipelineStateMachine()
        assert m.current_state is VoicePipelineState.IDLE

    def test_default_threshold_is_30_s(self) -> None:
        m = PipelineStateMachine()
        assert m.watchdog_threshold_s == 30.0

    def test_threshold_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="watchdog_threshold_s must be"):
            PipelineStateMachine(watchdog_threshold_s=0.1)

    def test_threshold_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="watchdog_threshold_s must be"):
            PipelineStateMachine(watchdog_threshold_s=601.0)

    def test_history_capacity_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="history_capacity"):
            PipelineStateMachine(history_capacity=0)

    def test_strict_default_off(self) -> None:
        m = PipelineStateMachine()
        assert m.strict is False


# ── record_transition — happy path ─────────────────────────────────


class TestRecordTransition:
    def test_valid_transition_updates_state(self) -> None:
        m = PipelineStateMachine()
        rec = m.record_transition(
            VoicePipelineState.IDLE,
            VoicePipelineState.WAKE_DETECTED,
        )
        assert rec.valid is True
        assert m.current_state is VoicePipelineState.WAKE_DETECTED
        assert m.transition_count == 1
        assert m.invalid_transition_count == 0

    def test_invalid_transition_logged_warn_in_lenient_mode(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        m = PipelineStateMachine(strict=False)
        with caplog.at_level(logging.WARNING):
            rec = m.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.THINKING,
            )
        assert rec.valid is False
        assert m.invalid_transition_count == 1
        assert any("pipeline.state.invalid_transition" in r.message for r in caplog.records)

    def test_invalid_transition_still_advances_state(self) -> None:
        """Even invalid transitions update current_state — the caller
        already moved, the machine just witnesses it."""
        m = PipelineStateMachine(strict=False)
        m.record_transition(
            VoicePipelineState.IDLE,
            VoicePipelineState.THINKING,
        )
        assert m.current_state is VoicePipelineState.THINKING

    def test_strict_mode_raises_on_invalid(self) -> None:
        m = PipelineStateMachine(strict=True)
        with pytest.raises(InvalidTransitionError) as exc_info:
            m.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.THINKING,
            )
        err = exc_info.value
        assert err.from_state is VoicePipelineState.IDLE
        assert err.to_state is VoicePipelineState.THINKING
        assert VoicePipelineState.WAKE_DETECTED in err.allowed

    def test_strict_mode_records_invalid_count_before_raising(self) -> None:
        m = PipelineStateMachine(strict=True)
        with pytest.raises(InvalidTransitionError):
            m.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.THINKING,
            )
        assert m.invalid_transition_count == 1

    def test_history_records_valid_and_invalid(self) -> None:
        m = PipelineStateMachine()
        m.record_transition(VoicePipelineState.IDLE, VoicePipelineState.WAKE_DETECTED)
        m.record_transition(
            VoicePipelineState.WAKE_DETECTED,
            VoicePipelineState.SPEAKING,
        )  # invalid
        history = m.history()
        assert len(history) == 2
        assert history[0].valid is True
        assert history[1].valid is False

    def test_history_bounded_to_capacity(self) -> None:
        m = PipelineStateMachine(history_capacity=3)
        for _ in range(10):
            m.record_transition(VoicePipelineState.IDLE, VoicePipelineState.IDLE)
        assert len(m.history()) == 3


# ── Watchdog ───────────────────────────────────────────────────────


class TestWatchdog:
    def test_fresh_machine_not_expired(self) -> None:
        m = PipelineStateMachine(watchdog_threshold_s=10.0)
        assert m.is_watchdog_expired() is False

    def test_expired_when_dwell_exceeds_threshold(self) -> None:
        m = PipelineStateMachine(watchdog_threshold_s=1.0)
        # Inject fake clock — anti-pattern #22 (avoid Windows tick drift).
        fake = [0.0]
        m._monotonic = lambda: fake[0]  # type: ignore[method-assign]
        # Re-anchor entered_monotonic to t=0 (before clock injection it
        # was set to time.monotonic()).
        m.reset()
        fake[0] = 5.0
        assert m.is_watchdog_expired() is True

    def test_inclusive_boundary(self) -> None:
        """``>=`` per anti-pattern #24 — exactly threshold-s dwell counts."""
        m = PipelineStateMachine(watchdog_threshold_s=2.0)
        fake = [0.0]
        m._monotonic = lambda: fake[0]  # type: ignore[method-assign]
        m.reset()
        fake[0] = 2.0
        assert m.is_watchdog_expired() is True

    def test_time_in_current_state_advances(self) -> None:
        m = PipelineStateMachine()
        fake = [0.0]
        m._monotonic = lambda: fake[0]  # type: ignore[method-assign]
        m.reset()
        fake[0] = 0.5
        assert m.time_in_current_state_s() == 0.5

    def test_transition_resets_dwell(self) -> None:
        m = PipelineStateMachine()
        fake = [0.0]
        m._monotonic = lambda: fake[0]  # type: ignore[method-assign]
        m.reset()
        fake[0] = 5.0
        assert m.time_in_current_state_s() == 5.0
        m.record_transition(
            VoicePipelineState.IDLE,
            VoicePipelineState.WAKE_DETECTED,
        )
        assert m.time_in_current_state_s() == 0.0

    def test_fire_watchdog_recovers_to_idle(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        m = PipelineStateMachine()
        m.record_transition(VoicePipelineState.IDLE, VoicePipelineState.WAKE_DETECTED)
        m.record_transition(
            VoicePipelineState.WAKE_DETECTED,
            VoicePipelineState.RECORDING,
        )
        with caplog.at_level(logging.WARNING):
            rec = m.fire_watchdog()
        assert rec.valid is True
        assert m.current_state is VoicePipelineState.IDLE
        assert any("pipeline.state.watchdog_fired" in r.message for r in caplog.records)

    def test_fire_watchdog_with_custom_recover_state(self) -> None:
        m = PipelineStateMachine()
        m.record_transition(VoicePipelineState.IDLE, VoicePipelineState.RECORDING)
        # RECORDING → TRANSCRIBING is allowed; tests that the operator
        # can pick any legal recovery target.
        rec = m.fire_watchdog(recover_to=VoicePipelineState.TRANSCRIBING)
        assert rec.valid is True
        assert m.current_state is VoicePipelineState.TRANSCRIBING


# ── reset ──────────────────────────────────────────────────────────


class TestReset:
    def test_clears_history(self) -> None:
        m = PipelineStateMachine()
        m.record_transition(VoicePipelineState.IDLE, VoicePipelineState.WAKE_DETECTED)
        m.reset()
        assert m.history() == []
        assert m.current_state is VoicePipelineState.IDLE
        assert m.transition_count == 0


# ── TransitionRecord shape ─────────────────────────────────────────


class TestTransitionRecord:
    def test_frozen_dataclass(self) -> None:
        rec = TransitionRecord(
            from_state=VoicePipelineState.IDLE,
            to_state=VoicePipelineState.WAKE_DETECTED,
            monotonic_at=1.0,
            valid=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            rec.valid = False  # type: ignore[misc]


# ── Thread-safety smoke ────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_record_does_not_corrupt_count(self) -> None:
        import threading

        m = PipelineStateMachine()
        n_threads = 8
        per_thread = 100
        barrier = threading.Barrier(n_threads)
        errors: list[Any] = []

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(per_thread):
                    m.record_transition(
                        VoicePipelineState.IDLE,
                        VoicePipelineState.IDLE,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert m.transition_count == n_threads * per_thread


# ── Step 12: frame history ──────────────────────────────────────────


class TestFrameHistory:
    """Pin the bounded frame ring buffer added in Step 12."""

    def test_record_frame_appends_to_history(self) -> None:
        from sovyx.voice.pipeline._frame_types import EndFrame

        m = PipelineStateMachine()
        frame = EndFrame(
            frame_type="End",
            timestamp_monotonic=1.0,
            reason="reset",
        )
        m.record_frame(frame)
        history = m.frame_history()
        assert len(history) == 1
        assert history[0] is frame

    def test_frame_history_bounded_at_capacity(self) -> None:
        from sovyx.voice.pipeline._frame_types import EndFrame

        m = PipelineStateMachine(history_capacity=3)
        for i in range(10):
            m.record_frame(
                EndFrame(
                    frame_type="End",
                    timestamp_monotonic=float(i),
                    reason=f"reset-{i}",
                ),
            )
        history = m.frame_history()
        # Bounded at 3; oldest dropped, newest 3 preserved.
        assert len(history) == 3
        # The deque keeps newest at the right; the snapshot tuple
        # returns the deque iteration order (oldest-first).
        assert history[0].timestamp_monotonic == 7.0
        assert history[2].timestamp_monotonic == 9.0

    def test_frame_history_returns_immutable_tuple(self) -> None:
        """Caller mutations must not leak back into the deque."""
        from sovyx.voice.pipeline._frame_types import EndFrame

        m = PipelineStateMachine()
        m.record_frame(
            EndFrame(
                frame_type="End",
                timestamp_monotonic=1.0,
                reason="reset",
            ),
        )
        history = m.frame_history()
        assert isinstance(history, tuple)
        # Tuple is immutable so the assignment below would itself
        # raise — but the contract is that the snapshot is decoupled
        # from the deque. Add a fresh frame, re-snapshot, verify the
        # earlier snapshot still has length 1.
        m.record_frame(
            EndFrame(
                frame_type="End",
                timestamp_monotonic=2.0,
                reason="reset-2",
            ),
        )
        assert len(history) == 1
        assert len(m.frame_history()) == 2

    def test_reset_clears_frame_history(self) -> None:
        from sovyx.voice.pipeline._frame_types import EndFrame

        m = PipelineStateMachine()
        m.record_frame(
            EndFrame(
                frame_type="End",
                timestamp_monotonic=1.0,
                reason="reset",
            ),
        )
        assert len(m.frame_history()) == 1
        m.reset()
        assert m.frame_history() == ()

    def test_record_frame_thread_safe(self) -> None:
        import threading

        from sovyx.voice.pipeline._frame_types import EndFrame

        m = PipelineStateMachine(history_capacity=10_000)
        n_threads = 8
        per_thread = 50
        barrier = threading.Barrier(n_threads)

        def worker(thread_id: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                m.record_frame(
                    EndFrame(
                        frame_type="End",
                        timestamp_monotonic=float(thread_id * 1000 + i),
                        reason=f"t{thread_id}-{i}",
                    ),
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        history = m.frame_history()
        assert len(history) == n_threads * per_thread
