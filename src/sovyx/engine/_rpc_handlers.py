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

    rpc.register_method("chat", _chat)
    rpc.register_method("mind.list", _mind_list)
    rpc.register_method("config.get", _config_get)
    logger.debug("cli_rpc_handlers_registered", count=3)
