"""Tests for GoogleProvider — Gemini API adapter (V05-36)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.pricing import (
    PRICING as _PRICING,
)
from sovyx.llm.pricing import (
    PROVIDER_DEFAULT_PRICING,
)
from sovyx.llm.providers.google import (
    _DEFAULT_CONTEXT_WINDOW,
    GoogleProvider,
)

_DEFAULT_PRICING = PROVIDER_DEFAULT_PRICING["google"]

# ── Helpers ───────────────────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response."""
    import json

    default_body: dict[str, Any] = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello from Gemini!"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 20,
        },
    }
    data = body if body is not None else default_body
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(data).encode(),
        headers={"content-type": "application/json"},
    )


# ── Init & Properties ────────────────────────────────────────────────


class TestInit:
    """Tests for GoogleProvider initialization."""

    def test_name(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        assert provider.name == "google"

    def test_available_with_key(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        assert provider.is_available is True

    def test_not_available_without_key(self) -> None:
        provider = GoogleProvider(api_key="")
        assert provider.is_available is False

    def test_supports_gemini(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.supports_model("gemini-2.0-flash") is True
        assert provider.supports_model("gemini-2.5-pro-preview-03-25") is True

    def test_does_not_support_claude(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.supports_model("claude-sonnet-4-20250514") is False
        assert provider.supports_model("gpt-4o") is False

    def test_context_window_default(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.get_context_window() == _DEFAULT_CONTEXT_WINDOW

    def test_context_window_specific_model(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.get_context_window("gemini-2.0-flash") == 1_048_576

    def test_context_window_unknown_model(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.get_context_window("gemini-99") == _DEFAULT_CONTEXT_WINDOW

    def test_context_window_none(self) -> None:
        provider = GoogleProvider(api_key="k")
        assert provider.get_context_window(None) == _DEFAULT_CONTEXT_WINDOW


# ── Generate — Happy Path ────────────────────────────────────────────


class TestGenerateSuccess:
    """Tests for successful generation."""

    @pytest.mark.asyncio()
    async def test_basic_generation(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())

        result = await provider.generate(
            messages=[{"role": "user", "content": "Hello"}],
        )
        assert result.content == "Hello from Gemini!"
        assert result.provider == "google"
        assert result.tokens_in == 10  # noqa: PLR2004
        assert result.tokens_out == 20  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_with_system_message(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())

        result = await provider.generate(
            messages=[
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
        )
        assert result.content == "Hello from Gemini!"

        # Verify system instruction was sent
        call_kwargs = provider._client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "systemInstruction" in payload

    @pytest.mark.asyncio()
    async def test_assistant_role_mapped_to_model(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())

        await provider.generate(
            messages=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
                {"role": "user", "content": "How are you?"},
            ],
        )
        call_kwargs = provider._client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        roles = [c["role"] for c in payload["contents"]]
        assert roles == ["user", "model", "user"]

    @pytest.mark.asyncio()
    async def test_cost_calculation(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())

        result = await provider.generate(
            messages=[{"role": "user", "content": "Hello"}],
            model="gemini-2.0-flash",
        )
        pricing = _PRICING["gemini-2.0-flash"]
        expected_cost = (10 * pricing[0] + 20 * pricing[1]) / 1_000_000
        assert abs(result.cost_usd - expected_cost) < 1e-10

    @pytest.mark.asyncio()
    async def test_unknown_model_uses_default_pricing(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())

        result = await provider.generate(
            messages=[{"role": "user", "content": "Hello"}],
            model="gemini-99-turbo",
        )
        expected_cost = (10 * _DEFAULT_PRICING[0] + 20 * _DEFAULT_PRICING[1]) / 1_000_000
        assert abs(result.cost_usd - expected_cost) < 1e-10

    @pytest.mark.asyncio()
    async def test_finish_reason_extracted(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "done"}], "role": "model"},
                    "finishReason": "MAX_TOKENS",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 10},
        }
        provider._client.post = AsyncMock(return_value=_make_response(body=body))
        result = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
        )
        assert result.finish_reason == "max_tokens"

    @pytest.mark.asyncio()
    async def test_multi_part_content(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Hello "}, {"text": "World"}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 10},
        }
        provider._client.post = AsyncMock(return_value=_make_response(body=body))
        result = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
        )
        assert result.content == "Hello World"

    @pytest.mark.asyncio()
    async def test_latency_positive(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(return_value=_make_response())
        result = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
        )
        assert result.latency_ms >= 0


# ── Generate — Error Handling ─────────────────────────────────────────


class TestGenerateErrors:
    """Tests for error handling during generation."""

    @pytest.mark.asyncio()
    async def test_no_api_key_raises(self) -> None:
        provider = GoogleProvider(api_key="")
        with pytest.raises(ProviderUnavailableError, match="not configured"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_4xx_raises_llm_error(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(
            return_value=_make_response(400, {"error": {"message": "bad request"}}),
        )
        with pytest.raises(LLMError, match="Google API error 400"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_empty_content_raises(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": ""}], "role": "model"},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 0},
        }
        provider._client.post = AsyncMock(return_value=_make_response(body=body))
        with pytest.raises(LLMError, match="empty content"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_no_candidates_empty(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        body = {
            "candidates": [],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 0},
        }
        provider._client.post = AsyncMock(return_value=_make_response(body=body))
        with pytest.raises(LLMError, match="empty content"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_connection_error_raises(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused"),
        )
        with pytest.raises(ProviderUnavailableError, match="connection failed"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_timeout_retries_then_raises(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(
            side_effect=httpx.ReadTimeout("timeout"),
        )
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_500_retries_then_raises(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        provider._client.post = AsyncMock(
            return_value=_make_response(500, {"error": {"message": "server error"}}),
        )
        with pytest.raises(LLMError, match="Google API error 500"):
            await provider.generate(
                messages=[{"role": "user", "content": "Hi"}],
            )

    @pytest.mark.asyncio()
    async def test_429_retries(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        # First two calls return 429, third succeeds
        provider._client.post = AsyncMock(
            side_effect=[
                _make_response(429, {"error": {"message": "rate limited"}}),
                _make_response(429, {"error": {"message": "rate limited"}}),
                _make_response(),
            ],
        )
        result = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
        )
        assert result.content == "Hello from Gemini!"


# ── Close ─────────────────────────────────────────────────────────────


class TestClose:
    """Tests for resource cleanup."""

    @pytest.mark.asyncio()
    async def test_close(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        provider._client = AsyncMock()
        await provider.close()
        provider._client.aclose.assert_awaited_once()


# ── Message Conversion ────────────────────────────────────────────────


class TestConvertMessages:
    """Tests for _convert_messages helper."""

    def test_user_only(self) -> None:
        contents, system = GoogleProvider._convert_messages(
            [
                {"role": "user", "content": "Hello"},
            ]
        )
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert system == ""

    def test_system_extracted(self) -> None:
        contents, system = GoogleProvider._convert_messages(
            [
                {"role": "system", "content": "Be helpful"},
                {"role": "user", "content": "Hello"},
            ]
        )
        assert system == "Be helpful"
        assert len(contents) == 1

    def test_assistant_becomes_model(self) -> None:
        contents, _ = GoogleProvider._convert_messages(
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]
        )
        assert contents[1]["role"] == "model"

    def test_empty_messages(self) -> None:
        contents, system = GoogleProvider._convert_messages([])
        assert contents == []
        assert system == ""


# ── Content Extraction ────────────────────────────────────────────────


class TestExtractContent:
    """Tests for _extract_content helper."""

    def test_single_part(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Hello"}]},
                }
            ],
        }
        assert GoogleProvider._extract_content(data) == "Hello"

    def test_multiple_parts(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "A"}, {"text": "B"}]},
                }
            ],
        }
        assert GoogleProvider._extract_content(data) == "AB"

    def test_no_candidates(self) -> None:
        assert GoogleProvider._extract_content({"candidates": []}) == ""

    def test_no_parts(self) -> None:
        data = {"candidates": [{"content": {"parts": []}}]}
        assert GoogleProvider._extract_content(data) == ""

    def test_non_text_parts_skipped(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "ok"}, {"inline_data": "..."}]},
                }
            ],
        }
        assert GoogleProvider._extract_content(data) == "ok"
