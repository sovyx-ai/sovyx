"""Sovyx ThinkPhase — context assembly + LLM call with model routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.llm.models import LLMResponse, LLMStreamChunk
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sovyx.cognitive.perceive import Perception
    from sovyx.context.assembler import ContextAssembler
    from sovyx.engine.types import MindId
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.config import MindConfig
    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)


DEGRADED_MODEL = "degraded"
"""Sentinel ``model`` value the ThinkPhase stamps on its degradation
fallback (``LLMResponse`` / ``LLMStreamChunk``) when the LLM call fails.
Paired with :data:`DEGRADED_PROVIDER`. Detected downstream by
:func:`is_degraded_llm_response` so the cognitive loop marks the
``ActionResult`` honestly (``degraded`` + ``error``) instead of letting
the canned fallback be processed as a normal answer (W1.2 / G-P1-1)."""

DEGRADED_PROVIDER = "none"
"""Sentinel ``provider`` value paired with :data:`DEGRADED_MODEL`."""

_DEGRADED_DETAIL_MAX_CHARS = 200
"""Cap on the sanitized exception summary attached to the degradation
fallback — keeps the metadata channel (and any dashboard rendering it)
bounded regardless of how verbose the underlying exception is."""


def _sanitize_degraded_detail(exc: BaseException) -> str:
    """Collapse an exception message to a short single-line summary.

    Whitespace (including newlines from multi-line exception messages)
    is collapsed to single spaces, then truncated to
    :data:`_DEGRADED_DETAIL_MAX_CHARS`. Observability-only — the full
    traceback stays in the ``logger.exception`` line.
    """
    return " ".join(str(exc).split())[:_DEGRADED_DETAIL_MAX_CHARS]


def is_degraded_llm_response(*, model: str, provider: str, finish_reason: str) -> bool:
    """True iff the response carries the ThinkPhase degradation sentinel.

    Single source of truth for the producer (ThinkPhase fallback) ↔
    consumer (cognitive loop, voice bridge) contract — callers MUST use
    this rather than matching the fields as independent literals
    (anti-pattern #53).
    """
    return model == DEGRADED_MODEL and provider == DEGRADED_PROVIDER and finish_reason == "error"


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
                phase="think",
                mind_id=str(mind_id),
            )

            logger.debug(
                "think_complete",
                model=response.model,
                tokens=response.tokens_in + response.tokens_out,
            )

            return response, ctx.messages

        except Exception as exc:  # noqa: BLE001
            # Observability-only attribution (resilience behaviour and the
            # spoken degradation message are unchanged): stamp the exception
            # class + a short sanitized summary on the fallback so downstream
            # consumers (cognitive loop → ActionResult.metadata → W1.2 voice
            # bridge signal, dashboards) can tell a provider outage from a
            # real bug instead of one indistinguishable degraded sentinel.
            error_type = type(exc).__name__
            logger.exception("think_phase_failed", error_type=error_type)
            degraded = LLMResponse(
                content=self._degradation_message,
                model=DEGRADED_MODEL,
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                cost_usd=0.0,
                finish_reason="error",
                provider=DEGRADED_PROVIDER,
                degraded_reason=error_type,
                degraded_detail=_sanitize_degraded_detail(exc),
            )
            return degraded, []

    async def process_streaming(
        self,
        perception: Perception,
        mind_id: MindId,
        conversation_history: list[dict[str, str]],
        person_name: str | None = None,
    ) -> tuple[AsyncIterator[LLMStreamChunk], list[dict[str, str]]]:
        """Think (streaming): assemble context → stream LLM → yield chunks.

        Returns ``(chunk_iterator, assembled_messages)`` so the caller
        can iterate chunks for real-time TTS while keeping the messages
        for tool re-invocation if the stream ends with tool_calls.
        """
        try:
            raw_complexity = perception.metadata.get("complexity", 0.5)
            complexity = (
                float(raw_complexity) if isinstance(raw_complexity, (int, float, str)) else 0.5
            )
            model = self._select_model(complexity)
            context_window = self._router.get_context_window(model)

            ctx = await self._assembler.assemble(
                mind_id=mind_id,
                current_message=perception.content,
                conversation_history=conversation_history,
                person_name=person_name,
                complexity=complexity,
                context_window=context_window,
            )

            tools: list[dict[str, object]] | None = None
            if self._plugin_manager and self._plugin_manager.plugin_count > 0:
                from sovyx.llm.router import LLMRouter as _LLMRouter

                defs = self._plugin_manager.get_tool_definitions()
                tools = _LLMRouter.tool_definitions_to_dicts(defs) or None

            chunk_iter = self._router.stream(
                messages=ctx.messages,
                model=model,
                temperature=self._mind_config.llm.temperature,
                tools=tools,
                phase="think",
                mind_id=str(mind_id),
            )

            return chunk_iter, ctx.messages

        except Exception as exc:  # noqa: BLE001
            # Same observability-only attribution as the non-streaming path —
            # the final chunk carries the exception class + sanitized summary
            # so the loop's reconstructed LLMResponse (and ActionResult
            # metadata) can attribute the degraded turn.
            error_type = type(exc).__name__
            detail = _sanitize_degraded_detail(exc)
            logger.exception("think_phase_streaming_failed", error_type=error_type)

            async def _degraded_iter() -> AsyncIterator[LLMStreamChunk]:
                yield LLMStreamChunk(
                    delta_text=self._degradation_message,
                    is_final=True,
                    finish_reason="error",
                    model=DEGRADED_MODEL,
                    provider=DEGRADED_PROVIDER,
                    degraded_reason=error_type,
                    degraded_detail=detail,
                )

            return _degraded_iter(), []

    def _select_model(self, complexity: float) -> str:
        """Select model based on complexity.

        complexity < 0.3 → fast_model (haiku)
        complexity >= 0.3 → default_model (sonnet)
        """
        if complexity < 0.3:  # noqa: PLR2004
            return self._mind_config.llm.fast_model
        return self._mind_config.llm.default_model
