"""Daily usage statistics recorder.

Persists one row per day per mind to ``daily_stats`` (system.db).
Populated at day boundary by CostGuard + DashboardCounters, queried
by ``GET /api/stats/history``.

Design principles:
- **One INSERT per day** at midnight (zero runtime overhead).
- **Upsert-safe**: calling ``snapshot_day`` twice for the same date
  overwrites (idempotent).
- **Read-optimised**: queries use the ``idx_daily_stats_date`` index.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class DailyStatsRecorder:
    """Persist and query daily usage snapshots.

    Registered in :class:`ServiceRegistry` during bootstrap.
    """

    def __init__(self, system_pool: DatabasePool) -> None:
        self._pool = system_pool

    async def snapshot_day(
        self,
        *,
        date: str,
        mind_id: str = "aria",
        messages: int = 0,
        llm_calls: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
        cost_by_provider: dict[str, float] | None = None,
        cost_by_model: dict[str, float] | None = None,
        conversations: int = 0,
    ) -> None:
        """Persist a single day's usage statistics.

        Uses ``INSERT OR REPLACE`` — safe to call multiple times for
        the same ``(date, mind_id)`` pair.

        Args:
            date: ISO-8601 date string (``YYYY-MM-DD``).
            mind_id: Mind instance identifier.
            messages: Total messages (inbound + outbound).
            llm_calls: Total LLM API calls.
            tokens: Total tokens consumed.
            cost_usd: Total cost in USD.
            cost_by_provider: Per-provider cost breakdown (JSON-serialised).
            cost_by_model: Per-model cost breakdown (JSON-serialised).
            conversations: Number of active conversations.
        """
        provider_json = json.dumps(
            {k: round(v, 8) for k, v in (cost_by_provider or {}).items()},
        )
        model_json = json.dumps(
            {k: round(v, 8) for k, v in (cost_by_model or {}).items()},
        )

        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """INSERT OR REPLACE INTO daily_stats
                       (date, mind_id, messages, llm_calls, tokens,
                        cost_usd, cost_by_provider, cost_by_model, conversations)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        date,
                        mind_id,
                        messages,
                        llm_calls,
                        tokens,
                        round(cost_usd, 8),
                        provider_json,
                        model_json,
                        conversations,
                    ),
                )
                await conn.commit()
            logger.info(
                "daily_stats_snapshot",
                date=date,
                mind_id=mind_id,
                cost=round(cost_usd, 4),
                messages=messages,
                llm_calls=llm_calls,
            )
        except Exception:
            logger.warning("daily_stats_snapshot_failed", date=date, exc_info=True)

    async def get_history(
        self,
        days: int = 30,
        mind_id: str = "aria",
    ) -> list[dict[str, Any]]:
        """Return the last *days* of usage stats, ordered by date.

        Args:
            days: Maximum number of days to return (capped at 365).
            mind_id: Mind instance to query.

        Returns:
            List of dicts, each with ``date``, ``cost``, ``messages``,
            ``llm_calls``, ``tokens``, ``conversations`` keys.
        """
        days = min(days, 365)
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """SELECT date, messages, llm_calls, tokens, cost_usd,
                              cost_by_provider, cost_by_model, conversations
                       FROM daily_stats
                       WHERE mind_id = ?
                       ORDER BY date DESC
                       LIMIT ?""",
                    (mind_id, days),
                )
                rows = list(await cursor.fetchall())
        except Exception:
            logger.warning("daily_stats_get_history_failed", exc_info=True)
            return []

        # Rows come DESC — reverse to chronological order
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            result.append(
                {
                    "date": row[0],
                    "messages": row[1],
                    "llm_calls": row[2],
                    "tokens": row[3],
                    "cost": round(float(row[4]), 6),
                    "cost_by_provider": _safe_json_loads(row[5]),
                    "cost_by_model": _safe_json_loads(row[6]),
                    "conversations": row[7],
                }
            )
        return result

    async def get_totals(self, mind_id: str = "aria") -> dict[str, Any]:
        """Return all-time aggregated totals.

        Args:
            mind_id: Mind instance to query.

        Returns:
            Dict with ``cost``, ``messages``, ``llm_calls``, ``tokens``,
            ``days_active`` keys.
        """
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """SELECT COALESCE(SUM(cost_usd), 0),
                              COALESCE(SUM(messages), 0),
                              COALESCE(SUM(llm_calls), 0),
                              COALESCE(SUM(tokens), 0),
                              COUNT(*)
                       FROM daily_stats
                       WHERE mind_id = ?""",
                    (mind_id,),
                )
                row = await cursor.fetchone()
        except Exception:
            logger.warning("daily_stats_get_totals_failed", exc_info=True)
            return _empty_totals()

        if row is None:
            return _empty_totals()

        return {
            "cost": round(float(row[0]), 6),
            "messages": int(row[1]),
            "llm_calls": int(row[2]),
            "tokens": int(row[3]),
            "days_active": int(row[4]),
        }

    async def get_month_totals(
        self,
        year: int,
        month: int,
        mind_id: str = "aria",
    ) -> dict[str, Any]:
        """Return aggregated totals for a specific month.

        Args:
            year: Four-digit year.
            month: Month (1-12).
            mind_id: Mind instance to query.

        Returns:
            Dict with ``cost``, ``messages``, ``llm_calls``, ``tokens`` keys.
        """
        # Date prefix for LIKE query: "2026-04-%"
        prefix = f"{year:04d}-{month:02d}-%"
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """SELECT COALESCE(SUM(cost_usd), 0),
                              COALESCE(SUM(messages), 0),
                              COALESCE(SUM(llm_calls), 0),
                              COALESCE(SUM(tokens), 0)
                       FROM daily_stats
                       WHERE mind_id = ? AND date LIKE ?""",
                    (mind_id, prefix),
                )
                row = await cursor.fetchone()
        except Exception:
            logger.warning("daily_stats_get_month_failed", exc_info=True)
            return _empty_month()

        if row is None:
            return _empty_month()

        return {
            "cost": round(float(row[0]), 6),
            "messages": int(row[1]),
            "llm_calls": int(row[2]),
            "tokens": int(row[3]),
        }


def _empty_totals() -> dict[str, Any]:
    """Default empty totals dict."""
    return {
        "cost": 0.0,
        "messages": 0,
        "llm_calls": 0,
        "tokens": 0,
        "days_active": 0,
    }


def _empty_month() -> dict[str, Any]:
    """Default empty month totals dict."""
    return {
        "cost": 0.0,
        "messages": 0,
        "llm_calls": 0,
        "tokens": 0,
    }


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    """Parse JSON string, returning empty dict on failure."""
    if not raw:
        return {}
    try:
        return dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
