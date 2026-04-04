"""Sovyx Bootstrap — wire all services in dependency order."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sovyx.engine.registry import ServiceRegistry
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.config import EngineConfig
    from sovyx.mind.config import MindConfig

logger = get_logger(__name__)


class MindManager:
    """Manage Minds within the engine. v0.1: single mind.

    Interface prepared for multi-mind in v1.0.
    """

    def __init__(self) -> None:
        self._minds: dict[str, object] = {}
        self._active: list[str] = []

    async def load_mind(self, mind_id: str, services: dict[str, object]) -> None:
        """Register a mind's services."""
        self._minds[mind_id] = services

    async def start_mind(self, mind_id: str) -> None:
        """Mark mind as active."""
        if mind_id not in self._active:
            self._active.append(mind_id)
        logger.info("mind_started", mind_id=mind_id)

    async def stop_mind(self, mind_id: str) -> None:
        """Mark mind as inactive."""
        if mind_id in self._active:
            self._active.remove(mind_id)
        logger.info("mind_stopped", mind_id=mind_id)

    def get_active_minds(self) -> list[str]:
        """Return list of active mind IDs."""
        return list(self._active)


async def bootstrap(
    engine_config: EngineConfig,
    mind_configs: Sequence[MindConfig],
) -> ServiceRegistry:
    """Initialize all services in dependency order.

    Order (SPE-001 §init_order):
    1. EventBus
    2. DatabaseManager (pools + migrations)
    3. Per-mind: Brain → Personality → Context → LLM → Cognitive
    4. PersonResolver + ConversationTracker
    5. BridgeManager + Channels

    Returns:
        ServiceRegistry with all services wired.
    """
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.consolidation import ConsolidationCycle, ConsolidationScheduler
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.episode_repo import EpisodeRepository
    from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.retrieval import HybridRetrieval
    from sovyx.brain.service import BrainService
    from sovyx.brain.spreading import SpreadingActivation
    from sovyx.brain.working_memory import WorkingMemory
    from sovyx.bridge.identity import PersonResolver
    from sovyx.bridge.manager import BridgeManager
    from sovyx.bridge.sessions import ConversationTracker
    from sovyx.cognitive.act import ActPhase, ToolExecutor
    from sovyx.cognitive.attend import AttendPhase
    from sovyx.cognitive.gate import CogLoopGate
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.cognitive.perceive import PerceivePhase
    from sovyx.cognitive.reflect import ReflectPhase
    from sovyx.cognitive.state import CognitiveStateMachine
    from sovyx.cognitive.think import ThinkPhase
    from sovyx.context.assembler import ContextAssembler
    from sovyx.context.budget import TokenBudgetManager
    from sovyx.context.formatter import ContextFormatter
    from sovyx.context.tokenizer import TokenCounter
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId
    from sovyx.llm.cost import CostGuard
    from sovyx.llm.providers.anthropic import AnthropicProvider
    from sovyx.llm.providers.ollama import OllamaProvider
    from sovyx.llm.providers.openai import OpenAIProvider
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.personality import PersonalityEngine
    from sovyx.persistence.manager import DatabaseManager

    registry = ServiceRegistry()

    # 1. EventBus
    event_bus = EventBus()
    registry.register_instance(EventBus, event_bus)

    # 2. DatabaseManager
    db_manager = DatabaseManager(engine_config, event_bus)
    await db_manager.start()
    registry.register_instance(DatabaseManager, db_manager)

    # 3. MindManager
    mind_manager = MindManager()
    registry.register_instance(MindManager, mind_manager)

    # Validate minds
    if not mind_configs:
        msg = "No minds configured — at least one MindConfig required"
        raise ValueError(msg)

    # Track last gate for BridgeManager wiring
    gate: CogLoopGate | None = None

    # Process each mind (v0.1: single mind)
    for mind_config in mind_configs:
        mind_id = MindId(mind_config.id)

        # Initialize per-mind databases
        await db_manager.initialize_mind_databases(mind_id)
        brain_pool = db_manager.get_brain_pool(mind_id)

        # Brain components
        embedding = EmbeddingEngine()
        concept_repo = ConceptRepository(brain_pool, embedding)
        episode_repo = EpisodeRepository(brain_pool, embedding)
        relation_repo = RelationRepository(brain_pool)
        working_memory = WorkingMemory()
        spreading = SpreadingActivation(
            relation_repo=relation_repo,
            working_memory=working_memory,
        )
        hebbian = HebbianLearning(relation_repo=relation_repo)
        ebbinghaus = EbbinghausDecay(
            concept_repo=concept_repo,
            relation_repo=relation_repo,
        )
        retrieval = HybridRetrieval(
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            embedding_engine=embedding,
        )

        brain_service = BrainService(
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            embedding_engine=embedding,
            spreading=spreading,
            hebbian=hebbian,
            decay=ebbinghaus,
            retrieval=retrieval,
            working_memory=working_memory,
            event_bus=event_bus,
        )
        registry.register_instance(BrainService, brain_service)

        # Consolidation scheduler
        consolidation_cycle = ConsolidationCycle(
            brain_service=brain_service,
            decay=ebbinghaus,
            event_bus=event_bus,
        )
        consolidation_scheduler = ConsolidationScheduler(
            cycle=consolidation_cycle,
            interval_hours=mind_config.brain.consolidation_interval_hours,
        )
        registry.register_instance(ConsolidationScheduler, consolidation_scheduler)

        # Personality
        personality = PersonalityEngine(mind_config)
        registry.register_instance(PersonalityEngine, personality)

        # Context Assembly
        token_counter = TokenCounter()
        budget_manager = TokenBudgetManager()
        formatter = ContextFormatter(token_counter)
        assembler = ContextAssembler(
            token_counter=token_counter,
            personality_engine=personality,
            brain_service=brain_service,
            budget_manager=budget_manager,
            formatter=formatter,
            mind_config=mind_config,
        )
        registry.register_instance(ContextAssembler, assembler)

        # LLM Providers + Router
        providers: list[AnthropicProvider | OpenAIProvider | OllamaProvider] = []

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            providers.append(AnthropicProvider(api_key=anthropic_key))

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            providers.append(OpenAIProvider(api_key=openai_key))

        providers.append(OllamaProvider())

        cost_guard = CostGuard(
            daily_budget=mind_config.llm.budget_daily_usd,
            per_conversation_budget=mind_config.llm.budget_per_conversation_usd,
        )
        router = LLMRouter(
            providers=providers,
            cost_guard=cost_guard,
            event_bus=event_bus,
        )
        registry.register_instance(LLMRouter, router)

        # Cognitive phases
        state_machine = CognitiveStateMachine()
        perceive = PerceivePhase()
        attend = AttendPhase(safety_config=mind_config.safety)
        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=mind_config,
        )
        act = ActPhase(tool_executor=ToolExecutor(), llm_router=router)
        reflect = ReflectPhase(brain_service=brain_service)

        cog_loop = CognitiveLoop(
            state_machine=state_machine,
            perceive=perceive,
            attend=attend,
            think=think,
            act=act,
            reflect=reflect,
            event_bus=event_bus,
        )
        registry.register_instance(CognitiveLoop, cog_loop)

        gate = CogLoopGate(cog_loop)
        registry.register_instance(CogLoopGate, gate)

        await mind_manager.load_mind(mind_config.id, {"brain": brain_service})
        await mind_manager.start_mind(mind_config.id)

    # 4. PersonResolver + ConversationTracker
    system_pool = db_manager.get_system_pool()
    # Use first mind's conversation pool (v0.1: single mind)
    first_mind_id = MindId(mind_configs[0].id) if mind_configs else MindId("default")
    conv_pool = db_manager.get_conversation_pool(first_mind_id)

    person_resolver = PersonResolver(system_pool)
    conversation_tracker = ConversationTracker(conv_pool)
    registry.register_instance(PersonResolver, person_resolver)
    registry.register_instance(ConversationTracker, conversation_tracker)

    # 5. BridgeManager
    if gate is None:
        msg = "No minds configured — cannot create BridgeManager"
        raise ValueError(msg)

    bridge = BridgeManager(
        event_bus=event_bus,
        cog_loop_gate=gate,
        person_resolver=person_resolver,
        conversation_tracker=conversation_tracker,
        mind_id=first_mind_id,
    )
    registry.register_instance(BridgeManager, bridge)

    # Telegram channel (if token available)
    telegram_token = os.environ.get("SOVYX_TELEGRAM_TOKEN", "")
    if telegram_token:
        from sovyx.bridge.channels.telegram import TelegramChannel

        telegram = TelegramChannel(token=telegram_token, bridge_manager=bridge)
        bridge.register_channel(telegram)

    logger.info(
        "bootstrap_complete",
        minds=len(mind_configs),
    )
    return registry
