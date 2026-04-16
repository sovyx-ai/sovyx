"""Sovyx Groq provider — Groq API (OpenAI-compatible)."""

from __future__ import annotations

from sovyx.llm.providers._openai_compat import OpenAICompatibleProvider, ProviderConfig

_GROQ_CONFIG = ProviderConfig(
    name="groq",
    api_url="https://api.groq.com/openai/v1/chat/completions",
    api_key_env="GROQ_API_KEY",
    default_model="llama-3.1-70b-versatile",
    supported_prefixes=("llama-", "mixtral-", "gemma-"),
    context_windows={
        "llama-3.1-70b-versatile": 131_072,
        "llama-3.1-8b-instant": 131_072,
        "mixtral-8x7b-32768": 32_768,
        "gemma2-9b-it": 8_192,
    },
    default_context_window=131_072,
)


class GroqProvider(OpenAICompatibleProvider):
    """Groq inference provider."""

    def __init__(self, api_key: str) -> None:
        super().__init__(_GROQ_CONFIG, api_key)
