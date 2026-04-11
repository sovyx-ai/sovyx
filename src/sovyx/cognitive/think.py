"""Sovyx ThinkPhase — context assembly + LLM call with model routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.llm.models import LLMResponse
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.perceive import Perception
    from sovyx.context.assembler import ContextAssembler
    from sovyx.engine.types import MindId
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.config import MindConfig
    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)


class ThinkPhase:
    """Assemble context + call LLM with complexity-based model routing.

    Order (critical — context_window depends on model):
        1. Model selection FIRST (only needs complexity from metadata)
        2. Get context_window from selected model via router
        3. Context assembly with REAL context_window
        4. LLM generate with selected model
        5. Parse response
    """

    def __init__(
        self,
        context_assembler: ContextAssembler,
        llm_router: LLMRouter,
        mind_config: MindConfig,
        plugin_manager: PluginManager | None = None,
        degradation_message: str = "I'm having trouble thinking clearly right now.",
    ) -> None:
        self._assembler = context_assembler
        self._router = llm_router
        self._mind_config = mind_config
        self._plugin_manager = plugin_manager
        self._degradation_message = degradation_message

    async def process(
        self,
        perception: Perception,
        mind_id: MindId,
        conversation_history: list[dict[str, str]],
        person_name: str | None = None,
    ) -> tuple[LLMResponse, list[dict[str, str]]]:
        """Think: assemble context → call LLM → return response + messages.

        Returns:
            (LLMResponse, assembled_messages) — messages needed for
            tool re-invocation in ActPhase (v0.5+).
        """
        try:
            # 1. Model selection (complexity-based)
            raw_complexity = perception.metadata.get("complexity", 0.5)
            complexity = (
                float(raw_complexity) if isinstance(raw_complexity, (int, float, str)) else 0.5
            )
            model = self._select_model(complexity)

            # 2. Context window from selected model
            context_window = self._router.get_context_window(model)

            # 3. Context assembly
            ctx = await self._assembler.assemble(
                mind_id=mind_id,
                current_message=perception.content,
                conversation_history=conversation_history,
                person_name=person_name,
                complexity=complexity,
                context_window=context_window,
            )

            # 4. Build tool definitions for LLM
            tools: list[dict[str, object]] | None = None
            if self._plugin_manager and self._plugin_manager.plugin_count > 0:
                from sovyx.llm.router import LLMRouter as _LLMRouter

                defs = self._plugin_manager.get_tool_definitions()
                tools = _LLMRouter.tool_definitions_to_dicts(defs) or None

            # 5. LLM generate
            response = await self._router.generate(
                messages=ctx.messages,
                model=model,
                temperature=self._mind_config.llm.temperature,
                tools=tools,
            )

            logger.debug(
                "think_complete",
                model=response.model,
                tokens=response.tokens_in + response.tokens_out,
            )

            return response, ctx.messages

        except Exception:
            logger.exception("think_phase_failed")
            degraded = LLMResponse(
                content=self._degradation_message,
                model="degraded",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                cost_usd=0.0,
                finish_reason="error",
                provider="none",
            )
            return degraded, []

    def _select_model(self, complexity: float) -> str:
        """Select model based on complexity.

        complexity < 0.3 → fast_model (haiku)
        complexity >= 0.3 → default_model (sonnet)
        """
        if complexity < 0.3:  # noqa: PLR2004
            return self._mind_config.llm.fast_model
        return self._mind_config.llm.default_model
