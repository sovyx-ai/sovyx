"""Sovyx Ollama provider — local LLM via httpx."""

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

_CONTEXT_WINDOWS: dict[str, int] = {
    "llama3.2:1b": 8_192,
    "llama3.2:3b": 8_192,
    "llama3.1:8b": 128_000,
    "mistral:7b": 32_000,
    "phi3:mini": 128_000,
}
_DEFAULT_CONTEXT = 8_192

_MAX_RETRIES = 3


class OllamaProvider:
    """Ollama local LLM provider using httpx."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "ollama"

    @property
    def is_available(self) -> bool:
        """Always True — availability checked at runtime."""
        return True

    def supports_model(self, model: str) -> bool:
        """Ollama can serve any local model."""
        return True

    def get_context_window(self, model: str | None = None) -> int:
        """Context window for the model."""
        if model:
            return _CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT)
        return _DEFAULT_CONTEXT

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "llama3.2:1b",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Generate response from Ollama."""
        url = f"{self._base_url}/api/chat"

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(url, json=payload)
                if resp.status_code >= 500:  # noqa: PLR2004
                    last_error = LLMError(f"Ollama error {resp.status_code}: {resp.text}")
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_delay(attempt, resp))
                        continue
                    break

                if resp.status_code != 200:  # noqa: PLR2004
                    error_msg = f"Ollama error {resp.status_code}: {resp.text}"
                    raise LLMError(error_msg)

                data = safe_parse_json(resp, "Ollama")
                latency = int((time.monotonic() - start) * 1000)

                message = data.get("message", {})
                content = message.get("content", "")

                if not content.strip():
                    error_msg = f"Ollama returned empty content (model={model})"
                    raise LLMError(error_msg)

                tokens_in = data.get("prompt_eval_count", 0)
                tokens_out = data.get("eval_count", 0)

                logger.debug(
                    "ollama_response",
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                )

                return LLMResponse(
                    content=content,
                    model=data.get("model", model),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=0.0,  # local — free
                    finish_reason="stop",
                    provider="ollama",
                )

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_delay(attempt))
                    continue
                break
            except httpx.ConnectError as e:
                error_msg = f"Ollama not reachable at {self._base_url}: {e}"
                raise ProviderUnavailableError(error_msg) from e

        if isinstance(last_error, httpx.TimeoutException):
            error_msg = f"Ollama timed out after {_MAX_RETRIES} retries"
            raise ProviderUnavailableError(error_msg) from last_error
        if last_error:
            raise LLMError(str(last_error)) from last_error

        error_msg = "Ollama: unexpected error"
        raise LLMError(error_msg)
