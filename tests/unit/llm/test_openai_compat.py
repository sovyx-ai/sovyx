"""Tests for the OpenAI-compatible base class and all subclass providers.

Covers:
- ProviderConfig shape
- Base class properties (name, is_available, supports_model, get_context_window)
- generate() with mocked httpx response
- stream() with mocked SSE
- All 7 subclasses (OpenAI, xAI, DeepSeek, Mistral, Together, Groq, Fireworks)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.llm.models import LLMResponse, LLMStreamChunk
from sovyx.llm.providers._openai_compat import OpenAICompatibleProvider, ProviderConfig


def _make_config(**overrides: Any) -> ProviderConfig:  # noqa: ANN401
    defaults = {
        "name": "test",
        "api_url": "https://api.test.ai/v1/chat/completions",
        "api_key_env": "TEST_API_KEY",
        "default_model": "test-model",
        "supported_prefixes": ("test-",),
        "context_windows": {"test-model": 64_000},
        "default_context_window": 64_000,
    }
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestProviderConfig:
    """ProviderConfig shape."""

    def test_frozen(self) -> None:
        cfg = _make_config()
        with pytest.raises(AttributeError):
            cfg.name = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        cfg = _make_config()
        assert cfg.name == "test"
        assert cfg.api_url.startswith("https://")


class TestBaseProperties:
    """OpenAICompatibleProvider property methods."""

    def test_name(self) -> None:
        p = OpenAICompatibleProvider(_make_config(name="myp"), "key")
        assert p.name == "myp"

    def test_is_available(self) -> None:
        assert OpenAICompatibleProvider(_make_config(), "key123").is_available
        assert not OpenAICompatibleProvider(_make_config(), "").is_available

    def test_supports_model(self) -> None:
        p = OpenAICompatibleProvider(_make_config(supported_prefixes=("grok-", "xai-")), "k")
        assert p.supports_model("grok-2")
        assert p.supports_model("xai-large")
        assert not p.supports_model("gpt-4o")

    def test_get_context_window(self) -> None:
        cfg = _make_config(
            context_windows={"m1": 100_000, "m2": 200_000},
            default_context_window=50_000,
        )
        p = OpenAICompatibleProvider(cfg, "k")
        assert p.get_context_window("m1") == 100_000  # noqa: PLR2004
        assert p.get_context_window("m2") == 200_000  # noqa: PLR2004
        assert p.get_context_window("m3") == 50_000  # noqa: PLR2004
        assert p.get_context_window() == 50_000  # noqa: PLR2004


class TestGenerate:
    """OpenAICompatibleProvider.generate() with mocked httpx."""

    @pytest.mark.asyncio()
    async def test_returns_llm_response(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.text = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "Hello!", "role": "assistant"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "test-model",
            }
        )

        provider = OpenAICompatibleProvider(_make_config(), "key")

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
                model="test-model",
            )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello!"
        assert result.provider == "test"
        assert result.tokens_in == 10  # noqa: PLR2004
        assert result.tokens_out == 5  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_unavailable_raises(self) -> None:
        provider = OpenAICompatibleProvider(_make_config(), "")
        with pytest.raises(Exception) as exc_info:
            await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
            )
        assert type(exc_info.value).__name__ == "ProviderUnavailableError"


class TestStream:
    """OpenAICompatibleProvider.stream() with mocked SSE."""

    @pytest.mark.asyncio()
    async def test_yields_chunks(self) -> None:
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}],"model":"m"}',
            "",
            'data: {"choices":[{"delta":{"content":" there"},"finish_reason":"stop"}],"model":"m"}',
            "",
            'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3}}',
            "",
            "data: [DONE]",
            "",
        ]

        mock_resp = AsyncMock()
        mock_resp.status_code = 200

        async def _aiter_lines():  # noqa: ANN202
            for line in sse_lines:
                yield line

        mock_resp.aiter_lines = _aiter_lines
        mock_resp.aread = AsyncMock(return_value=b"")

        provider = OpenAICompatibleProvider(_make_config(), "key")

        # patch client.stream as async context manager
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch.object(provider._client, "stream", return_value=cm):
            chunks: list[LLMStreamChunk] = []
            async for chunk in provider.stream(
                messages=[{"role": "user", "content": "hi"}],
                model="test-model",
            ):
                chunks.append(chunk)

        assert chunks[0].delta_text == "Hi"
        assert chunks[1].delta_text == " there"
        assert chunks[-1].is_final
        assert chunks[-1].tokens_in == 8  # noqa: PLR2004
        assert chunks[-1].tokens_out == 3  # noqa: PLR2004


# ── Subclass instantiation tests ─────────────────────────────────────


_SUBCLASS_SPECS: list[tuple[str, str, str, str]] = [
    ("sovyx.llm.providers.openai", "OpenAIProvider", "openai", "gpt-"),
    ("sovyx.llm.providers.xai", "XAIProvider", "xai", "grok-"),
    ("sovyx.llm.providers.deepseek", "DeepSeekProvider", "deepseek", "deepseek-"),
    ("sovyx.llm.providers.mistral", "MistralProvider", "mistral", "mistral-"),
    ("sovyx.llm.providers.groq", "GroqProvider", "groq", "llama-"),
    ("sovyx.llm.providers.fireworks", "FireworksProvider", "fireworks", "accounts/fireworks/"),
]


class TestSubclassProviders:
    """All OpenAI-compatible subclasses instantiate correctly."""

    @pytest.mark.parametrize(
        ("module_path", "class_name", "expected_name", "model_prefix"),
        _SUBCLASS_SPECS,
        ids=[s[2] for s in _SUBCLASS_SPECS],
    )
    def test_provider_shape(
        self,
        module_path: str,
        class_name: str,
        expected_name: str,
        model_prefix: str,
    ) -> None:
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        provider = cls(api_key="test-key")

        assert provider.name == expected_name
        assert provider.is_available
        assert provider.supports_model(f"{model_prefix}test")
        assert provider.get_context_window() > 0


class TestTogetherSupportsModel:
    """Together uses org/ prefix matching."""

    def test_accepts_org_prefix(self) -> None:
        from sovyx.llm.providers.together import TogetherProvider

        p = TogetherProvider(api_key="k")
        assert p.supports_model("meta-llama/Llama-3.1-70B-Instruct-Turbo")
        assert not p.supports_model("gpt-4o")
        assert not p.supports_model("plain-model")
