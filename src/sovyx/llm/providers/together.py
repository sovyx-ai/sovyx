"""Sovyx Together AI provider — Together API (OpenAI-compatible)."""

from __future__ import annotations

from sovyx.llm.providers._openai_compat import OpenAICompatibleProvider, ProviderConfig

_TOGETHER_CONFIG = ProviderConfig(
    name="together",
    api_url="https://api.together.xyz/v1/chat/completions",
    api_key_env="TOGETHER_API_KEY",
    default_model="meta-llama/Llama-3.1-70B-Instruct-Turbo",
    supported_prefixes=("meta-llama/", "mistralai/", "Qwen/", "google/"),
    context_windows={
        "meta-llama/Llama-3.1-70B-Instruct-Turbo": 131_072,
        "meta-llama/Llama-3.1-8B-Instruct-Turbo": 131_072,
    },
    default_context_window=131_072,
)


class TogetherProvider(OpenAICompatibleProvider):
    """Together AI provider."""

    def __init__(self, api_key: str) -> None:
        super().__init__(_TOGETHER_CONFIG, api_key)

    def supports_model(self, model: str) -> bool:
        """Together serves open-source models with org/ prefixes."""
        if "/" in model:
            return model.startswith(self._config.supported_prefixes)
        return False
