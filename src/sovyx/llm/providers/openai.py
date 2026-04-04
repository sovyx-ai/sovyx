"""Sovyx OpenAI provider — GPT API via httpx."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse
from sovyx.llm.providers._shared import retry_delay, safe_parse_json
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

_API_URL = "https://api.openai.com/v1/chat/completions"

_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o1": (15.0, 60.0),
    "o3-mini": (1.1, 4.4),
}
_DEFAULT_PRICING = (5.0, 15.0)

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
                content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason", "stop")

                if not content.strip():
                    error_msg = f"OpenAI returned empty content (model={model})"
                    raise LLMError(error_msg)

                usage = data.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)

                pricing = _PRICING.get(model, _DEFAULT_PRICING)
                cost = (tokens_in * pricing[0] + tokens_out * pricing[1]) / 1_000_000

                logger.debug(
                    "openai_response",
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=round(cost, 6),
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
