"""Sovyx OpenAI provider — GPT API via httpx."""

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
    format_tools_openai,
    parse_tool_calls_openai,
    retry_delay,
    safe_parse_json,
)
from sovyx.llm.providers._streaming import iter_sse_events
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

logger = get_logger(__name__)

_API_URL = "https://api.openai.com/v1/chat/completions"
_FALLBACK_PRICING = PROVIDER_DEFAULT_PRICING["openai"]

_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
}

_MAX_RETRIES = 3


class OpenAIProvider:
    """OpenAI GPT provider using httpx (no SDK)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "openai"

    @property
    def is_available(self) -> bool:
        """True if API key is configured."""
        return bool(self._api_key)

    def supports_model(self, model: str) -> bool:
        """True if model starts with gpt-, o1-, or o3-."""
        return model.startswith(("gpt-", "o1", "o3"))

    def get_context_window(self, model: str | None = None) -> int:
        """Context window for the model."""
        if model:
            return _CONTEXT_WINDOWS.get(model, 128_000)
        return 128_000

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Generate response from OpenAI."""
        if not self.is_available:
            msg = "OpenAI API key not configured"
            raise ProviderUnavailableError(msg)

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = format_tools_openai(tools)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(_API_URL, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:  # noqa: PLR2004
                    last_error = LLMError(f"OpenAI API error {resp.status_code}: {resp.text}")
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_delay(attempt, resp))
                        continue
                    break

                if resp.status_code != 200:  # noqa: PLR2004
                    error_msg = f"OpenAI API error {resp.status_code}: {resp.text}"
                    raise LLMError(error_msg)

                data = safe_parse_json(resp, "OpenAI")
                latency = int((time.monotonic() - start) * 1000)

                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content", "") or ""
                finish_reason = choice.get("finish_reason", "stop")

                # Parse tool calls
                tool_calls_out: list[ToolCall] | None = None
                raw_tc = message.get("tool_calls")
                if raw_tc:
                    parsed_tc = parse_tool_calls_openai(raw_tc)
                    tool_calls_out = [
                        ToolCall(
                            id=tc["id"],
                            function_name=tc["function_name"],
                            arguments=tc["arguments"],
                        )
                        for tc in parsed_tc
                    ]

                if not content.strip() and not tool_calls_out:
                    error_msg = f"OpenAI returned empty content (model={model})"
                    raise LLMError(error_msg)

                usage = data.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)

                cost = compute_cost(model, tokens_in, tokens_out, fallback=_FALLBACK_PRICING)

                logger.debug(
                    "openai_response",
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=round(cost, 6),
                    tool_calls=len(tool_calls_out) if tool_calls_out else 0,
                )

                return LLMResponse(
                    content=content,
                    model=data.get("model", model),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                    provider="openai",
                    tool_calls=tool_calls_out,
                )

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_delay(attempt))
                    continue
                break
            except httpx.ConnectError as e:
                error_msg = f"OpenAI connection failed: {e}"
                raise ProviderUnavailableError(error_msg) from e

        if isinstance(last_error, httpx.TimeoutException):
            error_msg = f"OpenAI request timed out after {_MAX_RETRIES} retries"
            raise ProviderUnavailableError(error_msg) from last_error
        if last_error:
            raise LLMError(str(last_error)) from last_error

        error_msg = "OpenAI: unexpected error"
        raise LLMError(error_msg)

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream incremental chunks from OpenAI Chat Completions.

        OpenAI SSE flow:
            * Each ``data:`` line carries one chat-completion chunk with
              ``choices[0].delta``. Text arrives in ``delta.content``.
              Tool calls arrive incrementally in ``delta.tool_calls``
              (indexed; ``id``/``function.name`` on the first chunk for
              each tool, then ``function.arguments`` deltas).
            * ``finish_reason`` shows up on the final delta of the choice.
            * Usage arrives in a final chunk with empty choices when
              ``stream_options.include_usage=true`` is set in the request.
            * The literal ``data: [DONE]`` ends the stream.
        """
        if not self.is_available:
            msg = "OpenAI API key not configured"
            raise ProviderUnavailableError(msg)

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = format_tools_openai(tools)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        tokens_in = 0
        tokens_out = 0
        finish_reason: str | None = None

        try:
            async with self._client.stream(
                "POST", _API_URL, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:  # noqa: PLR2004
                    body = await resp.aread()
                    error_msg = (
                        f"OpenAI stream error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:200]}"
                    )
                    raise LLMError(error_msg)

                async for event_type, data in iter_sse_events(resp):
                    if event_type == "done":
                        break

                    # Usage chunk: empty choices, populated usage block.
                    usage = data.get("usage")
                    if usage:
                        tokens_in = usage.get("prompt_tokens", tokens_in)
                        tokens_out = usage.get("completion_tokens", tokens_out)

                    choices = data.get("choices") or []
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {}) or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    text_delta = delta.get("content") or ""
                    if text_delta:
                        yield LLMStreamChunk(
                            delta_text=text_delta,
                            model=data.get("model", model),
                            provider="openai",
                        )

                    raw_tcs = delta.get("tool_calls") or []
                    for raw_tc in raw_tcs:
                        index = raw_tc.get("index", 0)
                        tc_id = raw_tc.get("id")
                        func = raw_tc.get("function") or {}
                        name = func.get("name")
                        if name:
                            name = _unsanitize_tool_name(name)
                        args_delta = func.get("arguments") or ""
                        if tc_id is None and not name and not args_delta:
                            continue
                        yield LLMStreamChunk(
                            tool_call_delta=ToolCallDelta(
                                index=index,
                                id=tc_id,
                                function_name=name,
                                arguments_json_delta=args_delta,
                            ),
                            model=data.get("model", model),
                            provider="openai",
                        )

        except httpx.ConnectError as exc:
            error_msg = f"OpenAI stream connection failed: {exc}"
            raise ProviderUnavailableError(error_msg) from exc
        except httpx.TimeoutException as exc:
            error_msg = f"OpenAI stream timed out: {exc}"
            raise ProviderUnavailableError(error_msg) from exc

        yield LLMStreamChunk(
            is_final=True,
            finish_reason=finish_reason or "stop",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            provider="openai",
        )
