"""Tests for sovyx.cognitive.state — Cognitive state machine."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.cognitive.state import VALID_TRANSITIONS, CognitiveStateMachine
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import CognitivePhase


class TestInitialState:
    """Initial state."""

    def test_starts_idle(self) -> None:
        sm = CognitiveStateMachine()
        assert sm.current == CognitivePhase.IDLE


class TestValidTransitions:
    """All valid transitions."""

    def test_idle_to_perceiving(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        assert sm.current == CognitivePhase.PERCEIVING

    def test_perceiving_to_attending(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        assert sm.current == CognitivePhase.ATTENDING

    def test_perceiving_to_idle(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.IDLE)
        assert sm.current == CognitivePhase.IDLE

    def test_attending_to_thinking(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        assert sm.current == CognitivePhase.THINKING

    def test_attending_to_idle(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.IDLE)
        assert sm.current == CognitivePhase.IDLE

    def test_thinking_to_acting(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.transition(CognitivePhase.ACTING)
        assert sm.current == CognitivePhase.ACTING

    def test_acting_to_reflecting(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.transition(CognitivePhase.ACTING)
        sm.transition(CognitivePhase.REFLECTING)
        assert sm.current == CognitivePhase.REFLECTING

    def test_reflecting_to_idle(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.transition(CognitivePhase.ACTING)
        sm.transition(CognitivePhase.REFLECTING)
        sm.transition(CognitivePhase.IDLE)
        assert sm.current == CognitivePhase.IDLE

    def test_full_cycle(self) -> None:
        """Complete OODA cycle."""
        sm = CognitiveStateMachine()
        for phase in [
            CognitivePhase.PERCEIVING,
            CognitivePhase.ATTENDING,
            CognitivePhase.THINKING,
            CognitivePhase.ACTING,
            CognitivePhase.REFLECTING,
            CognitivePhase.IDLE,
        ]:
            sm.transition(phase)
        assert sm.current == CognitivePhase.IDLE


class TestInvalidTransitions:
    """Invalid transitions raise CognitiveError."""

    def test_idle_to_thinking(self) -> None:
        sm = CognitiveStateMachine()
        with pytest.raises(CognitiveError, match="Invalid transition"):
            sm.transition(CognitivePhase.THINKING)

    def test_idle_to_acting(self) -> None:
        sm = CognitiveStateMachine()
        with pytest.raises(CognitiveError):
            sm.transition(CognitivePhase.ACTING)

    def test_thinking_to_idle(self) -> None:
        """THINKING → IDLE is invalid (must go through ACTING)."""
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        with pytest.raises(CognitiveError):
            sm.transition(CognitivePhase.IDLE)

    def test_acting_to_idle(self) -> None:
        """ACTING → IDLE is invalid (must go through REFLECTING)."""
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.transition(CognitivePhase.ACTING)
        with pytest.raises(CognitiveError):
            sm.transition(CognitivePhase.IDLE)

    def test_error_message_includes_valid_targets(self) -> None:
        sm = CognitiveStateMachine()
        with pytest.raises(CognitiveError, match="perceiving"):
            sm.transition(CognitivePhase.THINKING)


class TestReset:
    """reset() — unconditional return to IDLE."""

    def test_reset_from_idle(self) -> None:
        sm = CognitiveStateMachine()
        sm.reset()
        assert sm.current == CognitivePhase.IDLE

    def test_reset_from_thinking(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.reset()
        assert sm.current == CognitivePhase.IDLE

    def test_reset_from_acting(self) -> None:
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.transition(CognitivePhase.ACTING)
        sm.reset()
        assert sm.current == CognitivePhase.IDLE

    def test_reset_allows_new_cycle(self) -> None:
        """After reset, can start a new cycle."""
        sm = CognitiveStateMachine()
        sm.transition(CognitivePhase.PERCEIVING)
        sm.transition(CognitivePhase.ATTENDING)
        sm.transition(CognitivePhase.THINKING)
        sm.reset()
        # Should work — back to IDLE
        sm.transition(CognitivePhase.PERCEIVING)
        assert sm.current == CognitivePhase.PERCEIVING


class TestTransitionMap:
    """VALID_TRANSITIONS map completeness."""

    def test_all_phases_have_transitions(self) -> None:
        """Every phase in the map has at least one valid target."""
        for phase in VALID_TRANSITIONS:
            assert len(VALID_TRANSITIONS[phase]) > 0

    def test_reflecting_only_goes_to_idle(self) -> None:
        assert VALID_TRANSITIONS[CognitivePhase.REFLECTING] == {CognitivePhase.IDLE}


class TestPropertyBased:
    """Property-based tests."""

    @given(st.data())
    @settings(max_examples=30)
    def test_valid_random_walk(self, data: st.DataObject) -> None:
        """Random walk through valid transitions always succeeds."""
        sm = CognitiveStateMachine()
        for _ in range(20):
            valid = VALID_TRANSITIONS.get(sm.current, set())
            if not valid:
                break
            target = data.draw(st.sampled_from(sorted(valid, key=lambda x: x.value)))
            sm.transition(target)
        # Should never raise
        assert sm.current in CognitivePhase
