"""Dashboard status collector — aggregates data from Engine services.

Provides a snapshot of system state for the /api/status endpoint.
Uses the ServiceRegistry to resolve services lazily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
        }


class DashboardCounters:
    """Mutable counters updated by instrumented code via increment methods.

    These mirror the OTel metrics but are queryable (OTel counters are write-only).
    Uses a threading.Lock to make the check-then-reset in _maybe_reset atomic.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
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
        """Reset counters at day boundary. Must be called under lock."""
        today = time.strftime("%Y-%m-%d")
        if self._day_key != today:
            self.llm_calls = 0
            self.llm_cost = 0.0
            self.tokens = 0
            self.messages_received = 0
            self._day_key = today


# Module-level singleton — import and use from anywhere
_counters = DashboardCounters()


def get_counters() -> DashboardCounters:
    """Get the global dashboard counters."""
    return _counters


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

        mind_name = await self._get_mind_name()
        concepts, episodes = await self._get_memory_stats()

        calls, cost, tokens, _msgs = get_counters().snapshot()

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
        )

    async def _get_mind_name(self) -> str:
        """Get the active mind name."""
        try:
            from sovyx.engine.bootstrap import MindManager

            if self._registry.is_registered(MindManager):
                manager = await self._registry.resolve(MindManager)
                minds = manager.get_active_minds()
                if minds:
                    return minds[0]
        except Exception:  # noqa: BLE001
            logger.debug("status_mind_name_failed")
        return "sovyx"

    async def _get_memory_stats(self) -> tuple[int, int]:
        """Get concept and episode counts from brain repositories."""
        concepts = 0
        episodes = 0

        try:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.engine.types import MindId

            if self._registry.is_registered(ConceptRepository):
                c_repo = await self._registry.resolve(ConceptRepository)
                mind_id = MindId(await self._get_active_mind_id())
                concepts = await c_repo.count(mind_id)
        except Exception:  # noqa: BLE001
            logger.debug("status_concepts_failed")

        try:
            from sovyx.brain.episode_repo import EpisodeRepository
            from sovyx.engine.types import MindId as MindId2

            if self._registry.is_registered(EpisodeRepository):
                e_repo = await self._registry.resolve(EpisodeRepository)
                mind_id2 = MindId2(await self._get_active_mind_id())
                episodes = await e_repo.count(mind_id2)
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
        try:
            from sovyx.engine.bootstrap import MindManager

            if self._registry.is_registered(MindManager):
                manager = await self._registry.resolve(MindManager)
                minds = manager.get_active_minds()
                if minds:
                    return minds[0]
        except Exception:  # noqa: BLE001
            logger.debug("_get_active_mind_id_failed")
        return "default"
