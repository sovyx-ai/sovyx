"""Sovyx ActPhase — format response + tool execution framework."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from sovyx.llm.models import ToolCall, ToolResult
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.perceive import Perception
    from sovyx.llm.models import LLMResponse
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)


@dataclasses.dataclass
class ActionResult:
    """Result of the Act phase — ready for channel delivery."""

    response_text: str
    target_channel: str
    reply_to: str | None = None
    filtered: bool = False
    degraded: bool = False
    error: bool = False
    tool_calls_made: list[ToolCall] = dataclasses.field(default_factory=list)
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


class ToolExecutor:
    """Tool execution framework (ReAct pattern, SPE-003 §4).

    v0.1: No-op executor (no plugins available).
    Framework exists so v0.5+ doesn't need to rewrite ActPhase.
    """

    def __init__(self, max_depth: int = 3) -> None:
        self._max_depth = max_depth
        self._tools: dict[str, object] = {}

    def register_tool(self, name: str, handler: object) -> None:
        """Register a tool handler for future use."""
        self._tools[name] = handler

    async def execute(
        self, tool_calls: list[ToolCall]
    ) -> list[ToolResult]:
        """Execute tool calls.

        v0.1: returns 'no tools available' for each call.
        """
        results: list[ToolResult] = []
        for call in tool_calls:
            results.append(
                ToolResult(
                    call_id=call.id,
                    name=call.function_name,
                    output="Error: no tools available in v0.1",
                    success=False,
                )
            )
        return results


class ActPhase:
    """Format response and prepare for channel delivery.

    If LLM returns tool_calls: ToolExecutor.execute() → re-invoke LLM.
    If LLM returns text: format as ActionResult.
    """

    def __init__(
        self,
        tool_executor: ToolExecutor,
        llm_router: LLMRouter,
    ) -> None:
        self._tools = tool_executor
        self._router = llm_router

    async def process(
        self,
        llm_response: LLMResponse,
        assembled_messages: list[dict[str, str]],
        perception: Perception,
    ) -> ActionResult:
        """Format LLM response into ActionResult.

        If tool_calls present: execute tools, re-invoke LLM (v0.5+).
        """
        reply_to = str(perception.metadata.get("reply_to", "")) or None

        # Check for degraded/error responses
        if llm_response.finish_reason == "error":
            return ActionResult(
                response_text=llm_response.content,
                target_channel=perception.source,
                reply_to=reply_to,
                degraded=True,
            )

        # Handle tool calls (v0.1: framework only)
        if llm_response.tool_calls:
            await self._tools.execute(llm_response.tool_calls)
            # v0.1: no re-invocation, return tool error gracefully
            logger.debug(
                "tool_calls_no_op",
                calls=len(llm_response.tool_calls),
            )
            fallback = "I tried to use a tool but none are available yet."
            return ActionResult(
                response_text=llm_response.content or fallback,
                target_channel=perception.source,
                reply_to=reply_to,
                tool_calls_made=llm_response.tool_calls,
            )

        return ActionResult(
            response_text=llm_response.content,
            target_channel=perception.source,
            reply_to=reply_to,
        )
