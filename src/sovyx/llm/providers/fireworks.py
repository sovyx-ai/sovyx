"""Sovyx Fireworks AI provider — Fireworks API (OpenAI-compatible)."""

from __future__ import annotations

from sovyx.llm.providers._openai_compat import OpenAICompatibleProvider, ProviderConfig

_FIREWORKS_CONFIG = ProviderConfig(
    name="fireworks",
    api_url="https://api.fireworks.ai/inference/v1/chat/completions",
    api_key_env="FIREWORKS_API_KEY",
    default_model="accounts/fireworks/models/llama-v3p1-70b-instruct",
    supported_prefixes=("accounts/fireworks/",),
    context_windows={
        "accounts/fireworks/models/llama-v3p1-70b-instruct": 131_072,
        "accounts/fireworks/models/llama-v3p1-8b-instruct": 131_072,
    },
    default_context_window=131_072,
)


class FireworksProvider(OpenAICompatibleProvider):
    """Fireworks AI provider."""

    def __init__(self, api_key: str) -> None:
        super().__init__(_FIREWORKS_CONFIG, api_key)
