"""Dashboard status collector — aggregates data from Engine services.

Provides a snapshot of system state for the /api/status endpoint.
Uses the ServiceRegistry to resolve services lazily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


@dataclass
class StatusSnapshot:
    """Immutable snapshot of system status."""

    version: str
    uptime_seconds: float
    mind_name: str
    active_conversations: int
    memory_concepts: int
    memory_episodes: int
    llm_cost_today: float
    llm_calls_today: int
    tokens_today: int
    messages_today: int
    cost_history: list[dict[str, object]] = field(default_factory=list)
    timezone: str = "UTC"
    today_date: str = ""
    has_lifetime_activity: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "version": self.version,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "mind_name": self.mind_name,
            "active_conversations": self.active_conversations,
            "memory_concepts": self.memory_concepts,
            "memory_episodes": self.memory_episodes,
            "llm_cost_today": round(self.llm_cost_today, 4),
            "llm_calls_today": self.llm_calls_today,
            "tokens_today": self.tokens_today,
            "messages_today": self.messages_today,
            "cost_history": self.cost_history,
            "timezone": self.timezone,
            "today_date": self.today_date,
            "has_lifetime_activity": self.has_lifetime_activity,
        }


class DashboardCounters:
    """Mutable counters updated by instrumented code via increment methods.

    These mirror the OTel metrics but are queryable (OTel counters are write-only).
    Uses a threading.Lock to make the check-then-reset in _maybe_reset atomic.
    Day boundary is determined by the user's configured timezone (default UTC).
    Call :func:`configure_timezone` during bootstrap to set it.
    """

    def __init__(self, timezone: str = "UTC") -> None:
        import threading

        self._lock = threading.Lock()
        self._tz = ZoneInfo(timezone)
        self.llm_calls: int = 0
        self.llm_cost: float = 0.0
        self.tokens: int = 0
        self.messages_received: int = 0
        self._day_key: str = ""

    def record_llm_call(self, cost: float, tokens: int) -> None:
        """Record an LLM call."""
        with self._lock:
            self._maybe_reset()
            self.llm_calls += 1
            self.llm_cost += cost
            self.tokens += tokens

    def record_message(self) -> None:
        """Record an inbound message."""
        with self._lock:
            self._maybe_reset()
            self.messages_received += 1

    def snapshot(self) -> tuple[int, float, int, int]:
        """Atomic read of (llm_calls, llm_cost, tokens, messages_received)."""
        with self._lock:
            self._maybe_reset()
            return self.llm_calls, self.llm_cost, self.tokens, self.messages_received

    def _maybe_reset(self) -> None:
        """Reset counters at day boundary in user timezone. Must be called under lock."""
        today = _now_date_str(self._tz)
        if self._day_key != today:
            self.llm_calls = 0
            self.llm_cost = 0.0
            self.tokens = 0
            self.messages_received = 0
            self._day_key = today


def _now_date_str(tz: ZoneInfo) -> str:
    """Current date as YYYY-MM-DD in the given timezone."""
    from datetime import datetime

    return datetime.now(tz=tz).strftime("%Y-%m-%d")


# Module-level singleton — import and use from anywhere
_counters = DashboardCounters()


def get_counters() -> DashboardCounters:
    """Get the global dashboard counters."""
    return _counters


def configure_timezone(timezone: str) -> None:
    """Set the timezone for daily counter reset.

    Call during bootstrap after loading MindConfig.
    Falls back to UTC if the timezone string is invalid.
    """
    try:
        _counters._tz = ZoneInfo(timezone)
    except (KeyError, Exception):  # noqa: BLE001
        logger.warning("invalid_counter_timezone", timezone=timezone)
        _counters._tz = ZoneInfo("UTC")


class StatusCollector:
    """Collects status from all Engine services.

    Lazily resolves services from the registry to avoid import cycles.
    Gracefully handles missing services (returns defaults).
    """

    def __init__(self, registry: ServiceRegistry, start_time: float | None = None) -> None:
        self._registry = registry
        self._start_time = start_time or time.time()

    async def collect(self) -> StatusSnapshot:
        """Collect a status snapshot from all services."""
        from sovyx import __version__

        # Resolve mind once — used by both mind_name and memory stats
        mind_id_str = await self._get_active_mind_id()
        concepts, episodes = await self._get_memory_stats(mind_id_str)

        counters = get_counters()
        calls, cost, tokens, msgs = counters.snapshot()

        # Display "sovyx" for the default/fallback mind, real name otherwise
        mind_name = "sovyx" if mind_id_str == "default" else mind_id_str

        # Timezone context for frontend
        tz_name = str(counters._tz)
        today = _now_date_str(counters._tz)

        # Lifetime activity: engine has EVER been used (not just today)
        cost_history = await self._get_cost_history()
        has_lifetime = concepts > 0 or episodes > 0 or len(cost_history) > 0

        return StatusSnapshot(
            version=__version__,
            uptime_seconds=time.time() - self._start_time,
            mind_name=mind_name,
            active_conversations=await self._get_conversation_count(),
            memory_concepts=concepts,
            memory_episodes=episodes,
            llm_cost_today=cost,
            llm_calls_today=calls,
            tokens_today=tokens,
            messages_today=msgs,
            cost_history=cost_history,
            timezone=tz_name,
            today_date=today,
            has_lifetime_activity=has_lifetime,
        )

    async def _get_memory_stats(self, mind_id_str: str) -> tuple[int, int]:
        """Get concept and episode counts from brain repositories."""
        from sovyx.engine.types import MindId

        concepts = 0
        episodes = 0
        mind_id = MindId(mind_id_str)

        try:
            from sovyx.brain.concept_repo import ConceptRepository

            if self._registry.is_registered(ConceptRepository):
                c_repo = await self._registry.resolve(ConceptRepository)
                concepts = await c_repo.count(mind_id)
        except Exception:  # noqa: BLE001
            logger.debug("status_concepts_failed")

        try:
            from sovyx.brain.episode_repo import EpisodeRepository

            if self._registry.is_registered(EpisodeRepository):
                e_repo = await self._registry.resolve(EpisodeRepository)
                episodes = await e_repo.count(mind_id)
        except Exception:  # noqa: BLE001
            logger.debug("status_episodes_failed")

        return concepts, episodes

    async def _get_conversation_count(self) -> int:
        """Get count of active conversations."""
        try:
            from sovyx.dashboard.conversations import count_active_conversations

            return await count_active_conversations(self._registry)
        except Exception:  # noqa: BLE001
            logger.debug("status_conversations_failed")
            return 0

    async def _get_active_mind_id(self) -> str:
        """Get the first active mind ID for repository queries."""
        from sovyx.dashboard._shared import get_active_mind_id

        return await get_active_mind_id(self._registry)

    async def _get_cost_history(self) -> list[dict[str, object]]:
        """Get cost log from CostGuard if available."""
        try:
            from sovyx.llm.cost import CostGuard

            if self._registry.is_registered(CostGuard):
                guard = await self._registry.resolve(CostGuard)
                return guard.get_cost_history()
        except Exception:  # noqa: BLE001
            logger.debug("status_cost_history_failed")
        return []
