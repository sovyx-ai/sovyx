"""Sovyx LLM Router — multi-provider routing with failover, cost, and circuit breaking.

v0.5 adds complexity-based routing: simple queries go to cheap/local models
(Flash, Haiku, Ollama) while complex queries go to expensive/powerful models
(Sonnet, Pro, GPT-4o). Reduces cost by ~85% on mixed workloads.

Ref: SPE-007 §5, Pre-Compute V05-37.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import TYPE_CHECKING

from sovyx.engine.errors import CostLimitExceededError, ProviderUnavailableError
from sovyx.engine.events import ThinkCompleted
from sovyx.llm.circuit import CircuitBreaker
from sovyx.llm.models import LLMResponse
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics
from sovyx.observability.tracing import get_tracer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.events import EventBus
    from sovyx.engine.protocols import LLMProvider
    from sovyx.llm.cost import CostGuard

logger = get_logger(__name__)


# ── Complexity Classification ───────────────────────────────────────


class ComplexityLevel(Enum):
    """Message complexity level for model routing."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclasses.dataclass(frozen=True, slots=True)
class ComplexitySignals:
    """Signals used to estimate message complexity.

    Attributes:
        message_length: Total character count of all messages.
        turn_count: Number of conversation turns.
        has_tool_use: Whether tool use is requested.
        has_code: Whether the message contains code.
        explicit_model: User explicitly requested a model.
    """

    message_length: int = 0
    turn_count: int = 0
    has_tool_use: bool = False
    has_code: bool = False
    explicit_model: bool = False


# Thresholds for complexity classification
_SIMPLE_MAX_LENGTH = 500
_SIMPLE_MAX_TURNS = 3
_COMPLEX_MIN_LENGTH = 2000
_COMPLEX_MIN_TURNS = 8

# Model tiers for routing
_SIMPLE_MODELS: set[str] = {
    "gemini-2.0-flash",
    "claude-3-5-haiku-20241022",
    "gpt-4o-mini",
}

_COMPLEX_MODELS: set[str] = {
    "claude-sonnet-4-20250514",
    "gemini-2.5-pro-preview-03-25",
    "gpt-4o",
}


def classify_complexity(signals: ComplexitySignals) -> ComplexityLevel:
    """Classify message complexity based on heuristic signals.

    The classifier uses a simple scoring system:
        - Short messages (< 500 chars) with few turns → SIMPLE
        - Long messages (> 2000 chars), many turns, or code/tools → COMPLEX
        - Everything else → MODERATE

    Args:
        signals: Input signals for classification.

    Returns:
        Complexity level.
    """
    # Explicit model request bypasses classification
    if signals.explicit_model:
        return ComplexityLevel.MODERATE

    # Tool use or code always complex
    if signals.has_tool_use or signals.has_code:
        return ComplexityLevel.COMPLEX

    # Score-based classification
    score = 0.0

    # Length signal
    if signals.message_length <= _SIMPLE_MAX_LENGTH:
        score -= 1.0
    elif signals.message_length >= _COMPLEX_MIN_LENGTH:
        score += 1.0

    # Turn count signal
    if signals.turn_count <= _SIMPLE_MAX_TURNS:
        score -= 0.5
    elif signals.turn_count >= _COMPLEX_MIN_TURNS:
        score += 1.0

    if score <= -1.0:
        return ComplexityLevel.SIMPLE
    if score >= 1.0:
        return ComplexityLevel.COMPLEX
    return ComplexityLevel.MODERATE


def extract_signals(messages: Sequence[dict[str, str]]) -> ComplexitySignals:
    """Extract complexity signals from a message list.

    Args:
        messages: Chat messages (role + content).

    Returns:
        Extracted signals.
    """
    total_length = sum(len(m.get("content", "")) for m in messages)
    turn_count = sum(1 for m in messages if m.get("role") in ("user", "assistant"))
    has_code = any(
        "```" in m.get("content", "") or "def " in m.get("content", "") for m in messages
    )

    return ComplexitySignals(
        message_length=total_length,
        turn_count=turn_count,
        has_code=has_code,
    )


def select_model_for_complexity(
    complexity: ComplexityLevel,
    available_models: Sequence[str],
) -> str | None:
    """Select the best model for a given complexity level.

    Args:
        complexity: Classified complexity.
        available_models: Models available from registered providers.

    Returns:
        Selected model name, or ``None`` if no match.
    """
    if complexity == ComplexityLevel.SIMPLE:
        for model in available_models:
            if model in _SIMPLE_MODELS:
                return model
    elif complexity == ComplexityLevel.COMPLEX:
        for model in available_models:
            if model in _COMPLEX_MODELS:
                return model

    # MODERATE or no tier match → first available
    return available_models[0] if available_models else None


class LLMRouter:
    """Route LLM calls across providers with failover.

    Failover chain: tries providers in order (Anthropic → OpenAI → Ollama).
    CostGuard: checks budget before each call.
    CircuitBreaker: per-provider, avoids hammering down services.
    """

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        cost_guard: CostGuard,
        event_bus: EventBus,
        circuit_breaker_failures: int = 3,
        circuit_breaker_reset_s: int = 60,
    ) -> None:
        self._providers = list(providers)
        self._cost_guard = cost_guard
        self._events = event_bus
        self._circuits: dict[str, CircuitBreaker] = {
            p.name: CircuitBreaker(
                failure_threshold=circuit_breaker_failures,
                recovery_timeout_s=circuit_breaker_reset_s,
            )
            for p in providers
        }

    def get_context_window(self, model: str | None = None) -> int:
        """Get context window from the provider that serves this model.

        Checks the primary model first, then equivalent models (cross-provider
        fallback), so context budget matches the model that will actually run.
        """
        if model:
            # Check primary model
            for provider in self._providers:
                if provider.supports_model(model):
                    return provider.get_context_window(model)
            # Check equivalent models (will be used via cross-provider fallback)
            for equiv in self._get_equivalent_models(model):
                for provider in self._providers:
                    if provider.supports_model(equiv):
                        return provider.get_context_window(equiv)
        return 128_000  # safe fallback

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        conversation_id: str = "",
        tools: list[dict[str, object]] | None = None,
    ) -> LLMResponse:
        """Generate response via most available provider.

        Args:
            messages: Chat messages.
            model: Preferred model (None = default per provider).
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            conversation_id: For per-conversation cost tracking.
            tools: Optional tool definitions for function calling.
                Each dict has name, description, parameters keys.

        Returns:
            LLMResponse from first successful provider.

        Raises:
            CostLimitExceededError: Budget exhausted.
            ProviderUnavailableError: All providers failed.
        """
        # ── Complexity-based routing ──
        if model is None:
            signals = extract_signals(messages)
            complexity = classify_complexity(signals)
            available = [
                m for p in self._providers if p.is_available for m in self._get_provider_models(p)
            ]
            routed_model = select_model_for_complexity(complexity, available)
            if routed_model:
                model = routed_model
                logger.debug(
                    "complexity_routed",
                    complexity=complexity.value,
                    model=model,
                    signals_length=signals.message_length,
                    signals_turns=signals.turn_count,
                )

        # Cost estimation: chars/4 ≈ tokens (rough but order-of-magnitude correct)
        input_chars = sum(len(m.get("content", "")) for m in messages)
        est_input_tokens = input_chars // 4
        pricing = self._get_pricing(model)
        estimated_cost = (est_input_tokens * pricing[0] + max_tokens * pricing[1]) / 1_000_000
        if not self._cost_guard.can_afford(estimated_cost, conversation_id):
            msg = (
                f"Budget exhausted. Daily remaining: "
                f"${self._cost_guard.get_remaining_budget():.2f}"
            )
            raise CostLimitExceededError(msg)

        # Build model fallback chain: requested model first, then equivalents
        models_to_try: list[str | None] = [model]
        if model:
            models_to_try.extend(self._get_equivalent_models(model))

        errors: list[str] = []

        for try_model in models_to_try:
            for provider in self._providers:
                # Skip if model specified and provider doesn't support it
                if try_model and not provider.supports_model(try_model):
                    continue

                # Skip if provider not available
                if not provider.is_available:
                    continue

                # Skip if circuit is open
                circuit = self._circuits.get(provider.name)
                if circuit and not circuit.can_call():
                    errors.append(f"{provider.name}: circuit open")
                    continue

                try:
                    use_model = try_model or "default"
                    if try_model and try_model != model:
                        logger.info(
                            "cross_provider_fallback",
                            original_model=model,
                            fallback_model=try_model,
                            provider=provider.name,
                        )
                    tracer = get_tracer()
                    metrics = get_metrics()

                    with (
                        tracer.start_llm_span(
                            provider=provider.name,
                            model=use_model,
                        ) as span,
                        metrics.measure_latency(
                            metrics.llm_response_latency,
                            {"provider": provider.name},
                        ),
                    ):
                        raw = await provider.generate(
                            messages,
                            model=use_model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            tools=tools,
                        )

                    response = (
                        LLMResponse(**vars(raw)) if not isinstance(raw, LLMResponse) else raw
                    )

                    # Record span attributes post-call
                    span.set_attribute("sovyx.llm.tokens_in", response.tokens_in)
                    span.set_attribute("sovyx.llm.tokens_out", response.tokens_out)
                    span.set_attribute("sovyx.llm.cost_usd", response.cost_usd)

                    # Record metrics
                    metrics.llm_calls.add(
                        1,
                        {
                            "provider": provider.name,
                            "model": response.model,
                        },
                    )
                    metrics.tokens_used.add(
                        response.tokens_in,
                        {"direction": "in", "provider": provider.name},
                    )
                    metrics.tokens_used.add(
                        response.tokens_out,
                        {"direction": "out", "provider": provider.name},
                    )
                    metrics.llm_cost.add(
                        response.cost_usd,
                        {"provider": provider.name},
                    )

                    # Record success
                    if circuit:
                        circuit.record_success()

                    # Record cost
                    await self._cost_guard.record(
                        response.cost_usd, response.model, conversation_id
                    )

                    # Update dashboard counters (non-OTel, queryable)
                    from sovyx.dashboard.status import get_counters

                    get_counters().record_llm_call(
                        response.cost_usd,
                        response.tokens_in + response.tokens_out,
                    )

                    # Emit event
                    await self._events.emit(
                        ThinkCompleted(
                            model=response.model,
                            tokens_in=response.tokens_in,
                            tokens_out=response.tokens_out,
                            cost_usd=response.cost_usd,
                            latency_ms=response.latency_ms,
                        )
                    )

                    logger.info(
                        "llm_response",
                        provider=provider.name,
                        model=response.model,
                        tokens=response.tokens_in + response.tokens_out,
                        cost=round(response.cost_usd, 6),
                    )

                    return response

                except Exception as e:
                    if circuit:
                        circuit.record_failure()
                    errors.append(f"{provider.name}: {e}")
                    logger.warning(
                        "provider_failed",
                        provider=provider.name,
                        error=str(e),
                    )
                    continue

        error_msg = (
            f"All providers failed: {'; '.join(errors)}" if errors else "No available providers"
        )
        raise ProviderUnavailableError(error_msg)

    @staticmethod
    def tool_definitions_to_dicts(
        tool_definitions: Sequence[object],
    ) -> list[dict[str, object]]:
        """Convert ToolDefinition objects to generic dicts for generate().

        Each ToolDefinition is expected to have name, description,
        and parameters attributes (the PluginSDK ToolDefinition contract).

        Args:
            tool_definitions: List of ToolDefinition-like objects.

        Returns:
            List of dicts with name, description, parameters keys.
        """
        result: list[dict[str, object]] = []
        for td in tool_definitions:
            result.append(
                {
                    "name": getattr(td, "name", ""),
                    "description": getattr(td, "description", ""),
                    "parameters": getattr(td, "parameters", {}),
                }
            )
        return result

    @staticmethod
    def _get_equivalent_models(model: str) -> list[str]:
        """Get equivalent models from other providers for cross-provider fallback.

        When the primary model fails on all its providers, the router tries
        equivalent-tier models from other providers before giving up.

        Equivalence tiers:
            - Flagship: claude-sonnet ↔ gpt-4o ↔ gemini-2.5-pro
            - Fast: claude-haiku ↔ gpt-4o-mini ↔ gemini-flash
        """
        _equivalence: dict[str, list[str]] = {
            # Flagship tier
            "claude-sonnet-4-20250514": ["gpt-4o", "gemini-2.5-pro-preview-03-25"],
            "gpt-4o": ["claude-sonnet-4-20250514", "gemini-2.5-pro-preview-03-25"],
            "gemini-2.5-pro-preview-03-25": ["claude-sonnet-4-20250514", "gpt-4o"],
            # Fast tier
            "claude-3-5-haiku-20241022": ["gpt-4o-mini", "gemini-2.0-flash"],
            "gpt-4o-mini": ["claude-3-5-haiku-20241022", "gemini-2.0-flash"],
            "gemini-2.0-flash": ["gpt-4o-mini", "claude-3-5-haiku-20241022"],
            # Reasoning tier
            "claude-opus-4-20250514": ["o1"],
            "o1": ["claude-opus-4-20250514"],
        }
        return _equivalence.get(model, [])

    @staticmethod
    def _get_provider_models(provider: LLMProvider) -> list[str]:
        """Get known models for a provider based on its name.

        Returns a list of model names this provider is known to serve.
        Used for complexity-based routing.
        """
        _models_by_provider: dict[str, list[str]] = {
            "anthropic": [
                "claude-sonnet-4-20250514",
                "claude-3-5-haiku-20241022",
                "claude-opus-4-20250514",
            ],
            "openai": ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
            "google": ["gemini-2.0-flash", "gemini-2.5-pro-preview-03-25"],
            "ollama": [],  # Local models vary
        }
        return _models_by_provider.get(provider.name, [])

    @staticmethod
    def _get_pricing(model: str | None) -> tuple[float, float]:
        """Get (input, output) pricing per 1M tokens for a model.

        Falls back to a conservative default if model is unknown.
        """
        # Consolidated pricing table (per 1M tokens USD)
        pricing: dict[str, tuple[float, float]] = {
            # Anthropic
            "claude-sonnet-4-20250514": (3.0, 15.0),
            "claude-3-5-haiku-20241022": (1.0, 5.0),
            "claude-opus-4-20250514": (15.0, 75.0),
            # OpenAI
            "gpt-4o": (5.0, 15.0),
            "gpt-4o-mini": (0.15, 0.6),
            "o1": (15.0, 60.0),
            "o3-mini": (1.1, 4.4),
            # Google
            "gemini-2.0-flash": (0.10, 0.40),
            "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
        }
        if model and model in pricing:
            return pricing[model]
        # Conservative default (Sonnet-class)
        return (3.0, 15.0)

    async def stop(self) -> None:
        """Close all providers (best-effort)."""
        for provider in self._providers:
            try:
                await provider.close()
            except Exception:
                logger.warning(
                    "provider_close_failed",
                    provider=provider.name,
                    exc_info=True,
                )
