"""RPC handlers for CLI ↔ daemon communication (SPE-015 §2).

The CLI talks to the daemon via JSON-RPC 2.0 over a Unix-domain socket
exposed by :class:`sovyx.engine.rpc_server.DaemonRPCServer`. Handlers
live in this module so that ``cli/main.py::start`` can wire them with
a single call (``register_cli_handlers(rpc, registry)``) instead of
inlining a dozen lambdas in the bootstrap function.

Each handler is a thin adapter: it resolves the right service from
the :class:`ServiceRegistry` and delegates to existing engine code.
No business logic lives here — keep this file as a routing table
for the CLI surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.engine.rpc_server import DaemonRPCServer

logger = get_logger(__name__)

# Stable identity for every CLI session. PersonResolver attaches all
# CLI traffic to the same person row regardless of which terminal the
# user is running from — matches the dashboard's single-identity
# treatment (see ``dashboard/chat.py::_DASHBOARD_CHANNEL_USER_ID``).
CLI_CHANNEL_USER_ID = "cli-user"

# Display name surfaced to the LLM and conversation history. Kept
# short and identifying so the assistant knows it is talking to the
# operator at the terminal, not a Telegram contact.
CLI_USER_NAME = "CLI"


def register_cli_handlers(
    rpc: DaemonRPCServer,
    registry: ServiceRegistry,
) -> None:
    """Register every RPC method exposed to the CLI on ``rpc``.

    Idempotent: re-registration overwrites the previous handler. Safe
    to call after construction without any guard.
    """

    async def _chat(
        message: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a single chat turn through the cognitive loop."""
        from sovyx.dashboard.chat import handle_chat_message  # noqa: PLC0415
        from sovyx.engine.types import ChannelType  # noqa: PLC0415

        return await handle_chat_message(
            registry=registry,
            message=message,
            user_name=CLI_USER_NAME,
            conversation_id=conversation_id,
            channel=ChannelType.CLI,
            channel_user_id=CLI_CHANNEL_USER_ID,
        )

    async def _mind_list() -> dict[str, Any]:
        """Return the list of active minds and which one is the default."""
        from sovyx.engine.bootstrap import MindManager  # noqa: PLC0415

        mgr = await registry.resolve(MindManager)
        active = mgr.get_active_minds()
        return {
            "minds": active,
            "active": active[0] if active else None,
        }

    async def _config_get() -> dict[str, Any]:
        """Return the active mind's config in a shape friendly for tabular display.

        The full :class:`MindConfig` is large; we surface the fields
        the REPL needs (name, language, timezone, brain knobs, llm
        provider/model). The dashboard pulls the same struct via
        ``PersonalityEngine.config``; the CLI handler reuses that
        accessor so both surfaces stay in sync without a second
        registration of MindConfig.
        """
        from sovyx.bridge.manager import BridgeManager  # noqa: PLC0415
        from sovyx.engine.bootstrap import MindManager  # noqa: PLC0415
        from sovyx.mind.personality import PersonalityEngine  # noqa: PLC0415

        mind_id: str | None = None
        mgr = await registry.resolve(MindManager)
        active = mgr.get_active_minds()
        if active:
            mind_id = active[0]
        elif registry.is_registered(BridgeManager):
            bridge = await registry.resolve(BridgeManager)
            mind_id = str(bridge.mind_id)

        if not registry.is_registered(PersonalityEngine):
            return {"mind_id": mind_id, "available": False}

        personality = await registry.resolve(PersonalityEngine)
        cfg = personality.config
        return {
            "mind_id": mind_id or cfg.name,
            "available": True,
            "name": cfg.name,
            "language": cfg.language,
            "timezone": cfg.timezone,
            "template": cfg.template,
            "llm": {
                "default_provider": cfg.llm.default_provider,
                "default_model": cfg.llm.default_model,
                "fast_model": cfg.llm.fast_model,
                "temperature": cfg.llm.temperature,
                "budget_daily_usd": cfg.llm.budget_daily_usd,
            },
            "brain": {
                "consolidation_interval_hours": cfg.brain.consolidation_interval_hours,
                "dream_time": cfg.brain.dream_time,
                "dream_lookback_hours": cfg.brain.dream_lookback_hours,
                "dream_max_patterns": cfg.brain.dream_max_patterns,
                "max_concepts": cfg.brain.max_concepts,
                "forgetting_enabled": cfg.brain.forgetting_enabled,
                "decay_rate": cfg.brain.decay_rate,
            },
        }

    async def _mind_forget(
        mind_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Right-to-erasure: wipe every per-mind row across all pools.

        Phase 8 / T8.21 step 4 — daemon-side surface for the
        ``sovyx mind forget <mind_id>`` CLI. Resolves the live
        :class:`DatabaseManager` + :class:`EngineConfig` from the
        registry, builds a :class:`MindForgetService` against the
        target mind's pools + the shared system pool + the consent
        ledger, and runs the wipe.

        Args:
            mind_id: Target mind. Empty / whitespace is rejected by
                the underlying service.
            dry_run: When True, return the count report without
                writing. CLI's ``--dry-run`` confirmation flow.

        Returns:
            JSON-serialisable dict mirroring :class:`MindForgetReport`
            field-for-field. Every count is an int; ``dry_run`` is
            a bool.
        """
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.mind.forget import MindForgetService  # noqa: PLC0415
        from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415
        from sovyx.voice._consent_ledger import ConsentLedger  # noqa: PLC0415

        config = await registry.resolve(EngineConfig)
        db_manager = await registry.resolve(DatabaseManager)

        mid = MindId(mind_id)
        # The brain + conversations pools are per-mind so resolving
        # them validates that the mind actually exists; a missing
        # mind raises DatabaseConnectionError which the RPC layer
        # surfaces to the operator as a clear error.
        brain_pool = db_manager.get_brain_pool(mid)
        conv_pool = db_manager.get_conversation_pool(mid)
        system_pool = db_manager.get_system_pool()

        # ConsentLedger is path-resolved (no registry instance);
        # missing file is treated as an empty ledger by the service
        # so the path is always passed even when the file doesn't
        # exist yet.
        ledger_path = config.data_dir / "voice" / "consent.jsonl"
        ledger = ConsentLedger(path=ledger_path)

        service = MindForgetService(
            brain_pool=brain_pool,
            conversations_pool=conv_pool,
            system_pool=system_pool,
            ledger=ledger,
        )
        report = await service.forget_mind(mid, dry_run=dry_run)

        return {
            "mind_id": str(report.mind_id),
            "concepts_purged": report.concepts_purged,
            "relations_purged": report.relations_purged,
            "episodes_purged": report.episodes_purged,
            "concept_embeddings_purged": report.concept_embeddings_purged,
            "episode_embeddings_purged": report.episode_embeddings_purged,
            "conversation_imports_purged": report.conversation_imports_purged,
            "consolidation_log_purged": report.consolidation_log_purged,
            "conversations_purged": report.conversations_purged,
            "conversation_turns_purged": report.conversation_turns_purged,
            "daily_stats_purged": report.daily_stats_purged,
            "consent_ledger_purged": report.consent_ledger_purged,
            "total_brain_rows_purged": report.total_brain_rows_purged,
            "total_conversations_rows_purged": report.total_conversations_rows_purged,
            "total_system_rows_purged": report.total_system_rows_purged,
            "total_rows_purged": report.total_rows_purged,
            "dry_run": report.dry_run,
        }

    rpc.register_method("chat", _chat)
    rpc.register_method("mind.list", _mind_list)
    rpc.register_method("mind.forget", _mind_forget)
    rpc.register_method("config.get", _config_get)
    logger.debug("cli_rpc_handlers_registered", count=4)
