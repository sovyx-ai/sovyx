"""Sovyx Google provider — Gemini API via httpx.

Supports Gemini 2.0 Flash (default) and Gemini 2.5 Pro.
Context window: 1M tokens for both models.

Ref: SPE-007 §GoogleProvider, Pre-Compute V05-36.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse, LLMStreamChunk, ToolCall, ToolCallDelta
from sovyx.llm.pricing import PROVIDER_DEFAULT_PRICING, compute_cost
from sovyx.llm.providers._shared import (
    _unsanitize_tool_name,
    format_tools_google,
    parse_tool_calls_google,
    retry_delay,
    safe_parse_json,
)
from sovyx.llm.providers._streaming import iter_sse_events
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

logger = get_logger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_FALLBACK_PRICING = PROVIDER_DEFAULT_PRICING["google"]

_CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-flash-preview-04-17": 1_048_576,
    "gemini-2.5-pro-preview-03-25": 1_048_576,
}
_DEFAULT_CONTEXT_WINDOW = 1_048_576

_MAX_RETRIES = 3


class GoogleProvider:
    """Google Gemini provider using httpx (no SDK).

    Uses the ``generateContent`` REST endpoint with an API key.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "google"

    @property
    def is_available(self) -> bool:
        """True if API key is configured."""
        return bool(self._api_key)

    def supports_model(self, model: str) -> bool:
        """True if model starts with 'gemini-'."""
        return model.startswith("gemini-")

    def get_context_window(self, model: str | None = None) -> int:
        """Context window size (defaults to 1M)."""
        if model is None:
            return _DEFAULT_CONTEXT_WINDOW
        return _CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)

    async def close(self) -> None:
        """Close httpx client."""
        await self._client.aclose()

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "gemini-2.0-flash",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Generate response from Gemini.

        Args:
            messages: Chat messages (role + content).
            model: Gemini model name.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.

        Returns:
            LLMResponse with content and metadata.

        Raises:
            LLMError: On API error after retries.
            ProviderUnavailableError: On timeout/connection error.
        """
        if not self.is_available:
            msg = "Google API key not configured"
            raise ProviderUnavailableError(msg)

        # Convert messages to Gemini format
        contents, system_instruction = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}],
            }
        if tools:
            payload["tools"] = format_tools_google(tools)

        url = f"{_API_BASE}/{model}:generateContent"
        # Send API key via header (NOT query string) — keeps the key out of
        # access logs, error stacktraces (httpx stringifies URL on failure),
        # and any URL-based caching/telemetry layers.
        headers = {"x-goog-api-key": self._api_key}

        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(url, json=payload, headers=headers)

                if resp.status_code == 429 or resp.status_code >= 500:  # noqa: PLR2004
                    last_error = LLMError(f"Google API error {resp.status_code}: {resp.text}")
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_delay(attempt, resp))
                        continue
                    break

                if resp.status_code != 200:  # noqa: PLR2004
                    error_msg = f"Google API error {resp.status_code}: {resp.text}"
                    raise LLMError(error_msg)

                data = safe_parse_json(resp, "Google")
                latency = int((time.monotonic() - start) * 1000)

                content = self._extract_content(data)

                # Parse tool calls from function call parts
                tool_calls_out: list[ToolCall] | None = None
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    parsed_tc = parse_tool_calls_google(parts)
                    if parsed_tc:
                        tool_calls_out = [
                            ToolCall(
                                id=tc["id"],
                                function_name=tc["function_name"],
                                arguments=tc["arguments"],
                            )
                            for tc in parsed_tc
                        ]

                if not content.strip() and not tool_calls_out:
                    error_msg = f"Google returned empty content (model={model})"
                    raise LLMError(error_msg)

                # Extract usage metadata
                usage = data.get("usageMetadata", {})
                tokens_in = usage.get("promptTokenCount", 0)
                tokens_out = usage.get("candidatesTokenCount", 0)

                cost = compute_cost(model, tokens_in, tokens_out, fallback=_FALLBACK_PRICING)

                # Extract finish reason
                finish_reason = "stop"
                if candidates:
                    raw_reason = candidates[0].get("finishReason", "STOP")
                    finish_reason = raw_reason.lower()

                logger.debug(
                    "google_response",
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=round(cost, 6),
                    tool_calls=len(tool_calls_out) if tool_calls_out else 0,
                )

                return LLMResponse(
                    content=content,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                    provider="google",
                    tool_calls=tool_calls_out,
                )

            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_delay(attempt))
                    continue
                break
            except httpx.ConnectError as exc:
                error_msg = f"Google connection failed: {exc}"
                raise ProviderUnavailableError(error_msg) from exc

        if isinstance(last_error, httpx.TimeoutException):
            error_msg = f"Google request timed out after {_MAX_RETRIES} retries"
            raise ProviderUnavailableError(error_msg) from last_error
        if last_error:
            raise LLMError(str(last_error)) from last_error

        error_msg = "Google: unexpected error"
        raise LLMError(error_msg)

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str = "gemini-2.0-flash",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream incremental chunks from Gemini.

        Gemini's ``:streamGenerateContent?alt=sse`` flow:
            * Each SSE event carries a ``GenerateContentResponse``-shaped
              JSON with ``candidates[0].content.parts``. Text parts hold
              incremental tokens; ``functionCall`` parts arrive complete
              (Gemini does NOT split function-call args across chunks).
            * ``usageMetadata`` is included on the FINAL chunk only.
            * ``finishReason`` arrives on the final candidate.
        """
        if not self.is_available:
            msg = "Google API key not configured"
            raise ProviderUnavailableError(msg)

        contents, system_instruction = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}],
            }
        if tools:
            payload["tools"] = format_tools_google(tools)

        url = f"{_API_BASE}/{model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": self._api_key}

        tokens_in = 0
        tokens_out = 0
        finish_reason = "stop"
        # Gemini emits each functionCall as a complete part. We assign
        # synthetic indices in the order they appear so downstream
        # ToolCallDelta indices stay monotonic across the stream.
        tool_index_counter = 0

        try:
            async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:  # noqa: PLR2004
                    body = await resp.aread()
                    error_msg = (
                        f"Google stream error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:200]}"
                    )
                    raise LLMError(error_msg)

                async for _event_type, data in iter_sse_events(resp):
                    candidates = data.get("candidates") or []
                    if candidates:
                        candidate = candidates[0]
                        parts = candidate.get("content", {}).get("parts", []) or []
                        for part in parts:
                            text = part.get("text")
                            if text:
                                yield LLMStreamChunk(
                                    delta_text=text,
                                    model=model,
                                    provider="google",
                                )
                            fc = part.get("functionCall")
                            if fc:
                                args = fc.get("args", {}) or {}
                                yield LLMStreamChunk(
                                    tool_call_delta=ToolCallDelta(
                                        index=tool_index_counter,
                                        id=f"gemini-{tool_index_counter}",
                                        function_name=_unsanitize_tool_name(fc.get("name", "")),
                                        arguments_json_delta=json.dumps(args)
                                        if isinstance(args, dict)
                                        else str(args),
                                    ),
                                    model=model,
                                    provider="google",
                                )
                                tool_index_counter += 1
                        raw_reason = candidate.get("finishReason")
                        if raw_reason:
                            finish_reason = raw_reason.lower()

                    usage = data.get("usageMetadata") or {}
                    if usage:
                        tokens_in = usage.get("promptTokenCount", tokens_in)
                        tokens_out = usage.get("candidatesTokenCount", tokens_out)

        except httpx.ConnectError as exc:
            error_msg = f"Google stream connection failed: {exc}"
            raise ProviderUnavailableError(error_msg) from exc
        except httpx.TimeoutException as exc:
            error_msg = f"Google stream timed out: {exc}"
            raise ProviderUnavailableError(error_msg) from exc

        yield LLMStreamChunk(
            is_final=True,
            finish_reason=finish_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            provider="google",
        )

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _convert_messages(
        messages: Sequence[dict[str, str]],
    ) -> tuple[list[dict[str, Any]], str]:
        """Convert OpenAI-style messages to Gemini format.

        Gemini uses ``contents`` with ``role`` as ``"user"`` or ``"model"``
        (not ``"assistant"``). System messages go to ``systemInstruction``.

        Args:
            messages: List of ``{"role": ..., "content": ...}`` dicts.

        Returns:
            Tuple of (contents list, system instruction string).
        """
        system_text = ""
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_text = content
                continue

            gemini_role = "model" if role == "assistant" else "user"
            contents.append(
                {
                    "role": gemini_role,
                    "parts": [{"text": content}],
                }
            )

        return contents, system_text

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        """Extract text content from Gemini response.

        Args:
            data: Parsed JSON response.

        Returns:
            Concatenated text from all candidate parts.
        """
        candidates = data.get("candidates", [])
        if not candidates:
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        texts: list[str] = []
        for part in parts:
            text = part.get("text")
            if text:
                texts.append(text)

        return "".join(texts)
