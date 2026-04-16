"""Sovyx Anthropic provider — Claude API via httpx."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse, LLMStreamChunk, ToolCall, ToolCallDelta
from sovyx.llm.pricing import PROVIDER_DEFAULT_PRICING, compute_cost
from sovyx.llm.providers._shared import (
    _unsanitize_tool_name,
    format_tools_anthropic,
    parse_tool_calls_anthropic,
    retry_delay,
    safe_parse_json,
)
from sovyx.llm.providers._streaming import iter_sse_events
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

logger = get_logger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_FALLBACK_PRICING = PROVIDER_DEFAULT_PRICING["anthropic"]

_MAX_RETRIES = 3


class AnthropicProvider:
    """Anthropic Claude provider using httpx (no SDK)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "anthropic"

    @property
    def is_available(self) -> bool:
        """True if API key is configured."""
        return bool(self._api_key)

    def supports_model(self, model: str) -> bool:
        """True if model starts with 'claude-'."""
        return model.startswith("claude-")

    def get_context_window(self, model: str | None = None) -> int:
        """200K for all Claude models."""
        return 200_000

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Generate response from Claude.

        Args:
            messages: Chat messages (system extracted automatically).
            model: Claude model name.
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            tools: Optional tool definitions for function calling.

        Returns:
            LLMResponse with content and metadata.

        Raises:
            LLMError: On API error after retries.
            ProviderUnavailableError: On timeout/connection error.
        """
        if not self.is_available:
            msg = "Anthropic API key not configured"
            raise ProviderUnavailableError(msg)

        # Extract system message
        system_msg = ""
        chat_messages: list[dict[str, str]] = []
        for msg_item in messages:
            if msg_item.get("role") == "system":
                system_msg = msg_item.get("content", "")
            else:
                chat_messages.append(msg_item)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_msg:
            payload["system"] = system_msg
        if tools:
            payload["tools"] = format_tools_anthropic(tools)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(_API_URL, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:  # noqa: PLR2004
                    last_error = LLMError(f"Anthropic API error {resp.status_code}: {resp.text}")
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_delay(attempt, resp))
                        continue
                    break

                if resp.status_code != 200:  # noqa: PLR2004
                    error_msg = f"Anthropic API error {resp.status_code}: {resp.text}"
                    raise LLMError(error_msg)

                data = safe_parse_json(resp, "Anthropic")
                latency = int((time.monotonic() - start) * 1000)

                content_blocks = data.get("content", [])
                content = ""
                for block in content_blocks:
                    if block.get("type") == "text":
                        content += block.get("text", "")

                # Parse tool calls
                parsed_tc = parse_tool_calls_anthropic(content_blocks)
                tool_calls_out: list[ToolCall] | None = None
                if parsed_tc:
                    tool_calls_out = [
                        ToolCall(
                            id=tc["id"],
                            function_name=tc["function_name"],
                            arguments=tc["arguments"],
                        )
                        for tc in parsed_tc
                    ]

                stop_reason = data.get("stop_reason", "stop")
                # Map Anthropic stop reasons to normalized values
                finish_reason = "tool_use" if stop_reason == "tool_use" else stop_reason

                if not content.strip() and not tool_calls_out:
                    error_msg = f"Anthropic returned empty content (model={model})"
                    raise LLMError(error_msg)

                usage = data.get("usage", {})
                tokens_in = usage.get("input_tokens", 0)
                tokens_out = usage.get("output_tokens", 0)

                cost = compute_cost(model, tokens_in, tokens_out, fallback=_FALLBACK_PRICING)

                logger.debug(
                    "anthropic_response",
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=round(cost, 6),
                    tool_calls=len(parsed_tc),
                )

                return LLMResponse(
                    content=content,
                    model=data.get("model", model),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                    provider="anthropic",
                    tool_calls=tool_calls_out,
                )

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_delay(attempt))
                    continue
                break
            except httpx.ConnectError as e:
                error_msg = f"Anthropic connection failed: {e}"
                raise ProviderUnavailableError(error_msg) from e

        if isinstance(last_error, httpx.TimeoutException):
            error_msg = f"Anthropic request timed out after {_MAX_RETRIES} retries"
            raise ProviderUnavailableError(error_msg) from last_error
        if last_error:
            raise LLMError(str(last_error)) from last_error

        error_msg = "Anthropic: unexpected error"
        raise LLMError(error_msg)

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream incremental chunks from Claude.

        Anthropic Messages SSE event flow:
            * ``message_start`` — initial usage (input_tokens only).
            * ``content_block_start`` — begins a text or tool_use block.
            * ``content_block_delta`` — ``text_delta`` (visible text) or
              ``input_json_delta`` (tool args being serialized).
            * ``content_block_stop`` — block boundary (no payload needed).
            * ``message_delta`` — final usage (output_tokens) and
              stop_reason.
            * ``message_stop`` — terminal marker.

        Yields:
            Per-chunk text deltas and tool-call deltas, then a final
            ``is_final=True`` chunk with usage + finish_reason.
        """
        if not self.is_available:
            msg = "Anthropic API key not configured"
            raise ProviderUnavailableError(msg)

        system_msg = ""
        chat_messages: list[dict[str, str]] = []
        for msg_item in messages:
            if msg_item.get("role") == "system":
                system_msg = msg_item.get("content", "")
            else:
                chat_messages.append(msg_item)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
            "stream": True,
        }
        if system_msg:
            payload["system"] = system_msg
        if tools:
            payload["tools"] = format_tools_anthropic(tools)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        # Per-block tool-use accumulator. Block index -> (id, name, args_str).
        # Anthropic emits tool_use as a sequence: content_block_start with
        # id+name, then a stream of input_json_delta with partial JSON,
        # then content_block_stop. We forward the deltas as ToolCallDelta
        # so the router/cogloop can rebuild the final ToolCall.
        tool_blocks: dict[int, dict[str, str]] = {}

        tokens_in = 0
        tokens_out = 0
        finish_reason = "stop"

        try:
            async with self._client.stream(
                "POST", _API_URL, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:  # noqa: PLR2004
                    body = await resp.aread()
                    error_msg = (
                        f"Anthropic stream error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:200]}"
                    )
                    raise LLMError(error_msg)

                async for event_type, data in iter_sse_events(resp):
                    if event_type == "message_start":
                        usage = data.get("message", {}).get("usage", {})
                        tokens_in = usage.get("input_tokens", 0)
                        continue

                    if event_type == "content_block_start":
                        index = data.get("index", 0)
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_blocks[index] = {
                                "id": block.get("id", ""),
                                "name": _unsanitize_tool_name(block.get("name", "")),
                                "args": "",
                            }
                            yield LLMStreamChunk(
                                tool_call_delta=ToolCallDelta(
                                    index=index,
                                    id=tool_blocks[index]["id"],
                                    function_name=tool_blocks[index]["name"],
                                ),
                                model=model,
                                provider="anthropic",
                            )
                        continue

                    if event_type == "content_block_delta":
                        index = data.get("index", 0)
                        delta = data.get("delta", {})
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield LLMStreamChunk(
                                    delta_text=text,
                                    model=model,
                                    provider="anthropic",
                                )
                        elif delta_type == "input_json_delta":
                            partial = delta.get("partial_json", "")
                            if index in tool_blocks:
                                tool_blocks[index]["args"] += partial
                            yield LLMStreamChunk(
                                tool_call_delta=ToolCallDelta(
                                    index=index,
                                    arguments_json_delta=partial,
                                ),
                                model=model,
                                provider="anthropic",
                            )
                        continue

                    if event_type == "message_delta":
                        delta = data.get("delta", {})
                        stop_reason = delta.get("stop_reason")
                        if stop_reason:
                            finish_reason = (
                                "tool_use" if stop_reason == "tool_use" else stop_reason
                            )
                        usage = data.get("usage", {})
                        if "output_tokens" in usage:
                            tokens_out = usage["output_tokens"]
                        continue

                    # message_stop / content_block_stop / unknown — no-op.

        except httpx.ConnectError as exc:
            error_msg = f"Anthropic stream connection failed: {exc}"
            raise ProviderUnavailableError(error_msg) from exc
        except httpx.TimeoutException as exc:
            error_msg = f"Anthropic stream timed out: {exc}"
            raise ProviderUnavailableError(error_msg) from exc

        yield LLMStreamChunk(
            is_final=True,
            finish_reason=finish_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            provider="anthropic",
        )
