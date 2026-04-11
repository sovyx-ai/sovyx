"""Sovyx ActPhase — format response + tool execution framework."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from sovyx.llm.models import ToolCall, ToolResult
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.bridge.protocol import InlineButton
    from sovyx.cognitive.financial_gate import FinancialGate
    from sovyx.cognitive.output_guard import OutputGuard
    from sovyx.cognitive.perceive import Perception
    from sovyx.cognitive.pii_guard import PIIGuard
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
    output_filtered: bool = False
    filter_reason: str | None = None
    pending_confirmation: bool = False
    confirmation_details: dict[str, object] | None = None
    buttons: list[list[InlineButton]] | None = None
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

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
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
    If LLM returns text: apply OutputGuard → format as ActionResult.
    """

    def __init__(
        self,
        tool_executor: ToolExecutor,
        llm_router: LLMRouter,
        output_guard: OutputGuard | None = None,
        financial_gate: FinancialGate | None = None,
        pii_guard: PIIGuard | None = None,
    ) -> None:
        self._tools = tool_executor
        self._router = llm_router
        self._output_guard = output_guard
        self._financial_gate = financial_gate
        self._pii_guard = pii_guard

    def _build_financial_confirmation(
        self,
        tool_calls: list[ToolCall],
        source: str,
        reply_to: str | None,
    ) -> ActionResult | None:
        """Check tool calls against the financial gate.

        Returns ActionResult with inline buttons if any tool call is
        financial, or None if all calls can proceed.
        """
        from sovyx.bridge.protocol import InlineButton  # noqa: TC001
        from sovyx.cognitive.financial_gate import PendingConfirmation  # noqa: TC001

        assert self._financial_gate is not None  # noqa: S101

        pending_list: list[tuple[ToolCall, PendingConfirmation]] = []
        for tc in tool_calls:
            pending = self._financial_gate.check_tool_call(tc)
            if pending:
                pending_list.append((tc, pending))

        if not pending_list:
            return None

        # Build confirmation message
        if len(pending_list) == 1:
            tc, pending = pending_list[0]
            confirm_msg = f"⚠️ Financial action requires confirmation:\n{pending.summary}"
            buttons: list[list[InlineButton]] = [
                [
                    InlineButton(
                        text="✅ Approve",
                        callback_data=f"fin_confirm:{tc.id}",
                    ),
                    InlineButton(
                        text="❌ Deny",
                        callback_data=f"fin_cancel:{tc.id}",
                    ),
                ]
            ]
            details: dict[str, object] = {
                "tool_call_id": tc.id,
                "tool_name": tc.function_name,
                "summary": pending.summary,
            }
        else:
            # Multiple financial tool calls — batch confirmation
            lines = ["⚠️ Multiple financial actions require confirmation:"]
            for i, (_tc, pending) in enumerate(pending_list, 1):
                lines.append(f"{i}. {pending.summary}")
            confirm_msg = "\n".join(lines)
            # Use first tool call ID as group anchor
            group_id = pending_list[0][0].id
            buttons = [
                [
                    InlineButton(
                        text="✅ Approve All",
                        callback_data=f"fin_confirm_all:{group_id}",
                    ),
                    InlineButton(
                        text="❌ Deny All",
                        callback_data=f"fin_cancel_all:{group_id}",
                    ),
                ]
            ]
            details = {
                "tool_call_ids": [tc.id for tc, _ in pending_list],
                "tool_names": [tc.function_name for tc, _ in pending_list],
                "summaries": [p.summary for _, p in pending_list],
                "count": len(pending_list),
            }

        return ActionResult(
            response_text=confirm_msg,
            target_channel=source,
            reply_to=reply_to,
            pending_confirmation=True,
            confirmation_details=details,
            buttons=buttons,
            tool_calls_made=tool_calls,
        )

    def _apply_pii_guard(self, text: str) -> str:
        """Apply PII redaction if configured.

        Returns:
            Text with PII redacted (or unchanged if disabled).
        """
        if self._pii_guard is None:
            return text
        result = self._pii_guard.check(text)
        return result.text

    def _apply_output_guard(self, text: str) -> tuple[str, bool, str | None]:
        """Apply output safety filter if configured.

        Returns:
            (filtered_text, was_filtered, filter_reason)
        """
        if self._output_guard is None:
            return text, False, None

        result = self._output_guard.check(text)
        if not result.filtered:
            return text, False, None

        reason = None
        if result.match and result.match.category:
            reason = f"{result.action}:{result.match.category.value}"
        else:
            reason = result.action

        return result.text, True, reason

    async def process(
        self,
        llm_response: LLMResponse,
        assembled_messages: list[dict[str, str]],
        perception: Perception,
    ) -> ActionResult:
        """Format LLM response into ActionResult.

        If tool_calls present: execute tools, re-invoke LLM (v0.5+).
        Otherwise: apply output guard → format as ActionResult.
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
            # Financial gate: check if any tool call needs confirmation
            if self._financial_gate:
                result = self._build_financial_confirmation(
                    llm_response.tool_calls,
                    perception.source,
                    reply_to,
                )
                if result is not None:
                    return result

            await self._tools.execute(llm_response.tool_calls)
            # v0.1: no re-invocation, return tool error gracefully
            logger.debug(
                "tool_calls_no_op",
                calls=len(llm_response.tool_calls),
            )
            fallback = "I tried to use a tool but none are available yet."
            response_text = llm_response.content or fallback

            # Apply output guard + PII guard to tool-call responses
            text, was_filtered, reason = self._apply_output_guard(
                response_text,
            )
            text = self._apply_pii_guard(text)

            return ActionResult(
                response_text=text,
                target_channel=perception.source,
                reply_to=reply_to,
                tool_calls_made=llm_response.tool_calls,
                output_filtered=was_filtered,
                filter_reason=reason,
            )

        # Apply output guard + PII guard to LLM response
        text, was_filtered, reason = self._apply_output_guard(
            llm_response.content,
        )
        text = self._apply_pii_guard(text)

        return ActionResult(
            response_text=text,
            target_channel=perception.source,
            reply_to=reply_to,
            output_filtered=was_filtered,
            filter_reason=reason,
        )
