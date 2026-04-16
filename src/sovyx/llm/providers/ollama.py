"""Sovyx Ollama provider — local LLM via httpx."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse, LLMStreamChunk, ToolCall, ToolCallDelta
from sovyx.llm.providers._shared import (
    _unsanitize_tool_name,
    format_tools_openai,
    parse_tool_calls_openai,
    retry_delay,
    safe_parse_json,
)
from sovyx.llm.providers._streaming import iter_ndjson_lines
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

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

# Standard env var used by the official Ollama CLI and libraries.
_OLLAMA_HOST_ENV = "OLLAMA_HOST"
_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider:
    """Ollama local LLM provider using httpx.

    Resolves base URL in this order:
    1. Explicit ``base_url`` constructor arg
    2. ``OLLAMA_HOST`` env var (same as official Ollama CLI)
    3. ``http://localhost:11434`` default

    After construction, call :meth:`ping` to verify reachability.
    ``is_available`` returns ``False`` until a successful ping.
    """

    def __init__(
        self,
        base_url: str | None = None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get(_OLLAMA_HOST_ENV, _DEFAULT_BASE_URL)
        self._base_url = base_url.rstrip("/")
        self._verified: bool = False
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "ollama"

    @property
    def base_url(self) -> str:
        """Resolved Ollama base URL."""
        return self._base_url

    @property
    def is_available(self) -> bool:
        """True only after a successful :meth:`ping`.

        Before ping, returns False — prevents the router from sending
        requests to an Ollama instance that may not exist.
        """
        return self._verified

    def supports_model(self, model: str) -> bool:
        """Ollama serves local models only.

        Cloud models (claude-*, gpt-*, gemini-*, o1*, o3*) are rejected
        so the router doesn't incorrectly route to Ollama or use
        Ollama's 8K context window for cloud model calculations.
        """
        _cloud_prefixes = ("claude-", "gpt-", "gemini-", "o1", "o3")
        return not model.startswith(_cloud_prefixes)

    def get_context_window(self, model: str | None = None) -> int:
        """Context window for the model."""
        if model:
            return _CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT)
        return _DEFAULT_CONTEXT

    # ── Discovery ────────────────────────────────────────────

    async def ping(self, timeout: float = 2.0) -> bool:
        """Check if Ollama is reachable and set the ``_verified`` flag.

        Uses ``GET /api/tags`` because it's lightweight and confirms
        the Ollama server is fully operational (not just listening).

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            True if Ollama responded with HTTP 200.
        """
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/tags",
                timeout=timeout,
            )
            self._verified = resp.status_code == 200  # noqa: PLR2004
            if self._verified:
                logger.debug("ollama_ping_ok", base_url=self._base_url)
            else:
                logger.debug(
                    "ollama_ping_unexpected_status",
                    base_url=self._base_url,
                    status=resp.status_code,
                )
            return self._verified
        except (httpx.HTTPError, OSError, TimeoutError):
            # Ollama is optional — daemon-not-running, wrong port, and
            # DNS failures all look the same to us. Log with traceback
            # so "Ollama ping failed" can be told apart from permanent
            # misconfig without repro'ing the request.
            self._verified = False
            logger.debug(
                "ollama_ping_failed",
                base_url=self._base_url,
                exc_info=True,
            )
            return False

    async def list_models(self, timeout: float = 5.0) -> list[str]:
        """Return names of locally installed Ollama models.

        Calls ``GET /api/tags`` and extracts the ``name`` field from
        each model entry.  Names include the tag suffix returned by
        Ollama (e.g. ``"llama3.1:latest"``).

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            Sorted list of model name strings, empty on any error.
        """
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/tags",
                timeout=timeout,
            )
            if resp.status_code != 200:  # noqa: PLR2004
                return []
            data = resp.json()
            models = sorted(m["name"] for m in data.get("models", []) if "name" in m)
            logger.debug("ollama_models_listed", count=len(models), models=models[:5])
            return models
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            # httpx: network. OSError: low-level socket. TimeoutError:
            # asyncio. ValueError: json.JSONDecodeError (subclass) when
            # the response body isn't valid JSON. Returning an empty
            # list signals "no models" upstream; traceback preserved so
            # the failure mode is distinguishable in logs.
            logger.debug(
                "ollama_list_models_failed",
                base_url=self._base_url,
                exc_info=True,
            )
            return []

    # ── Lifecycle ────────────────────────────────────────────

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    # ── Generation ───────────────────────────────────────────

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "llama3.2:1b",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
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
        if tools:
            payload["tools"] = format_tools_openai(tools)

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
                content = message.get("content", "") or ""

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
                    tool_calls=len(tool_calls_out) if tool_calls_out else 0,
                )

                return LLMResponse(
                    content=content,
                    model=data.get("model", model),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=0.0,  # local — free
                    finish_reason="tool_calls" if tool_calls_out else "stop",
                    provider="ollama",
                    tool_calls=tool_calls_out,
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

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "llama3.2:1b",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream incremental chunks from Ollama.

        Ollama NDJSON flow:
            * Each JSON line carries ``message.content`` (text delta).
            * The terminal line has ``done: true`` plus
              ``prompt_eval_count`` / ``eval_count`` for usage.
        """
        url = f"{self._base_url}/api/chat"

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = format_tools_openai(tools)

        tokens_in = 0
        tokens_out = 0

        try:
            async with self._client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:  # noqa: PLR2004
                    body = await resp.aread()
                    error_msg = (
                        f"Ollama stream error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:200]}"
                    )
                    raise LLMError(error_msg)

                async for data in iter_ndjson_lines(resp):
                    if data.get("done"):
                        tokens_in = data.get("prompt_eval_count", 0)
                        tokens_out = data.get("eval_count", 0)
                        break

                    message = data.get("message", {}) or {}
                    text = message.get("content", "") or ""
                    if text:
                        yield LLMStreamChunk(
                            delta_text=text,
                            model=data.get("model", model),
                            provider="ollama",
                        )

                    # Ollama tool calls arrive complete in the message
                    raw_tcs = message.get("tool_calls") or []
                    for i, raw_tc in enumerate(raw_tcs):
                        func = raw_tc.get("function", {})
                        name = func.get("name", "")
                        if name:
                            name = _unsanitize_tool_name(name)
                        args = func.get("arguments", {}) or {}
                        yield LLMStreamChunk(
                            tool_call_delta=ToolCallDelta(
                                index=i,
                                id=f"ollama-{i}",
                                function_name=name,
                                arguments_json_delta=json.dumps(args)
                                if isinstance(args, dict)
                                else str(args),
                            ),
                            model=data.get("model", model),
                            provider="ollama",
                        )

        except httpx.ConnectError as exc:
            error_msg = f"Ollama stream not reachable at {self._base_url}: {exc}"
            raise ProviderUnavailableError(error_msg) from exc
        except httpx.TimeoutException as exc:
            error_msg = f"Ollama stream timed out: {exc}"
            raise ProviderUnavailableError(error_msg) from exc

        yield LLMStreamChunk(
            is_final=True,
            finish_reason="stop",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            provider="ollama",
        )
