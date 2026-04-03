"""Sovyx LLM response models."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class ToolCall:
    """Tool call from LLM (ReAct pattern, SPE-003 §4)."""

    id: str
    function_name: str
    arguments: dict[str, object]


@dataclasses.dataclass
class ToolResult:
    """Tool execution result."""

    call_id: str
    name: str
    output: str
    success: bool


@dataclasses.dataclass
class LLMResponse:
    """Unified LLM response across all providers."""

    content: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_usd: float
    finish_reason: str  # "stop", "max_tokens", "tool_use", "error"
    provider: str
    tool_calls: list[ToolCall] | None = None
