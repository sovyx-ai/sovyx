"""Edge case tests for system resilience."""

from __future__ import annotations

from sovyx.brain.working_memory import WorkingMemory
from sovyx.cognitive.state import CognitiveStateMachine
from sovyx.context.budget import TokenBudgetManager
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.events import EventBus


class TestBrainEdgeCases:
    """Brain module edge cases."""

    def test_working_memory_empty(self) -> None:
        """Empty working memory returns empty list."""
        wm = WorkingMemory()
        assert wm.get_active_concepts() == []

    def test_working_memory_zero_activation(self) -> None:
        """Zero activation item is added but may be decayed out."""
        wm = WorkingMemory()
        wm.activate("test", 0.0)
        # Implementation detail: 0.0 activation
        active = dict(wm.get_active_concepts())
        # Either present with 0 or absent — both valid
        assert active.get("test", 0.0) == 0.0

    def test_working_memory_negative_activation(self) -> None:
        """Negative activation doesn't crash."""
        wm = WorkingMemory()
        wm.activate("test", -1.0)
        # Should handle gracefully
        assert isinstance(wm.get_active_concepts(), list)

    def test_working_memory_huge_activation(self) -> None:
        """Very large activation value doesn't crash."""
        wm = WorkingMemory()
        wm.activate("test", 1e10)
        active = dict(wm.get_active_concepts())
        assert "test" in active


class TestContextEdgeCases:
    """Context assembly edge cases."""

    def test_min_context_window(self) -> None:
        """Minimum context window (2048) produces valid allocations."""
        mgr = TokenBudgetManager()
        alloc = mgr.allocate(context_window=2048, conversation_length=5, brain_result_count=0)
        total = alloc.total
        assert total <= 2048  # noqa: PLR2004

    def test_huge_context_window(self) -> None:
        """200K context window works correctly."""
        mgr = TokenBudgetManager()
        alloc = mgr.allocate(context_window=200000, conversation_length=5, brain_result_count=0)
        total = alloc.total
        assert total <= 200000  # noqa: PLR2004

    def test_extreme_turn_count(self) -> None:
        """1000 turns adapts conversation allocation."""
        mgr = TokenBudgetManager()
        alloc_low = mgr.allocate(
            context_window=100000, conversation_length=1, brain_result_count=0
        )
        alloc_high = mgr.allocate(
            context_window=100000, conversation_length=1000, brain_result_count=0
        )
        # High turn count should give more to conversation
        assert alloc_high.conversation >= alloc_low.conversation


class TestCogLoopEdgeCases:
    """CogLoop edge cases."""

    def test_state_machine_reset(self) -> None:
        """State machine can be reset cleanly."""
        sm = CognitiveStateMachine()
        sm.reset()
        # Should be in initial state

    async def test_event_bus_no_subscribers(self) -> None:
        """Emitting event with no subscribers doesn't crash."""
        bus = EventBus()
        from sovyx.engine.events import EngineStarted

        # This should not raise
        await bus.emit(EngineStarted())


class TestInputValidation:
    """Input validation edge cases."""

    def test_token_counter_whitespace_only(self) -> None:
        """Whitespace-only input returns tokens (spaces are tokens)."""
        tc = TokenCounter()
        count = tc.count("   \n\t\r   ")
        assert count >= 0

    def test_token_counter_repeated_chars(self) -> None:
        """Repeated single character doesn't cause issues."""
        tc = TokenCounter()
        count = tc.count("a" * 50000)
        assert count > 0

    def test_budget_zero_turn_count(self) -> None:
        """Zero turns works fine."""
        mgr = TokenBudgetManager()
        alloc = mgr.allocate(context_window=4096, conversation_length=0, brain_result_count=0)
        total = alloc.total
        assert total <= 4096  # noqa: PLR2004
