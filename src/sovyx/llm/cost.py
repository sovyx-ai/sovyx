"""Sovyx CostGuard — LLM spending control with persistence.

Hot path is in-memory (dict lookups). Persistence via engine_state
table ensures spend survives process restarts.

Tracks costs per-day, per-conversation, per-provider, and per-mind
for fine-grained billing dashboards (SPE-026 §7).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

_STATE_KEY = "cost_guard_state"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Cost breakdown for a given period.

    Attributes:
        total_cost: Total USD spent.
        total_tokens: Total tokens consumed (input + output).
        by_provider: Cost per provider (e.g. ``{"anthropic": 1.5}``).
        by_mind: Cost per mind ID (e.g. ``{"default": 0.8}``).
        by_model: Cost per model (e.g. ``{"claude-3-opus": 1.2}``).
        tokens_by_provider: Token counts per provider.
        tokens_by_mind: Token counts per mind ID.
    """

    total_cost: float = 0.0
    total_tokens: int = 0
    by_provider: dict[str, float] = field(default_factory=dict)
    by_mind: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    tokens_by_provider: dict[str, int] = field(default_factory=dict)
    tokens_by_mind: dict[str, int] = field(default_factory=dict)


class CostGuard:
    """Control LLM spending with daily and per-conversation budgets.

    Tracks spending per day (resets at midnight UTC) and per conversation.
    Additionally tracks per-provider, per-mind, and per-model breakdowns
    for cost analytics dashboards.

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

        # Per-provider/mind/model breakdowns (reset daily with _maybe_reset)
        self._provider_spend: dict[str, float] = defaultdict(float)
        self._mind_spend: dict[str, float] = defaultdict(float)
        self._model_spend: dict[str, float] = defaultdict(float)
        self._provider_tokens: dict[str, int] = defaultdict(int)
        self._mind_tokens: dict[str, int] = defaultdict(int)
        self._total_tokens: int = 0

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
                # Restore breakdown data
                self._provider_spend = defaultdict(
                    float, state.get("provider_spend", {}),
                )
                self._mind_spend = defaultdict(
                    float, state.get("mind_spend", {}),
                )
                self._model_spend = defaultdict(
                    float, state.get("model_spend", {}),
                )
                self._provider_tokens = defaultdict(
                    int,
                    {k: int(v) for k, v in state.get("provider_tokens", {}).items()},
                )
                self._mind_tokens = defaultdict(
                    int,
                    {k: int(v) for k, v in state.get("mind_tokens", {}).items()},
                )
                self._total_tokens = int(state.get("total_tokens", 0))
                logger.info(
                    "cost_guard_restored",
                    daily_spend=round(self._daily_spend, 4),
                    conversations=len(self._conversation_spend),
                    providers=len(self._provider_spend),
                    minds=len(self._mind_spend),
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

        state = json.dumps(
            {
                "date": str(self._last_reset),
                "daily_spend": round(self._daily_spend, 8),
                "conversation_spend": {
                    k: round(v, 8) for k, v in self._conversation_spend.items()
                },
                "provider_spend": {
                    k: round(v, 8) for k, v in self._provider_spend.items()
                },
                "mind_spend": {
                    k: round(v, 8) for k, v in self._mind_spend.items()
                },
                "model_spend": {
                    k: round(v, 8) for k, v in self._model_spend.items()
                },
                "provider_tokens": dict(self._provider_tokens),
                "mind_tokens": dict(self._mind_tokens),
                "total_tokens": self._total_tokens,
            }
        )

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
            self._provider_spend.clear()
            self._mind_spend.clear()
            self._model_spend.clear()
            self._provider_tokens.clear()
            self._mind_tokens.clear()
            self._total_tokens = 0
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
        *,
        provider: str = "",
        mind_id: str = "",
        tokens: int = 0,
    ) -> None:
        """Record a spend and persist to SQLite.

        Args:
            cost: Actual cost in USD.
            model: Model used.
            conversation_id: Conversation ID.
            provider: LLM provider name (e.g. ``"anthropic"``, ``"openai"``).
            mind_id: Mind instance ID for multi-mind tracking.
            tokens: Total tokens consumed (input + output).
        """
        self._maybe_reset()
        self._daily_spend += cost
        if conversation_id:
            self._conversation_spend[conversation_id] = (
                self._conversation_spend.get(conversation_id, 0.0) + cost
            )
        # Track per-provider/mind/model breakdowns
        if provider:
            self._provider_spend[provider] += cost
            self._provider_tokens[provider] += tokens
        if mind_id:
            self._mind_spend[mind_id] += cost
            self._mind_tokens[mind_id] += tokens
        if model:
            self._model_spend[model] += cost
        self._total_tokens += tokens
        self._dirty = True
        logger.debug(
            "cost_recorded",
            cost=round(cost, 6),
            model=model,
            provider=provider or "unknown",
            mind_id=mind_id or "default",
            tokens=tokens,
            daily_total=round(self._daily_spend, 4),
        )
        await self.persist()

    async def record_cost(
        self,
        provider: str,
        mind_id: str,
        tokens: int,
        cost: float,
        *,
        model: str = "",
        conversation_id: str = "",
    ) -> None:
        """Record cost with per-provider and per-mind breakdown.

        Convenience wrapper around :meth:`record` with explicit
        provider/mind parameters.

        Args:
            provider: LLM provider name (e.g. ``"anthropic"``).
            mind_id: Mind instance ID.
            tokens: Total tokens consumed.
            cost: Actual cost in USD.
            model: Model name (optional).
            conversation_id: Conversation ID (optional).
        """
        await self.record(
            cost=cost,
            model=model,
            conversation_id=conversation_id,
            provider=provider,
            mind_id=mind_id,
            tokens=tokens,
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

    def get_breakdown(self, period: str = "day") -> CostBreakdown:
        """Get cost breakdown for a period.

        Currently supports ``"day"`` (today's spend). Future periods
        (``"week"``, ``"month"``) will require historical persistence.

        Args:
            period: Time period — ``"day"`` supported.

        Returns:
            :class:`CostBreakdown` with per-provider, per-mind, and
            per-model cost data.

        Raises:
            ValueError: If *period* is not supported.
        """
        if period != "day":
            msg = f"Unsupported period: {period!r}. Currently only 'day' is supported."
            raise ValueError(msg)

        self._maybe_reset()
        return CostBreakdown(
            total_cost=self._daily_spend,
            total_tokens=self._total_tokens,
            by_provider=dict(self._provider_spend),
            by_mind=dict(self._mind_spend),
            by_model=dict(self._model_spend),
            tokens_by_provider=dict(self._provider_tokens),
            tokens_by_mind=dict(self._mind_tokens),
        )

    def get_provider_spend(self, provider: str) -> float:
        """Get total daily spend for a specific provider."""
        self._maybe_reset()
        return self._provider_spend.get(provider, 0.0)

    def get_mind_spend(self, mind_id: str) -> float:
        """Get total daily spend for a specific mind."""
        self._maybe_reset()
        return self._mind_spend.get(mind_id, 0.0)

    def get_model_spend(self, model: str) -> float:
        """Get total daily spend for a specific model."""
        self._maybe_reset()
        return self._model_spend.get(model, 0.0)
