"""Sovyx LLM Router — multi-provider routing with failover, cost, and circuit breaking."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.errors import CostLimitExceededError, ProviderUnavailableError
from sovyx.engine.events import ThinkCompleted
from sovyx.llm.circuit import CircuitBreaker
from sovyx.llm.models import LLMResponse
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.events import EventBus
    from sovyx.engine.protocols import LLMProvider
    from sovyx.llm.cost import CostGuard

logger = get_logger(__name__)


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

        Uses supports_model() to resolve provider.
        """
        if model:
            for provider in self._providers:
                if provider.supports_model(model):
                    return provider.get_context_window(model)
        return 128_000  # safe fallback

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        conversation_id: str = "",
    ) -> LLMResponse:
        """Generate response via most available provider.

        Args:
            messages: Chat messages.
            model: Preferred model (None = default per provider).
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            conversation_id: For per-conversation cost tracking.

        Returns:
            LLMResponse from first successful provider.

        Raises:
            CostLimitExceededError: Budget exhausted.
            ProviderUnavailableError: All providers failed.
        """
        # Cost check (estimate ~$0.01 per call)
        estimated_cost = 0.01
        if not self._cost_guard.can_afford(estimated_cost, conversation_id):
            msg = (
                f"Budget exhausted. Daily remaining: "
                f"${self._cost_guard.get_remaining_budget():.2f}"
            )
            raise CostLimitExceededError(msg)

        errors: list[str] = []

        for provider in self._providers:
            # Skip if model specified and provider doesn't support it
            if model and not provider.supports_model(model):
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
                use_model = model or "default"
                raw = await provider.generate(
                    messages,
                    model=use_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                response = LLMResponse(**vars(raw)) if not isinstance(raw, LLMResponse) else raw

                # Record success
                if circuit:
                    circuit.record_success()

                # Record cost
                self._cost_guard.record(response.cost_usd, response.model, conversation_id)

                # Emit event
                await self._events.emit(
                    ThinkCompleted(
                        model=response.model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
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
