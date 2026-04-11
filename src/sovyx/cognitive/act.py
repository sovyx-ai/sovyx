"""Sovyx ActPhase — format response + tool execution (ReAct loop).

When the LLM returns tool_calls, ToolExecutor dispatches to PluginManager,
injects results back, and re-invokes the LLM (max iterations configurable).
Financial gate integration preserved — financial tool calls require user
confirmation before execution.

Spec: SPE-003 §4 (ReAct pattern), SPE-008 §6 (PluginManager dispatch)
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from sovyx.llm.models import ToolCall, ToolResult
from sovyx.llm.providers._shared import _sanitize_tool_name
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.financial_gate import FinancialGate
    from sovyx.cognitive.output_guard import OutputGuard
    from sovyx.cognitive.perceive import Perception
    from sovyx.cognitive.pii_guard import PIIGuard
    from sovyx.llm.models import LLMResponse
    from sovyx.llm.router import LLMRouter
    from sovyx.plugins.manager import PluginManager

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
    buttons: list[list[object]] | None = None
    tool_calls_made: list[ToolCall] = dataclasses.field(default_factory=list)
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


class ToolExecutor:
    """Tool execution framework (ReAct pattern, SPE-003 §4).

    Dispatches tool calls to PluginManager.execute(). When no
    PluginManager is configured, returns "no tools available" for
    each call (graceful degradation).
    """

    def __init__(
        self,
        max_depth: int = 3,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self._max_depth = max_depth
        self._plugin_manager = plugin_manager

    @property
    def max_depth(self) -> int:
        """Maximum ReAct loop iterations."""
        return self._max_depth

    @property
    def has_tools(self) -> bool:
        """True if a PluginManager is configured with loaded plugins."""
        if self._plugin_manager is None:
            return False
        return self._plugin_manager.plugin_count > 0

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute tool calls via PluginManager.

        Each call is dispatched independently. If PluginManager is not
        configured, returns "no tools available" for each call.
        """
        results: list[ToolResult] = []
        for call in tool_calls:
            if self._plugin_manager is None:
                results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.function_name,
                        output="Error: no tools available",
                        success=False,
                    )
                )
                continue

            try:
                result = await self._plugin_manager.execute(
                    call.function_name,
                    dict(call.arguments),
                )
                # Copy call_id from the LLM's tool call into the result
                results.append(
                    ToolResult(
                        call_id=call.id,
                        name=result.name,
                        output=result.output,
                        success=result.success,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "tool_execution_error",
                    tool=call.function_name,
                    call_id=call.id,
                    error=str(e),
                )
                results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.function_name,
                        output=f"Error: {e}",
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
            buttons: list[list[object]] = [
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

    async def _apply_output_guard(self, text: str) -> tuple[str, bool, str | None]:
        """Apply output safety filter if configured.

        Uses async cascade (regex→LLM) when available,
        falls back to sync regex-only.

        Returns:
            (filtered_text, was_filtered, filter_reason)
        """
        if self._output_guard is None:
            return text, False, None

        result = await self._output_guard.check_async(text)
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

        # Handle tool calls (ReAct loop)
        if llm_response.tool_calls:
            return await self._react_loop(
                llm_response,
                assembled_messages,
                perception,
                reply_to,
            )

        # Apply output guard + PII guard to LLM response
        text, was_filtered, reason = await self._apply_output_guard(
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

    async def _react_loop(
        self,
        llm_response: LLMResponse,
        assembled_messages: list[dict[str, str]],
        perception: Perception,
        reply_to: str | None,
    ) -> ActionResult:
        """Execute the ReAct loop: tool_calls → execute → re-invoke LLM.

        Max iterations controlled by ToolExecutor.max_depth (default 3).
        Financial gate checked on every iteration.

        Args:
            llm_response: Initial LLM response with tool_calls.
            assembled_messages: Messages used for the initial LLM call.
            perception: Original perception for context.
            reply_to: Reply-to message ID.

        Returns:
            ActionResult with final response text.
        """
        current_response = llm_response
        all_tool_calls: list[ToolCall] = []
        messages: list[dict[str, Any]] = list(assembled_messages)

        for iteration in range(self._tools.max_depth):
            tool_calls = current_response.tool_calls
            if not tool_calls:
                break

            # Financial gate: check before execution
            if self._financial_gate:
                confirmation = self._build_financial_confirmation(
                    tool_calls,
                    perception.source,
                    reply_to,
                )
                if confirmation is not None:
                    confirmation.tool_calls_made = all_tool_calls + tool_calls
                    return confirmation

            # Execute tool calls
            results = await self._tools.execute(tool_calls)
            all_tool_calls.extend(tool_calls)

            logger.info(
                "react_iteration",
                iteration=iteration + 1,
                tool_calls=len(tool_calls),
                successes=sum(1 for r in results if r.success),
                failures=sum(1 for r in results if not r.success),
            )

            # Build messages for re-invocation:
            # 1. Add assistant message WITH tool_calls (required by OpenAI)
            assistant_msg: dict[str, object] = {
                "role": "assistant",
                "content": current_response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": _sanitize_tool_name(tc.function_name),
                            "arguments": json.dumps(tc.arguments)
                            if isinstance(tc.arguments, dict)
                            else str(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            # 2. Add tool results with tool_call_id (required by OpenAI)
            for result in results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": (
                            f"[{result.name}] {'✓' if result.success else '✗'}: {result.output}"
                        ),
                    }
                )

            # 3. Get tools for re-invocation
            tools = self._get_tools_for_reinvocation()

            # 4. Re-invoke LLM with tool results
            try:
                current_response = await self._router.generate(
                    messages=messages,
                    tools=tools,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("react_reinvoke_failed", error=str(e))
                # Fall back to last known content or tool results summary
                fallback = self._summarize_tool_results(results)
                text = self._apply_pii_guard(fallback)
                return ActionResult(
                    response_text=text,
                    target_channel=perception.source,
                    reply_to=reply_to,
                    tool_calls_made=all_tool_calls,
                    degraded=True,
                )

        # Final response (after loop ends or no more tool_calls)
        response_text = current_response.content or ""
        text, was_filtered, reason = await self._apply_output_guard(response_text)
        text = self._apply_pii_guard(text)

        return ActionResult(
            response_text=text,
            target_channel=perception.source,
            reply_to=reply_to,
            tool_calls_made=all_tool_calls,
            output_filtered=was_filtered,
            filter_reason=reason,
        )

    def _get_tools_for_reinvocation(self) -> list[dict[str, object]] | None:
        """Get tool definitions for LLM re-invocation.

        Returns None if no PluginManager is available.
        """
        if not self._tools.has_tools or self._tools._plugin_manager is None:
            return None
        from sovyx.llm.router import LLMRouter

        return LLMRouter.tool_definitions_to_dicts(
            self._tools._plugin_manager.get_tool_definitions(),
        )

    @staticmethod
    def _summarize_tool_results(results: list[ToolResult]) -> str:
        """Summarize tool results for fallback display."""
        if not results:
            return "Tool execution completed but no results available."
        lines: list[str] = []
        for r in results:
            status = "✓" if r.success else "✗"
            lines.append(f"{status} {r.name}: {r.output}")
        return "\n".join(lines)
