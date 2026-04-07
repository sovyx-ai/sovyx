"""Sovyx Google provider — Gemini API via httpx.

Supports Gemini 2.0 Flash (default) and Gemini 2.5 Pro.
Context window: 1M tokens for both models.

Ref: SPE-007 §GoogleProvider, Pre-Compute V05-36.
"""

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

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Cost per 1M tokens (USD) — (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash-preview-04-17": (0.15, 0.60),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
}
_DEFAULT_PRICING = (0.10, 0.40)  # fallback to flash pricing

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

        url = f"{_API_BASE}/{model}:generateContent?key={self._api_key}"

        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(url, json=payload)

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
                if not content.strip():
                    error_msg = f"Google returned empty content (model={model})"
                    raise LLMError(error_msg)

                # Extract usage metadata
                usage = data.get("usageMetadata", {})
                tokens_in = usage.get("promptTokenCount", 0)
                tokens_out = usage.get("candidatesTokenCount", 0)

                pricing = _PRICING.get(model, _DEFAULT_PRICING)
                cost = (tokens_in * pricing[0] + tokens_out * pricing[1]) / 1_000_000

                # Extract finish reason
                candidates = data.get("candidates", [])
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
