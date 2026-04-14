"""Single source of truth for LLM model pricing.

One table used by every provider (`llm/providers/{anthropic,openai,google}.py`)
and the router (`llm/router.py`). Values are **USD per 1 million tokens**,
in `(input, output)` order.

Updating a model's rate means editing exactly one line below — the old
pattern of duplicating the same numbers in four files was responsible for
silent drift (e.g., `router.py` was missing `gemini-2.5-flash-preview-04-17`
while `providers/google.py` had it).

References:
    - https://www.anthropic.com/pricing
    - https://openai.com/api/pricing/
    - https://ai.google.dev/pricing
"""

from __future__ import annotations

# ── Per-model pricing (USD per 1M tokens) ──────────────────────────────
#
# Keep sorted within each provider block. Add a comment next to each entry
# only when the price is unusual (e.g., cached input, tiered output).

PRICING: dict[str, tuple[float, float]] = {
    # ── Anthropic ──
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (1.0, 5.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    # ── OpenAI ──
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o1": (15.0, 60.0),
    "o3-mini": (1.1, 4.4),
    # ── Google ──
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash-preview-04-17": (0.15, 0.60),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
}

# Conservative fallback (Sonnet-class) when the model is unknown and the
# caller hasn't supplied a provider-specific default.
DEFAULT_PRICING: tuple[float, float] = (3.0, 15.0)

# Per-provider fallbacks preserve the old per-file defaults so a missing
# model doesn't cross-contaminate cost estimates between providers.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (5.0, 15.0),
    "google": (0.10, 0.40),
    "ollama": (0.0, 0.0),  # local inference — free
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
