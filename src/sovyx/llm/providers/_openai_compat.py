"""Base class for OpenAI-compatible LLM providers.

xAI (Grok), DeepSeek, Mistral, Together AI, Groq, and Fireworks all
expose an API wire-compatible with OpenAI's ``/v1/chat/completions``.
This module provides the shared ``generate()`` + ``stream()`` logic so
each provider file is ~30 LOC of configuration.

The original ``OpenAIProvider`` is also refactored to subclass this
base, proving the abstraction against the existing test suite.
"""

from __future__ import annotations

import asyncio
import dataclasses
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

_MAX_RETRIES = 3


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Static configuration for an OpenAI-compatible provider."""

    name: str
    api_url: str
    api_key_env: str
    default_model: str
    supported_prefixes: tuple[str, ...]
    context_windows: dict[str, int]
    default_context_window: int = 128_000


class OpenAICompatibleProvider:
    """Base for any provider that speaks the OpenAI Chat Completions wire format.

    Subclasses supply a :class:`ProviderConfig` and an API key.  The
    base handles ``generate()``, ``stream()``, retry, error handling,
    tool-call parsing, and cost computation identically to the original
    ``OpenAIProvider``.
    """

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return self._config.name

    @property
    def is_available(self) -> bool:
        """True if API key is configured."""
        return bool(self._api_key)

    def supports_model(self, model: str) -> bool:
        """True if model starts with one of the configured prefixes."""
        return model.startswith(self._config.supported_prefixes)

    def get_context_window(self, model: str | None = None) -> int:
        """Context window for the model."""
        if model:
            return self._config.context_windows.get(model, self._config.default_context_window)
        return self._config.default_context_window

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        """Build authorization headers."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _fallback_pricing(self) -> tuple[float, float]:
        return PROVIDER_DEFAULT_PRICING.get(self._config.name, (3.0, 15.0))

    # ── generate ──────────────────────────────────────────────

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Generate response from an OpenAI-compatible endpoint."""
        use_model = model or self._config.default_model
        if not self.is_available:
            msg = f"{self._config.name} API key not configured"
            raise ProviderUnavailableError(msg)

        payload: dict[str, Any] = {
            "model": use_model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = format_tools_openai(tools)

        headers = self._headers()
        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(self._config.api_url, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:  # noqa: PLR2004
                    last_error = LLMError(
                        f"{self._config.name} API error {resp.status_code}: {resp.text}"
                    )
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_delay(attempt, resp))
                        continue
                    break

                if resp.status_code != 200:  # noqa: PLR2004
                    error_msg = f"{self._config.name} API error {resp.status_code}: {resp.text}"
                    raise LLMError(error_msg)

                data = safe_parse_json(resp, self._config.name)
                latency = int((time.monotonic() - start) * 1000)

                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content", "") or ""
                finish_reason = choice.get("finish_reason", "stop")

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
                    error_msg = f"{self._config.name} returned empty content (model={use_model})"
                    raise LLMError(error_msg)

                usage = data.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)

                cost = compute_cost(
                    use_model,
                    tokens_in,
                    tokens_out,
                    fallback=self._fallback_pricing(),
                )

                logger.debug(
                    "openai_compat_response",
                    provider=self._config.name,
                    model=use_model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=round(cost, 6),
                    tool_calls=len(tool_calls_out) if tool_calls_out else 0,
                )

                return LLMResponse(
                    content=content,
                    model=data.get("model", use_model),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                    provider=self._config.name,
                    tool_calls=tool_calls_out,
                )

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_delay(attempt))
                    continue
                break
            except httpx.ConnectError as e:
                error_msg = f"{self._config.name} connection failed: {e}"
                raise ProviderUnavailableError(error_msg) from e

        if isinstance(last_error, httpx.TimeoutException):
            error_msg = f"{self._config.name} request timed out after {_MAX_RETRIES} retries"
            raise ProviderUnavailableError(error_msg) from last_error
        if last_error:
            raise LLMError(str(last_error)) from last_error

        error_msg = f"{self._config.name}: unexpected error"
        raise LLMError(error_msg)

    # ── stream ────────────────────────────────────────────────

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream incremental chunks from an OpenAI-compatible endpoint."""
        use_model = model or self._config.default_model
        if not self.is_available:
            msg = f"{self._config.name} API key not configured"
            raise ProviderUnavailableError(msg)

        payload: dict[str, Any] = {
            "model": use_model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = format_tools_openai(tools)

        headers = self._headers()
        tokens_in = 0
        tokens_out = 0
        finish_reason: str | None = None

        try:
            async with self._client.stream(
                "POST", self._config.api_url, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:  # noqa: PLR2004
                    body = await resp.aread()
                    error_msg = (
                        f"{self._config.name} stream error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:200]}"
                    )
                    raise LLMError(error_msg)

                async for event_type, data in iter_sse_events(resp):
                    if event_type == "done":
                        break

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
                            model=data.get("model", use_model),
                            provider=self._config.name,
                        )

                    raw_tcs = delta.get("tool_calls") or []
                    for raw_tc in raw_tcs:
                        index = raw_tc.get("index", 0)
                        tc_id = raw_tc.get("id")
                        func = raw_tc.get("function") or {}
                        tc_name = func.get("name")
                        if tc_name:
                            tc_name = _unsanitize_tool_name(tc_name)
                        args_delta = func.get("arguments") or ""
                        if tc_id is None and not tc_name and not args_delta:
                            continue
                        yield LLMStreamChunk(
                            tool_call_delta=ToolCallDelta(
                                index=index,
                                id=tc_id,
                                function_name=tc_name,
                                arguments_json_delta=args_delta,
                            ),
                            model=data.get("model", use_model),
                            provider=self._config.name,
                        )

        except httpx.ConnectError as exc:
            error_msg = f"{self._config.name} stream connection failed: {exc}"
            raise ProviderUnavailableError(error_msg) from exc
        except httpx.TimeoutException as exc:
            error_msg = f"{self._config.name} stream timed out: {exc}"
            raise ProviderUnavailableError(error_msg) from exc

        yield LLMStreamChunk(
            is_final=True,
            finish_reason=finish_reason or "stop",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=use_model,
            provider=self._config.name,
        )
