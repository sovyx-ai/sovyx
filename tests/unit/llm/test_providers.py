"""Tests for sovyx.llm.providers — Anthropic, OpenAI, Ollama."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from sovyx.engine.errors import LLMError, ProviderUnavailableError
from sovyx.llm.models import LLMResponse
from sovyx.llm.providers.anthropic import AnthropicProvider
from sovyx.llm.providers.ollama import OllamaProvider
from sovyx.llm.providers.openai import OpenAIProvider

# ── Anthropic ──


class TestAnthropicProvider:
    """Anthropic provider tests."""

    def test_name(self) -> None:
        p = AnthropicProvider("sk-test")
        assert p.name == "anthropic"

    def test_is_available(self) -> None:
        assert AnthropicProvider("sk-test").is_available is True
        assert AnthropicProvider("").is_available is False

    def test_supports_model(self) -> None:
        p = AnthropicProvider("sk-test")
        assert p.supports_model("claude-sonnet-4-20250514") is True
        assert p.supports_model("gpt-4o") is False

    def test_context_window(self) -> None:
        p = AnthropicProvider("sk-test")
        assert p.get_context_window() == 200_000

    async def test_generate_success(self) -> None:
        p = AnthropicProvider("sk-test")
        mock_resp = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Hello!"}],
                "model": "claude-sonnet-4-20250514",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            },
        )
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        result = await p.generate(
            [{"role": "user", "content": "Hi"}],
            model="claude-sonnet-4-20250514",
        )
        assert isinstance(result, LLMResponse)
        assert result.content == "Hello!"
        assert result.provider == "anthropic"
        assert result.tokens_in == 10
        assert result.cost_usd > 0
        await p.close()

    async def test_generate_with_system(self) -> None:
        p = AnthropicProvider("sk-test")
        mock_resp = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Hi"}],
                "model": "claude-sonnet-4-20250514",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "stop_reason": "end_turn",
            },
        )
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        await p.generate(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ]
        )
        call_args = p._client.post.call_args
        payload = call_args.kwargs.get("json", call_args[1].get("json", {}))
        assert "system" in payload
        await p.close()

    async def test_unavailable_without_key(self) -> None:
        p = AnthropicProvider("")
        with pytest.raises(ProviderUnavailableError):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_api_error(self) -> None:
        p = AnthropicProvider("sk-test")
        mock_resp = httpx.Response(400, json={"error": "bad request"})
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        with pytest.raises(LLMError, match="400"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_retry_on_429(self) -> None:
        p = AnthropicProvider("sk-test")
        fail_resp = httpx.Response(429, json={"error": "rate limited"})
        ok_resp = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "OK"}],
                "model": "claude-sonnet-4-20250514",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            },
        )
        p._client.post = AsyncMock(side_effect=[fail_resp, ok_resp])  # type: ignore[method-assign]

        result = await p.generate([{"role": "user", "content": "Hi"}])
        assert result.content == "OK"
        assert p._client.post.call_count == 2  # noqa: PLR2004
        await p.close()

    async def test_timeout_raises(self) -> None:
        p = AnthropicProvider("sk-test")
        p._client.post = AsyncMock(  # type: ignore[method-assign]
            side_effect=httpx.TimeoutException("timeout")
        )

        with pytest.raises(ProviderUnavailableError, match="timed out"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()


# ── OpenAI ──


class TestOpenAIProvider:
    """OpenAI provider tests."""

    def test_name(self) -> None:
        assert OpenAIProvider("sk-test").name == "openai"

    def test_supports_model(self) -> None:
        p = OpenAIProvider("sk-test")
        assert p.supports_model("gpt-4o") is True
        assert p.supports_model("o1") is True
        assert p.supports_model("o3-mini") is True
        assert p.supports_model("claude-sonnet") is False

    def test_context_window(self) -> None:
        p = OpenAIProvider("sk-test")
        assert p.get_context_window("gpt-4o") == 128_000

    async def test_generate_success(self) -> None:
        p = OpenAIProvider("sk-test")
        mock_resp = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "Hello!"},
                        "finish_reason": "stop",
                    }
                ],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        result = await p.generate([{"role": "user", "content": "Hi"}], model="gpt-4o")
        assert result.content == "Hello!"
        assert result.provider == "openai"
        await p.close()

    async def test_unavailable_without_key(self) -> None:
        p = OpenAIProvider("")
        with pytest.raises(ProviderUnavailableError):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_api_error(self) -> None:
        p = OpenAIProvider("sk-test")
        mock_resp = httpx.Response(400, json={"error": "bad"})
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        with pytest.raises(LLMError, match="400"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_retry_on_500(self) -> None:
        p = OpenAIProvider("sk-test")
        fail_resp = httpx.Response(500, json={"error": "server"})
        ok_resp = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
        p._client.post = AsyncMock(side_effect=[fail_resp, ok_resp])  # type: ignore[method-assign]
        result = await p.generate([{"role": "user", "content": "Hi"}])
        assert result.content == "OK"
        await p.close()

    async def test_timeout_raises(self) -> None:
        p = OpenAIProvider("sk-test")
        p._client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))  # type: ignore[method-assign]
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_connection_error(self) -> None:
        p = OpenAIProvider("sk-test")
        p._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))  # type: ignore[method-assign]
        with pytest.raises(ProviderUnavailableError, match="connection failed"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_retry_exhausted(self) -> None:
        p = OpenAIProvider("sk-test")
        fail_resp = httpx.Response(500, json={"error": "server"})
        p._client.post = AsyncMock(return_value=fail_resp)  # type: ignore[method-assign]
        with pytest.raises(LLMError):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()


# ── Ollama ──


class TestOllamaProvider:
    """Ollama provider tests."""

    def test_name(self) -> None:
        assert OllamaProvider().name == "ollama"

    def test_not_available_before_ping(self) -> None:
        """is_available defaults False — must call ping() first."""
        assert OllamaProvider().is_available is False

    def test_base_url_default(self) -> None:
        p = OllamaProvider()
        assert p.base_url == "http://localhost:11434"

    def test_base_url_explicit(self) -> None:
        p = OllamaProvider(base_url="http://gpu-box:11434/")
        assert p.base_url == "http://gpu-box:11434"  # trailing slash stripped

    def test_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OLLAMA_HOST env var is respected (same as official Ollama CLI)."""
        monkeypatch.setenv("OLLAMA_HOST", "http://remote:9999")
        p = OllamaProvider()
        assert p.base_url == "http://remote:9999"

    def test_base_url_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to localhost when OLLAMA_HOST is not set."""
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        p = OllamaProvider()
        assert p.base_url == "http://localhost:11434"

    def test_explicit_url_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit base_url takes priority over OLLAMA_HOST."""
        monkeypatch.setenv("OLLAMA_HOST", "http://remote:9999")
        p = OllamaProvider(base_url="http://explicit:7777")
        assert p.base_url == "http://explicit:7777"

    async def test_ping_success(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(200, json={"models": []})
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        assert await p.ping() is True
        assert p.is_available is True
        await p.close()

    async def test_ping_failure_connection(self) -> None:
        p = OllamaProvider()
        p._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))  # type: ignore[method-assign]
        assert await p.ping() is False
        assert p.is_available is False
        await p.close()

    async def test_ping_failure_timeout(self) -> None:
        p = OllamaProvider()
        p._client.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))  # type: ignore[method-assign]
        assert await p.ping() is False
        assert p.is_available is False
        await p.close()

    async def test_ping_unexpected_status(self) -> None:
        """Non-200 response sets _verified to False."""
        p = OllamaProvider()
        mock_resp = httpx.Response(500, text="error")
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        assert await p.ping() is False
        assert p.is_available is False
        await p.close()

    async def test_ping_sets_verified_flag(self) -> None:
        """Verified flag transitions: False → True → False on error."""
        p = OllamaProvider()
        assert p._verified is False

        ok_resp = httpx.Response(200, json={"models": []})
        p._client.get = AsyncMock(return_value=ok_resp)  # type: ignore[method-assign]
        await p.ping()
        assert p._verified is True

        p._client.get = AsyncMock(side_effect=httpx.ConnectError("down"))  # type: ignore[method-assign]
        await p.ping()
        assert p._verified is False
        await p.close()

    async def test_list_models_success(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:latest", "size": 4_000_000_000},
                    {"name": "mistral:latest", "size": 3_500_000_000},
                    {"name": "codellama:7b", "size": 3_800_000_000},
                ],
            },
        )
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        models = await p.list_models()
        assert models == ["codellama:7b", "llama3.1:latest", "mistral:latest"]  # sorted
        await p.close()

    async def test_list_models_empty(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(200, json={"models": []})
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        assert await p.list_models() == []
        await p.close()

    async def test_list_models_error(self) -> None:
        p = OllamaProvider()
        p._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))  # type: ignore[method-assign]
        assert await p.list_models() == []
        await p.close()

    async def test_list_models_bad_status(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(500, text="error")
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        assert await p.list_models() == []
        await p.close()

    async def test_list_models_malformed_json(self) -> None:
        """Handles models without 'name' field gracefully."""
        p = OllamaProvider()
        mock_resp = httpx.Response(
            200,
            json={"models": [{"size": 100}, {"name": "ok:latest"}]},
        )
        p._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        assert await p.list_models() == ["ok:latest"]
        await p.close()

    def test_supports_any_model(self) -> None:
        p = OllamaProvider()
        assert p.supports_model("llama3.2:1b") is True
        assert p.supports_model("anything") is True

    def test_rejects_cloud_models(self) -> None:
        p = OllamaProvider()
        assert p.supports_model("claude-3-opus") is False
        assert p.supports_model("gpt-4o") is False
        assert p.supports_model("gemini-pro") is False
        assert p.supports_model("o1-preview") is False
        assert p.supports_model("o3-mini") is False

    def test_context_window(self) -> None:
        p = OllamaProvider()
        assert p.get_context_window("llama3.2:1b") == 8_192

    async def test_generate_success(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(
            200,
            json={
                "message": {"content": "Hello!"},
                "model": "llama3.2:1b",
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        result = await p.generate([{"role": "user", "content": "Hi"}], model="llama3.2:1b")
        assert result.content == "Hello!"
        assert result.provider == "ollama"
        assert result.cost_usd == 0.0
        await p.close()

    async def test_connection_error(self) -> None:
        p = OllamaProvider()
        p._client.post = AsyncMock(  # type: ignore[method-assign]
            side_effect=httpx.ConnectError("refused")
        )

        with pytest.raises(ProviderUnavailableError, match="not reachable"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_api_error(self) -> None:
        p = OllamaProvider()
        mock_resp = httpx.Response(400, json={"error": "bad model"})
        p._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        with pytest.raises(LLMError, match="400"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_retry_on_500(self) -> None:
        p = OllamaProvider()
        fail_resp = httpx.Response(500, json={"error": "server"})
        ok_resp = httpx.Response(
            200,
            json={
                "message": {"content": "OK"},
                "model": "llama3.2:1b",
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )
        p._client.post = AsyncMock(side_effect=[fail_resp, ok_resp])  # type: ignore[method-assign]
        result = await p.generate([{"role": "user", "content": "Hi"}])
        assert result.content == "OK"
        await p.close()

    async def test_timeout_raises(self) -> None:
        p = OllamaProvider()
        p._client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))  # type: ignore[method-assign]
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()

    async def test_retry_exhausted(self) -> None:
        p = OllamaProvider()
        fail_resp = httpx.Response(500, json={"error": "server"})
        p._client.post = AsyncMock(return_value=fail_resp)  # type: ignore[method-assign]
        with pytest.raises(LLMError):
            await p.generate([{"role": "user", "content": "Hi"}])
        await p.close()


# ── Protocol compliance ──


class TestProtocolCompliance:
    """All providers satisfy LLMProvider protocol."""

    def test_anthropic_is_provider(self) -> None:
        from sovyx.engine.protocols import LLMProvider

        assert isinstance(AnthropicProvider("key"), LLMProvider)

    def test_openai_is_provider(self) -> None:
        from sovyx.engine.protocols import LLMProvider

        assert isinstance(OpenAIProvider("key"), LLMProvider)

    def test_ollama_is_provider(self) -> None:
        from sovyx.engine.protocols import LLMProvider

        assert isinstance(OllamaProvider(), LLMProvider)
