"""Sovyx token budget manager — adaptive allocation across context slots.

SPE-006 §3: Allocate tokens between system prompt, memory, conversation,
temporal context, and response reserve with adaptive proportions.
"""

from __future__ import annotations

import dataclasses

from sovyx.engine.errors import SovyxError
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Minimum allocations (non-negotiable)
MIN_SYSTEM_PROMPT = 200
MIN_CONVERSATION = 500
MIN_RESPONSE = 256
MIN_TEMPORAL = 50
MIN_CONTEXT_WINDOW = 2048


class TokenBudgetError(SovyxError):
    """Token budget allocation error."""


@dataclasses.dataclass(frozen=True)
class TokenBudget:
    """Token allocation per slot."""

    system_prompt: int
    memory_concepts: int
    memory_episodes: int
    temporal: int
    conversation: int
    response_reserve: int
    total: int


class TokenBudgetManager:
    """Allocate tokens adaptively between context slots.

    Default v0.1 proportions (sum to 100%):
        system_prompt: 15%, memory_concepts: 20%, memory_episodes: 13%,
        temporal: 2%, conversation: 37%, response_reserve: 13%

    Adaptation rules:
        - Long conversation (>15 turns): +8% conversation, -5% concepts, -3% episodes
        - Short conversation (<3 turns): -10% conversation, +6% concepts, +4% episodes
        - Complex query (>0.7): +3% response_reserve
        - Many brain results (>20): +5% concepts, -5% conversation
    """

    def allocate(
        self,
        conversation_length: int,
        brain_result_count: int,
        complexity: float = 0.5,
        context_window: int = 128_000,
    ) -> TokenBudget:
        """Calculate adaptive budget.

        Args:
            conversation_length: Number of turns in conversation.
            brain_result_count: Number of brain search results.
            complexity: Query complexity [0, 1].
            context_window: Model context window size.

        Returns:
            TokenBudget with allocations per slot.

        Raises:
            TokenBudgetError: If context_window too small.
        """
        if context_window < MIN_CONTEXT_WINDOW:
            msg = f"context_window={context_window} too small. Minimum: {MIN_CONTEXT_WINDOW}"
            raise TokenBudgetError(msg)

        # Base proportions
        p_system = 0.15
        p_concepts = 0.20
        p_episodes = 0.13
        p_temporal = 0.02
        p_conversation = 0.37
        p_response = 0.13

        # Adapt: long conversation
        if conversation_length > 15:  # noqa: PLR2004
            p_conversation += 0.08
            p_concepts -= 0.05
            p_episodes -= 0.03

        # Adapt: short conversation
        elif conversation_length < 3:  # noqa: PLR2004
            p_conversation -= 0.10
            p_concepts += 0.06
            p_episodes += 0.04

        # Adapt: complex query
        if complexity > 0.7:  # noqa: PLR2004
            p_response += 0.03
            p_conversation -= 0.03

        # Adapt: many brain results
        if brain_result_count > 20:  # noqa: PLR2004
            p_concepts += 0.05
            p_conversation -= 0.05

        # Calculate raw allocations
        raw_system = max(MIN_SYSTEM_PROMPT, int(context_window * p_system))
        raw_temporal = max(MIN_TEMPORAL, int(context_window * p_temporal))
        raw_response = max(MIN_RESPONSE, int(context_window * p_response))
        raw_conversation = max(MIN_CONVERSATION, int(context_window * p_conversation))
        raw_concepts = max(0, int(context_window * p_concepts))
        raw_episodes = max(0, int(context_window * p_episodes))

        # Normalise: MIN floors can cause overflow on small windows.
        # Shrink flex slots (concepts, episodes, conversation) proportionally.
        total_allocated = (
            raw_system + raw_temporal + raw_response
            + raw_conversation + raw_concepts + raw_episodes
        )
        if total_allocated > context_window:
            overflow = total_allocated - context_window
            # Reduce flex slots (concepts → episodes → conversation)
            flex_slots = [
                ("concepts", raw_concepts),
                ("episodes", raw_episodes),
                ("conversation", raw_conversation - MIN_CONVERSATION),
            ]
            for _name, available in flex_slots:
                reduction = min(overflow, available)
                if _name == "concepts":
                    raw_concepts -= reduction
                elif _name == "episodes":
                    raw_episodes -= reduction
                else:
                    raw_conversation -= reduction
                overflow -= reduction
                if overflow <= 0:
                    break

        return TokenBudget(
            system_prompt=raw_system,
            memory_concepts=raw_concepts,
            memory_episodes=raw_episodes,
            temporal=raw_temporal,
            conversation=raw_conversation,
            response_reserve=raw_response,
            total=context_window,
        )
