"""Tests for the unified LLM pricing module (sovyx.llm.pricing).

Every provider + the router delegates cost computation to this module,
so it's the single place the pricing table can drift — the tests below
pin the public contract.
"""

from __future__ import annotations

import pytest

from sovyx.llm.pricing import (
    DEFAULT_PRICING,
    PRICING,
    PROVIDER_DEFAULT_PRICING,
    compute_cost,
    get_pricing,
)


class TestPricingTable:
    def test_every_provider_default_is_a_pair(self) -> None:
        for provider, pair in PROVIDER_DEFAULT_PRICING.items():
            assert len(pair) == 2, provider
            assert all(isinstance(v, (int, float)) for v in pair), provider

    def test_pricing_values_are_positive_or_zero(self) -> None:
        for model, (price_in, price_out) in PRICING.items():
            assert price_in >= 0, f"{model} input pricing negative"
            assert price_out >= 0, f"{model} output pricing negative"

    def test_default_pricing_is_sonnet_class(self) -> None:
        # DEFAULT_PRICING is the "conservative fallback" — Sonnet rates.
        assert DEFAULT_PRICING == (3.0, 15.0)


class TestGetPricing:
    def test_known_model_returns_exact_rate(self) -> None:
        assert get_pricing("gpt-4o") == PRICING["gpt-4o"]
        assert get_pricing("claude-sonnet-4-20250514") == PRICING["claude-sonnet-4-20250514"]

    def test_unknown_model_returns_default(self) -> None:
        assert get_pricing("vaporware-model-9999") == DEFAULT_PRICING

    def test_none_returns_default(self) -> None:
        assert get_pricing(None) == DEFAULT_PRICING

    def test_fallback_overrides_default(self) -> None:
        custom = (0.0, 0.0)
        assert get_pricing("vaporware", fallback=custom) == custom

    def test_fallback_ignored_for_known_model(self) -> None:
        # Fallback only kicks in on miss.
        custom = (999.0, 999.0)
        assert get_pricing("gpt-4o", fallback=custom) == PRICING["gpt-4o"]

    @pytest.mark.parametrize("provider", ["anthropic", "openai", "google", "ollama"])
    def test_every_provider_has_a_default(self, provider: str) -> None:
        assert provider in PROVIDER_DEFAULT_PRICING


class TestComputeCost:
    def test_cost_matches_manual_calculation(self) -> None:
        tokens_in = 1_000_000
        tokens_out = 500_000
        price_in, price_out = PRICING["gpt-4o-mini"]
        expected = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        assert compute_cost("gpt-4o-mini", tokens_in, tokens_out) == expected

    def test_zero_tokens_zero_cost(self) -> None:
        assert compute_cost("gpt-4o", 0, 0) == 0.0

    def test_ollama_provider_default_is_free(self) -> None:
        # Unknown-to-table model routed through the ollama fallback
        # should land on (0.0, 0.0) — local inference is free.
        fallback = PROVIDER_DEFAULT_PRICING["ollama"]
        assert compute_cost("llama3.1-8b", 10_000, 10_000, fallback=fallback) == 0.0

    def test_unknown_model_falls_back(self) -> None:
        # Without a provider-specific fallback, unknown models cost at
        # DEFAULT_PRICING — verify the math matches get_pricing.
        tokens_in, tokens_out = 1_000, 2_000
        expected = (tokens_in * DEFAULT_PRICING[0] + tokens_out * DEFAULT_PRICING[1]) / 1_000_000
        assert compute_cost("nope", tokens_in, tokens_out) == expected
