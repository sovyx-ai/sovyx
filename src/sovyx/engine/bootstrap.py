"""Sovyx Bootstrap — wire all services in dependency order."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sovyx.engine.config import EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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

    On partial failure, already-initialized resources are cleaned up
    in reverse order (see ``sovyx-imm-d6-bootstrap`` §1).

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
    _closables: list[object] = []  # cleanup on failure (reverse order)

    try:
        # 0. Load channel.env (tokens saved via dashboard setup flow)
        _channel_env = engine_config.data_dir / "channel.env"
        if _channel_env.exists():
            for _line in _channel_env.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()  # noqa: PLW2901
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _k, _v = _k.strip(), _v.strip()
                    if _k and _v and _k.startswith("SOVYX_") and _k not in os.environ:
                        os.environ[_k] = _v

        # 0. EngineConfig + logging setup
        registry.register_instance(EngineConfig, engine_config)

        # Setup structured logging with file handler (for dashboard log viewer).
        # Console format controlled by config.console_format (default: "text").
        # File handler always writes JSON (for machine parsing).
        from sovyx.observability.logging import setup_logging

        setup_logging(engine_config.log)

        # 1. EventBus
        event_bus = EventBus()
        registry.register_instance(EventBus, event_bus)

        # 2. DatabaseManager
        db_manager = DatabaseManager(engine_config, event_bus)
        await db_manager.start()
        _closables.append(db_manager)
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

            # Configure daily counter timezone + persistence from mind config
            from sovyx.dashboard.status import configure_timezone, get_counters

            configure_timezone(
                mind_config.timezone,
                system_pool=db_manager.get_system_pool(),
            )
            await get_counters().restore()

            # Initialize per-mind databases
            await db_manager.initialize_mind_databases(mind_id)
            brain_pool = db_manager.get_brain_pool(mind_id)

            # Brain components — preload embedding model on startup
            embedding = EmbeddingEngine()
            await embedding.ensure_loaded()
            concept_repo = ConceptRepository(brain_pool, embedding)
            episode_repo = EpisodeRepository(brain_pool, embedding)
            relation_repo = RelationRepository(brain_pool)
            working_memory = WorkingMemory()
            spreading = SpreadingActivation(
                relation_repo=relation_repo,
                working_memory=working_memory,
            )
            hebbian = HebbianLearning(
                relation_repo=relation_repo,
                concept_repo=concept_repo,
            )
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
            registry.register_instance(ConceptRepository, concept_repo)
            registry.register_instance(RelationRepository, relation_repo)
            registry.register_instance(EpisodeRepository, episode_repo)

            # Consolidation scheduler
            consolidation_cycle = ConsolidationCycle(
                brain_service=brain_service,
                decay=ebbinghaus,
                event_bus=event_bus,
                concept_repo=concept_repo,
                relation_repo=relation_repo,
            )
            consolidation_scheduler = ConsolidationScheduler(
                cycle=consolidation_cycle,
                interval_hours=mind_config.brain.consolidation_interval_hours,
            )
            _closables.append(consolidation_scheduler)
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
            from sovyx.llm.providers.google import GoogleProvider

            providers: list[
                AnthropicProvider | OpenAIProvider | GoogleProvider | OllamaProvider
            ] = []

            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if anthropic_key:
                providers.append(AnthropicProvider(api_key=anthropic_key))
                logger.info("llm_provider_registered", provider="anthropic")

            openai_key = os.environ.get("OPENAI_API_KEY", "")
            if openai_key:
                providers.append(OpenAIProvider(api_key=openai_key))
                logger.info("llm_provider_registered", provider="openai")

            google_key = os.environ.get("GOOGLE_API_KEY", "")
            if google_key:
                providers.append(GoogleProvider(api_key=google_key))
                logger.info("llm_provider_registered", provider="google")

            ollama_provider = OllamaProvider()
            providers.append(ollama_provider)

            # Always ping Ollama to set _verified flag correctly.
            # Health checks and Settings page need accurate availability.
            await ollama_provider.ping()

            # Auto-detect Ollama when no cloud providers are configured
            cloud_providers = [p for p in providers if p.name != "ollama"]
            if not cloud_providers:
                if ollama_provider.is_available:
                    models = await ollama_provider.list_models()
                    if models:
                        selected = _select_best_ollama_model(models)
                        mind_config.llm.default_provider = "ollama"
                        mind_config.llm.default_model = selected
                        logger.info(
                            "ollama_auto_detected",
                            models=models,
                            selected=selected,
                            hint="Using local Ollama. No cloud API key needed.",
                        )
                        # Persist so next restart reads config directly
                        _persist_ollama_config(
                            mind_config,
                            engine_config.database.data_dir / mind_config.id / "mind.yaml",
                        )
                    else:
                        logger.warning(
                            "ollama_no_models",
                            hint="Ollama is running but has no models. Run: ollama pull llama3.1",
                        )
                else:
                    logger.warning(
                        "no_llm_provider_detected",
                        hint="Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY. "
                        "Or install Ollama: https://ollama.ai",
                    )

            # fast_model fallback: ThinkPhase uses fast_model for low-complexity
            # queries. If empty, those calls silently fail (model="").
            if not mind_config.llm.fast_model and mind_config.llm.default_model:
                mind_config.llm.fast_model = mind_config.llm.default_model

            logger.info(
                "llm_router_config",
                default_model=mind_config.llm.default_model,
                default_provider=mind_config.llm.default_provider,
                providers=[p.name for p in providers if p.is_available],
            )

            # DailyStatsRecorder for historical usage tracking
            from sovyx.dashboard.daily_stats import DailyStatsRecorder

            stats_recorder = DailyStatsRecorder(db_manager.get_system_pool())
            registry.register_instance(DailyStatsRecorder, stats_recorder)

            cost_guard = CostGuard(
                daily_budget=mind_config.llm.budget_daily_usd,
                per_conversation_budget=mind_config.llm.budget_per_conversation_usd,
                system_pool=db_manager.get_system_pool(),
                timezone=mind_config.timezone,
                stats_recorder=stats_recorder,
            )
            await cost_guard.restore()
            registry.register_instance(CostGuard, cost_guard)
            router = LLMRouter(
                providers=providers,
                cost_guard=cost_guard,
                event_bus=event_bus,
            )
            _closables.append(router)
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
            from sovyx.cognitive.output_guard import OutputGuard

            output_guard = OutputGuard(safety_config=mind_config.safety)
            act = ActPhase(
                tool_executor=ToolExecutor(),
                llm_router=router,
                output_guard=output_guard,
            )
            reflect = ReflectPhase(
                brain_service=brain_service,
                llm_router=router,
                fast_model=mind_config.llm.fast_model,
            )

            cog_loop = CognitiveLoop(
                state_machine=state_machine,
                perceive=perceive,
                attend=attend,
                think=think,
                act=act,
                reflect=reflect,
                event_bus=event_bus,
                brain=brain_service,
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

    except Exception:
        # Cleanup already-initialized resources in reverse order
        for resource in reversed(_closables):
            try:
                stop_fn = getattr(resource, "stop", None)
                close_fn = getattr(resource, "close", None)
                if stop_fn is not None:
                    await stop_fn()
                elif close_fn is not None:
                    await close_fn()
            except Exception:
                logger.warning(
                    "bootstrap_cleanup_failed",
                    resource=type(resource).__name__,
                    exc_info=True,
                )
        raise


# ── Ollama auto-detection helpers ────────────────────────────


# Priority order: newer/larger models first.
_PREFERRED_OLLAMA_MODELS: list[str] = [
    "llama3.1",
    "llama3",
    "llama3.2",
    "mistral",
    "gemma2",
    "qwen2.5",
    "phi3",
    "codellama",
    "deepseek-coder",
]


def _select_best_ollama_model(models: list[str]) -> str:
    """Pick the best available Ollama model by preference order.

    Strips tag suffixes (e.g. ``"llama3.1:latest"`` → ``"llama3.1"``)
    for matching, then returns the original name with tag.

    Args:
        models: Non-empty list of model names from ``OllamaProvider.list_models()``.

    Returns:
        Best model name (with original tag), or first model as fallback.
    """
    # Build lookup: base_name → full_name (first occurrence wins)
    base_to_full: dict[str, str] = {}
    for m in models:
        base = m.split(":")[0]
        if base not in base_to_full:
            base_to_full[base] = m

    for preferred in _PREFERRED_OLLAMA_MODELS:
        if preferred in base_to_full:
            return base_to_full[preferred]

    return models[0]


def _persist_ollama_config(mind_config: MindConfig, mind_yaml_path: Path) -> None:
    """Persist auto-detected Ollama config to mind.yaml.

    Creates the file if it doesn't exist. Merges with existing YAML
    to preserve user-edited fields in other sections.

    Args:
        mind_config: The MindConfig with updated LLM fields.
        mind_yaml_path: Path to mind.yaml.
    """
    import yaml

    existing: dict[str, object] = {}
    if mind_yaml_path.exists():
        try:
            with open(mind_yaml_path) as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            logger.debug("mind_yaml_read_failed", path=str(mind_yaml_path))

    # Update only the LLM section — don't clobber other config
    existing["llm"] = {
        "default_provider": mind_config.llm.default_provider,
        "default_model": mind_config.llm.default_model,
        "fast_model": mind_config.llm.fast_model,
        "temperature": mind_config.llm.temperature,
        "streaming": mind_config.llm.streaming,
        "budget_daily_usd": mind_config.llm.budget_daily_usd,
        "budget_per_conversation_usd": mind_config.llm.budget_per_conversation_usd,
    }

    try:
        mind_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mind_yaml_path, "w") as f:
            yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)
        logger.info("ollama_config_persisted", path=str(mind_yaml_path))
    except Exception:
        logger.warning("ollama_config_persist_failed", path=str(mind_yaml_path))
