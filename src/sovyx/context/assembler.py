"""Sovyx context assembler — orchestrate context assembly for LLM.

SPE-006: "The quality of the context directly determines the quality
of the response." 6 slots, adaptive budget, Lost-in-Middle ordering.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.context.budget import TokenBudgetManager
    from sovyx.context.formatter import ContextFormatter
    from sovyx.context.tokenizer import TokenCounter
    from sovyx.engine.types import MindId
    from sovyx.mind.config import MindConfig
    from sovyx.mind.personality import PersonalityEngine

logger = get_logger(__name__)


@dataclasses.dataclass
class AssembledContext:
    """Context assembled and ready for LLM."""

    messages: list[dict[str, str]]
    tokens_used: int
    token_budget: int
    sources: list[str]
    budget_breakdown: dict[str, int]


class ContextAssembler:
    """Assemble complete context with 6 slots (SPE-006).

    Slots (in order):
        1. SYSTEM PROMPT: personality + rules — NEVER cut
        2. TEMPORAL: date/time — NEVER cut (tiny)
        3. MEMORY (concepts): relevant concepts — cuttable
        4. MEMORY (episodes): recent episodes — cuttable
        5. CONVERSATION: message history — cuttable (oldest first)
        6. CURRENT MESSAGE: user input — NEVER cut
    """

    def __init__(
        self,
        token_counter: TokenCounter,
        personality_engine: PersonalityEngine,
        brain_service: BrainService,
        budget_manager: TokenBudgetManager,
        formatter: ContextFormatter,
        mind_config: MindConfig,
    ) -> None:
        self._counter = token_counter
        self._personality = personality_engine
        self._brain = brain_service
        self._budget = budget_manager
        self._formatter = formatter
        self._mind_config = mind_config

    async def assemble(
        self,
        mind_id: MindId,
        current_message: str,
        conversation_history: list[dict[str, str]],
        person_name: str | None = None,
        complexity: float = 0.5,
        context_window: int = 128_000,
    ) -> AssembledContext:
        """Assemble complete context for LLM.

        Args:
            mind_id: Mind to recall from.
            current_message: Current user message.
            conversation_history: Previous messages (NOT mutated).
            person_name: User's name (appended to system prompt).
            complexity: Query complexity [0, 1].
            context_window: Model context window size.

        Returns:
            AssembledContext ready for LLM call.
        """
        # 1. Budget allocation
        brain_results = await self._brain.recall(current_message, mind_id)
        concepts, episodes = brain_results

        budget = self._budget.allocate(
            conversation_length=len(conversation_history),
            brain_result_count=len(concepts),
            complexity=complexity,
            context_window=context_window,
        )

        # 2. System prompt (NEVER cut)
        system_prompt = self._personality.generate_system_prompt()
        if person_name:
            system_prompt += f"\n\nYou are currently talking to {person_name}."

        # 3. Temporal context (NEVER cut)
        temporal = self._formatter.format_temporal(self._mind_config.timezone)

        # 4. Memory blocks (cuttable)
        concepts_block = self._formatter.format_concepts_block(concepts, budget.memory_concepts)
        episodes_block = self._formatter.format_episodes_block(episodes, budget.memory_episodes)

        # 5. Build system content
        system_parts = [system_prompt, temporal]
        if concepts_block:
            system_parts.append(concepts_block)
        if episodes_block:
            system_parts.append(episodes_block)
        system_content = "\n\n".join(system_parts)

        # 6. Trim conversation history (NEVER mutate original — v12 fix)
        trimmed = self._trim_history(conversation_history, budget.conversation)

        # 7. Build messages
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        messages.extend(trimmed)
        messages.append({"role": "user", "content": current_message})

        # 8. Count tokens
        tokens_used = self._counter.count_messages(messages)

        # 9. Overflow check — trim more if needed
        max_usable = budget.total - budget.response_reserve
        if tokens_used > max_usable and len(trimmed) > 0:
            # Remove oldest messages until we fit
            while tokens_used > max_usable and trimmed:
                trimmed = trimmed[1:]
                messages = [{"role": "system", "content": system_content}]
                messages.extend(trimmed)
                messages.append({"role": "user", "content": current_message})
                tokens_used = self._counter.count_messages(messages)

        # Build sources list
        sources: list[str] = ["personality"]
        if concepts_block:
            sources.append(f"concepts({len(concepts)})")
        if episodes_block:
            sources.append(f"episodes({len(episodes)})")
        sources.append(f"history({len(trimmed)})")

        breakdown = {
            "system_prompt": budget.system_prompt,
            "memory_concepts": budget.memory_concepts,
            "memory_episodes": budget.memory_episodes,
            "temporal": budget.temporal,
            "conversation": budget.conversation,
            "response_reserve": budget.response_reserve,
        }

        logger.debug(
            "context_assembled",
            tokens_used=tokens_used,
            budget_total=budget.total,
            messages=len(messages),
            sources=sources,
        )

        return AssembledContext(
            messages=messages,
            tokens_used=tokens_used,
            token_budget=budget.total,
            sources=sources,
            budget_breakdown=breakdown,
        )

    def _trim_history(
        self,
        history: list[dict[str, str]],
        budget_tokens: int,
    ) -> list[dict[str, str]]:
        """Trim conversation history to fit budget.

        Removes oldest messages first. NEVER mutates original list.

        Args:
            history: Original conversation history.
            budget_tokens: Token budget for conversation.

        Returns:
            New list with trimmed history.
        """
        if not history:
            return []

        # Start from most recent, add backwards
        result: list[dict[str, str]] = []
        used = 0
        for msg in reversed(history):
            msg_tokens = self._counter.count_messages([msg])
            if used + msg_tokens > budget_tokens:
                break
            result.append(msg)
            used += msg_tokens

        result.reverse()
        return result
