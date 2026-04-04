"""Sovyx CostGuard — LLM spending control with persistence.

Hot path is in-memory (dict lookups). Persistence via engine_state
table ensures spend survives process restarts.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

_STATE_KEY = "cost_guard_state"


class CostGuard:
    """Control LLM spending with daily and per-conversation budgets.

    Tracks spending per day (resets at midnight UTC) and per conversation.
    Optionally backed by SQLite engine_state table for crash recovery.
    """

    def __init__(
        self,
        daily_budget: float,
        per_conversation_budget: float,
        system_pool: DatabasePool | None = None,
    ) -> None:
        self._daily_budget = daily_budget
        self._per_conversation_budget = per_conversation_budget
        self._system_pool = system_pool
        self._daily_spend: float = 0.0
        self._conversation_spend: dict[str, float] = {}
        self._last_reset: date = datetime.now(tz=UTC).date()
        self._dirty = False

    async def restore(self) -> None:
        """Restore spend state from engine_state (call once at startup).

        If no state exists or pool is None, starts fresh.
        """
        if self._system_pool is None:
            return

        try:
            async with self._system_pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT value FROM engine_state WHERE key = ?",
                    (_STATE_KEY,),
                )
                row = await cursor.fetchone()

            if row is None:
                return

            state = json.loads(row[0])
            saved_date = state.get("date", "")

            # Only restore if same day (midnight reset still works)
            if saved_date == str(self._last_reset):
                self._daily_spend = state.get("daily_spend", 0.0)
                self._conversation_spend = state.get("conversation_spend", {})
                logger.info(
                    "cost_guard_restored",
                    daily_spend=round(self._daily_spend, 4),
                    conversations=len(self._conversation_spend),
                )
            else:
                logger.debug("cost_guard_state_stale_starting_fresh", saved_date=saved_date)
        except Exception:
            logger.warning("cost_guard_restore_failed", exc_info=True)

    async def persist(self) -> None:
        """Persist current spend state to engine_state.

        Called after each record() if pool is available.
        Skipped if nothing changed since last persist.
        """
        if self._system_pool is None or not self._dirty:
            return

        state = json.dumps({
            "date": str(self._last_reset),
            "daily_spend": round(self._daily_spend, 8),
            "conversation_spend": {
                k: round(v, 8) for k, v in self._conversation_spend.items()
            },
        })

        try:
            async with self._system_pool.write() as conn:
                await conn.execute(
                    """INSERT INTO engine_state (key, value, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                    (_STATE_KEY, state),
                )
                await conn.commit()
            self._dirty = False
        except Exception:
            logger.warning("cost_guard_persist_failed", exc_info=True)

    def _maybe_reset(self) -> None:
        """Reset daily spend if new day."""
        today = datetime.now(tz=UTC).date()
        if today > self._last_reset:
            self._daily_spend = 0.0
            self._conversation_spend.clear()
            self._last_reset = today
            self._dirty = True

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

    async def record(
        self,
        cost: float,
        model: str,
        conversation_id: str,
    ) -> None:
        """Record a spend and persist to SQLite.

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
        self._dirty = True
        logger.debug(
            "cost_recorded",
            cost=round(cost, 6),
            model=model,
            daily_total=round(self._daily_spend, 4),
        )
        await self.persist()

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
