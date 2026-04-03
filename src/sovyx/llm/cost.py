"""Sovyx CostGuard — LLM spending control."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class CostGuard:
    """Control LLM spending with daily and per-conversation budgets.

    Tracks spending per day (resets at midnight UTC) and per conversation.
    """

    def __init__(
        self,
        daily_budget: float,
        per_conversation_budget: float,
    ) -> None:
        self._daily_budget = daily_budget
        self._per_conversation_budget = per_conversation_budget
        self._daily_spend: float = 0.0
        self._conversation_spend: dict[str, float] = {}
        self._last_reset: date = datetime.now(tz=UTC).date()

    def _maybe_reset(self) -> None:
        """Reset daily spend if new day."""
        today = datetime.now(tz=UTC).date()
        if today > self._last_reset:
            self._daily_spend = 0.0
            self._conversation_spend.clear()
            self._last_reset = today

    def can_afford(
        self,
        estimated_cost: float,
        conversation_id: str = "",
    ) -> bool:
        """Check if both daily and per-conversation budgets allow the spend.

        Args:
            estimated_cost: Estimated cost of the call.
            conversation_id: Conversation to check (empty = skip conv check).

        Returns:
            True if budget allows.
        """
        self._maybe_reset()

        if self._daily_spend + estimated_cost > self._daily_budget:
            return False

        if conversation_id:
            conv_spend = self._conversation_spend.get(conversation_id, 0.0)
            if conv_spend + estimated_cost > self._per_conversation_budget:
                return False

        return True

    def record(
        self,
        cost: float,
        model: str,
        conversation_id: str,
    ) -> None:
        """Record a spend.

        Args:
            cost: Actual cost in USD.
            model: Model used.
            conversation_id: Conversation ID.
        """
        self._maybe_reset()
        self._daily_spend += cost
        if conversation_id:
            self._conversation_spend[conversation_id] = (
                self._conversation_spend.get(conversation_id, 0.0) + cost
            )
        logger.debug(
            "cost_recorded",
            cost=round(cost, 6),
            model=model,
            daily_total=round(self._daily_spend, 4),
        )

    def get_daily_spend(self) -> float:
        """Get total daily spend."""
        self._maybe_reset()
        return self._daily_spend

    def get_remaining_budget(self) -> float:
        """Get remaining daily budget."""
        self._maybe_reset()
        return max(0.0, self._daily_budget - self._daily_spend)

    def get_conversation_spend(self, conversation_id: str) -> float:
        """Get total spend for a conversation."""
        return self._conversation_spend.get(conversation_id, 0.0)

    def get_conversation_remaining(self, conversation_id: str) -> float:
        """Get remaining budget for a conversation."""
        spent = self.get_conversation_spend(conversation_id)
        return max(0.0, self._per_conversation_budget - spent)
