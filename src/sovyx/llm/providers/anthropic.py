"""Sovyx Anthropic provider — Claude API via httpx."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.llm.providers._shared import (
    format_tools_anthropic,
    parse_tool_calls_anthropic,
    retry_delay,
    safe_parse_json,
)
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Cost per 1M tokens (USD)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (1.0, 5.0),
    "claude-opus-4-20250514": (15.0, 75.0),
}
_DEFAULT_PRICING = (3.0, 15.0)  # fallback

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

                pricing = _PRICING.get(model, _DEFAULT_PRICING)
                cost = (tokens_in * pricing[0] + tokens_out * pricing[1]) / 1_000_000

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
