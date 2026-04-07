"""VAL-08: Coverage gaps for LLM providers (anthropic, openai, ollama).

Covers:
- Empty content response → LLMError
- ConnectError → ProviderUnavailableError (anthropic)
- Retry exhaustion with last_error re-raise
- Ollama/OpenAI default context window for unknown models
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sovyx.engine.errors import LLMError, ProviderUnavailableError

# ── Anthropic ──


class TestAnthropicGaps:
    @pytest.mark.asyncio()
    async def test_empty_content_raises(self) -> None:
        """Anthropic response with empty content blocks raises LLMError."""
        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        # Response: valid JSON but empty content
        mock_resp = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": ""}],
                "model": "claude-sonnet-4-20250514",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "stop_reason": "end_turn",
            },
            request=httpx.Request("POST", "https://api.anthropic.com"),
        )

        with (
            patch.object(provider._client, "post", new_callable=AsyncMock, return_value=mock_resp),
            pytest.raises(LLMError, match="empty content"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()

    @pytest.mark.asyncio()
    async def test_empty_content_whitespace_only(self) -> None:
        """Whitespace-only content is treated as empty."""
        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        mock_resp = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "   \n  "}],
                "model": "claude-sonnet-4-20250514",
                "usage": {"input_tokens": 10, "output_tokens": 1},
                "stop_reason": "end_turn",
            },
            request=httpx.Request("POST", "https://api.anthropic.com"),
        )

        with (
            patch.object(provider._client, "post", new_callable=AsyncMock, return_value=mock_resp),
            pytest.raises(LLMError, match="empty content"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()

    @pytest.mark.asyncio()
    async def test_connect_error_raises_unavailable(self) -> None:
        """ConnectError raises ProviderUnavailableError."""
        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        with (
            patch.object(
                provider._client,
                "post",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("DNS resolution failed"),
            ),
            pytest.raises(ProviderUnavailableError, match="connection failed"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()

    @pytest.mark.asyncio()
    async def test_retry_exhaustion_429(self) -> None:
        """After max retries on 429, raises LLMError with last error."""
        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        mock_resp = httpx.Response(
            429,
            text="rate limited",
            request=httpx.Request("POST", "https://api.anthropic.com"),
        )

        with (
            patch.object(provider._client, "post", new_callable=AsyncMock, return_value=mock_resp),
            patch("sovyx.llm.providers.anthropic.retry_delay", return_value=0.0),
            pytest.raises(LLMError, match="429"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()


# ── OpenAI ──


class TestOpenAIGaps:
    @pytest.mark.asyncio()
    async def test_empty_content_raises(self) -> None:
        """OpenAI response with empty content raises LLMError."""
        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")

        mock_resp = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
            request=httpx.Request("POST", "https://api.openai.com"),
        )

        with (
            patch.object(provider._client, "post", new_callable=AsyncMock, return_value=mock_resp),
            pytest.raises(LLMError, match="empty content"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()

    def test_default_context_window(self) -> None:
        """Unknown model returns default 128K context window."""
        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")
        assert provider.get_context_window("gpt-future-model") == 128_000


# ── Ollama ──


class TestOllamaGaps:
    @pytest.mark.asyncio()
    async def test_empty_content_raises(self) -> None:
        """Ollama response with empty content raises LLMError."""
        from sovyx.llm.providers.ollama import OllamaProvider

        provider = OllamaProvider()

        mock_resp = httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": ""},
                "model": "llama3.2",
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 0,
                "total_duration": 1000000,
            },
            request=httpx.Request("POST", "http://localhost:11434"),
        )

        with (
            patch.object(provider._client, "post", new_callable=AsyncMock, return_value=mock_resp),
            pytest.raises(LLMError, match="empty content"),
        ):
            await provider.generate(
                messages=[{"role": "user", "content": "hello"}],
            )

        await provider.close()

    def test_default_context_window(self) -> None:
        """Unknown model returns default context window."""
        from sovyx.llm.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        # Default is the _DEFAULT_CONTEXT value
        ctx = provider.get_context_window("unknown-model-xyz")
        assert ctx > 0


class TestOllamaOpenAICoverageGaps:
    """Cover remaining ollama + openai paths."""

    def test_ollama_context_window_default(self) -> None:
        """get_context_window without model returns default."""
        from sovyx.llm.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        window = provider.get_context_window()
        assert window > 0

    def test_openai_context_window_default(self) -> None:
        """get_context_window without model returns 128k."""
        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="sk-test")
        window = provider.get_context_window()
        assert window == 128_000  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_ollama_unexpected_error_at_end_of_retries(self) -> None:
        """When all retries fail with non-timeout error, raises LLMError."""
        import httpx

        from sovyx.llm.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        with (
            patch.object(
                provider._client,  # noqa: SLF001
                "post",
                side_effect=httpx.ConnectError("refused"),
            ),
            pytest.raises(LLMError),
        ):
            await provider.generate(
                [{"role": "user", "content": "hi"}],
                model="llama3",
            )
        await provider.close()

    @pytest.mark.asyncio()
    async def test_openai_unexpected_error_at_end_of_retries(self) -> None:
        """When all retries fail with non-timeout error, raises LLMError."""
        import httpx

        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="sk-test")
        with (
            patch.object(
                provider._client,  # noqa: SLF001
                "post",
                side_effect=httpx.ConnectError("refused"),
            ),
            pytest.raises(LLMError),
        ):
            await provider.generate(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o",
            )
        await provider.close()
