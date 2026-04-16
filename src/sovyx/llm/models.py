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


@dataclasses.dataclass
class ToolCallDelta:
    """Incremental update to an in-flight tool call during streaming.

    Providers emit tool_call deltas in pieces — first the call ``id``
    + ``function_name``, then chunks of ``arguments_json`` as the model
    serializes the arguments object. Consumers accumulate deltas keyed
    by ``index`` and parse the final JSON when the stream ends.
    """

    index: int
    id: str | None = None
    function_name: str | None = None
    arguments_json_delta: str = ""


@dataclasses.dataclass
class LLMStreamChunk:
    """One incremental piece of an LLM response.

    Streaming providers yield these in order. ``delta_text`` carries
    the next visible token(s). ``tool_call_delta`` carries an in-flight
    tool call piece (mutually exclusive with text in practice — most
    providers emit one OR the other per chunk).

    The ``is_final`` chunk carries no new ``delta_text`` but signals
    end-of-stream and provides the final usage/finish_reason. Cost
    accounting waits for this chunk because cloud providers only emit
    usage at the end of the SSE stream.
    """

    delta_text: str = ""
    tool_call_delta: ToolCallDelta | None = None
    is_final: bool = False
    finish_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""
