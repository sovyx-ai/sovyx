"""Single source of truth for LLM model pricing.

One table used by every provider (`llm/providers/{anthropic,openai,google}.py`)
and the router (`llm/router.py`). Values are **USD per 1 million tokens**,
in `(input, output)` order.

Updating a model's rate means editing exactly one line below — the old
pattern of duplicating the same numbers in four files was responsible for
silent drift (e.g., `router.py` was missing `gemini-2.5-flash-preview-04-17`
while `providers/google.py` had it).

Last validated: 2026-04-16
Sources:
    - https://platform.claude.com/docs/en/docs/about-claude/pricing
    - https://openai.com/api/pricing/
    - https://ai.google.dev/pricing
    - https://docs.x.ai/docs/models
    - https://api-docs.deepseek.com/quick_start/pricing
    - https://docs.mistral.ai/getting-started/pricing/
    - https://www.together.ai/pricing
    - https://groq.com/pricing/
    - https://fireworks.ai/pricing
"""

from __future__ import annotations

# ── Per-model pricing (USD per 1M tokens) ──────────────────────────────
#
# Keep sorted within each provider block.

PRICING: dict[str, tuple[float, float]] = {
    # ── Anthropic (validated 2026-04-16) ──
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-5-20250514": (3.0, 15.0),
    "claude-sonnet-4-6-20250827": (3.0, 15.0),
    "claude-opus-4-5-20250918": (5.0, 25.0),
    "claude-opus-4-6-20250918": (5.0, 25.0),
    "claude-opus-4-7-20260401": (5.0, 25.0),
    # ── OpenAI (validated 2026-04-16) ──
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o1": (15.0, 60.0),
    "o3": (2.0, 8.0),
    "o3-mini": (1.1, 4.4),
    "o4-mini": (1.1, 4.4),
    # ── Google (validated 2026-04-16) ──
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-preview-04-17": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    # ── xAI / Grok (validated 2026-04-16) ──
    "grok-4": (2.0, 6.0),
    "grok-4.20-0309": (2.0, 6.0),
    "grok-4-1-fast": (0.20, 0.50),
    "grok-3": (3.0, 15.0),
    "grok-2": (2.0, 10.0),
    # ── DeepSeek (validated 2026-04-16, V3.2 unified pricing) ──
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.28, 0.42),
    # ── Mistral ──
    "mistral-large-latest": (2.0, 6.0),
    "mistral-small-latest": (0.10, 0.30),
    # ── Together AI ──
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": (0.18, 0.18),
    # ── Groq (validated 2026-04-16) ──
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "qwen-3-32b": (0.29, 0.59),
    # ── Fireworks (parameter-tier pricing) ──
    "accounts/fireworks/models/llama-v3p3-70b-instruct": (0.90, 0.90),
    "accounts/fireworks/models/llama-v3p1-70b-instruct": (0.90, 0.90),
    "accounts/fireworks/models/llama-v3p1-8b-instruct": (0.20, 0.20),
    # ── Legacy (kept for backward compat, may be removed) ──
    "claude-3-5-haiku-20241022": (0.80, 4.0),
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768": (0.24, 0.24),
}

# Conservative fallback (Sonnet-class) when the model is unknown and the
# caller hasn't supplied a provider-specific default.
DEFAULT_PRICING: tuple[float, float] = (3.0, 15.0)

# Per-provider fallbacks preserve the old per-file defaults so a missing
# model doesn't cross-contaminate cost estimates between providers.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (2.5, 10.0),
    "google": (0.30, 2.50),
    "ollama": (0.0, 0.0),  # local inference — free
    "xai": (2.0, 6.0),
    "deepseek": (0.28, 0.42),
    "mistral": (2.0, 6.0),
    "together": (0.88, 0.88),
    "groq": (0.59, 0.79),
    "fireworks": (0.90, 0.90),
}


def get_pricing(
    model: str | None,
    *,
    fallback: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Return ``(input_per_1m, output_per_1m)`` pricing in USD.

    Args:
        model: The model identifier to look up.
        fallback: Price to use when the model is not in the table. Callers
            with a provider context should pass ``PROVIDER_DEFAULT_PRICING[name]``
            so an unknown model doesn't silently cost-estimate at another
            provider's rate.

    Returns:
        ``(input, output)`` rate per 1M tokens.
    """
    if model is not None and model in PRICING:
        return PRICING[model]
    return fallback if fallback is not None else DEFAULT_PRICING


def compute_cost(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    fallback: tuple[float, float] | None = None,
) -> float:
    """Estimate the USD cost of a single call given its token counts.

    Args:
        model: Model identifier, or ``None`` if unknown.
        tokens_in: Input tokens consumed.
        tokens_out: Output tokens produced.
        fallback: See :func:`get_pricing`.

    Returns:
        Estimated cost in USD.
    """
    price_in, price_out = get_pricing(model, fallback=fallback)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
