"""Sovyx OpenAI provider — GPT API via the shared OpenAI-compatible base."""

from __future__ import annotations

from sovyx.llm.providers._openai_compat import OpenAICompatibleProvider, ProviderConfig

_OPENAI_CONFIG = ProviderConfig(
    name="openai",
    api_url="https://api.openai.com/v1/chat/completions",
    api_key_env="OPENAI_API_KEY",
    default_model="gpt-4o",
    supported_prefixes=("gpt-", "o1", "o3"),
    context_windows={
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "o1": 200_000,
        "o3-mini": 200_000,
    },
    default_context_window=128_000,
)


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI GPT provider."""

    def __init__(self, api_key: str) -> None:
        super().__init__(_OPENAI_CONFIG, api_key)
