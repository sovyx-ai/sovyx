"""LLM Proxy Service — multi-provider routing via LiteLLM with token metering.

Provides a unified OpenAI-compatible API across LLM providers with:
- Model aliasing (``sovyx/default``, ``sovyx/fast``, ``sovyx/local``)
- BYOK (Bring Your Own Key): user provides their own API key
- Per-user, per-model token metering and cost tracking
- Tier-based rate limiting (Sync: 60/min, Cloud: 120/min, Business: 300/min)

References:
    - SPE-007 §5: LLM router specification
    - IMPL-SUP-008 §1: LiteLLM Cloud Proxy (CLD-020)
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from sovyx.engine.errors import CloudError
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_RETRIES = 3
METERING_FLUSH_INTERVAL_S = 60


class RateTier(Enum):
    """Rate limit tiers for LLM proxy access."""

    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


# Requests per minute by tier
TIER_RATE_LIMITS: dict[RateTier, int] = {
    RateTier.FREE: 10,
    RateTier.STARTER: 30,
    RateTier.SYNC: 60,
    RateTier.CLOUD: 120,
    RateTier.BUSINESS: 300,
    RateTier.ENTERPRISE: 1000,
}

# Model aliases mapped to provider models
DEFAULT_MODEL_ALIASES: dict[str, list[str]] = {
    "sovyx/default": ["anthropic/claude-sonnet-4-20250514", "openai/gpt-4o"],
    "sovyx/fast": ["anthropic/claude-haiku-3"],
    "sovyx/local": ["ollama/llama3.1"],
}

# Fallback chains
DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "sovyx/default": ["sovyx/fast"],
    "sovyx/fast": ["sovyx/local"],
}


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Configuration for the LLM proxy service.

    Attributes:
        timeout_s: Request timeout in seconds.
        max_retries: Number of retries on failure.
        drop_unsupported_params: Silently drop unsupported provider params.
        model_aliases: Mapping of alias → provider model list.
        fallbacks: Mapping of model → fallback models.
    """

    timeout_s: int = DEFAULT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    drop_unsupported_params: bool = True
    model_aliases: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_ALIASES),
    )
    fallbacks: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_FALLBACKS),
    )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Single LLM usage record for metering.

    Attributes:
        user_id: Sovyx account/API key identifier.
        model: Model name as requested.
        provider_model: Actual provider model used.
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.
        cost_usd: Estimated cost in USD.
        latency_ms: Request latency in milliseconds.
        success: Whether the request succeeded.
        timestamp: When the request was made.
        error: Error message if failed.
    """

    user_id: str
    model: str
    provider_model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    success: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed (prompt + completion)."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    """Response from the LLM proxy.

    Attributes:
        content: Generated text content.
        model: Model that generated the response.
        provider_model: Actual provider/model identifier.
        prompt_tokens: Input tokens used.
        completion_tokens: Output tokens generated.
        cost_usd: Estimated cost in USD.
        latency_ms: Request latency in milliseconds.
        finish_reason: Why generation stopped.
    """

    content: str
    model: str
    provider_model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    finish_reason: str = "stop"

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class MeteringSnapshot:
    """Aggregated metering data for a user over a period.

    Attributes:
        user_id: Account identifier.
        period: Date of the aggregation period.
        total_requests: Number of requests.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total estimated cost.
        by_model: Token counts per model.
        cost_by_model: Cost per model.
        failed_requests: Number of failed requests.
    """

    user_id: str
    period: date
    total_requests: int
    total_tokens: int
    total_cost_usd: float
    by_model: dict[str, int]
    cost_by_model: dict[str, float]
    failed_requests: int


# ── Exceptions ────────────────────────────────────────────────────────────


class ProxyError(CloudError):
    """Base exception for LLM proxy errors."""


class RateLimitExceededError(ProxyError):
    """Raised when user exceeds their tier rate limit."""

    def __init__(self, user_id: str, tier: RateTier, limit: int) -> None:
        self.user_id = user_id
        self.tier = tier
        self.limit = limit
        super().__init__(
            f"Rate limit exceeded for user {user_id}: {limit} req/min ({tier.value} tier)"
        )


class ModelNotFoundError(ProxyError):
    """Raised when requested model is not configured."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Model not found: {model}")


class AllProvidersFailedError(ProxyError):
    """Raised when all providers in the chain fail."""

    def __init__(self, model: str, errors: list[str]) -> None:
        self.model = model
        self.errors = errors
        super().__init__(f"All providers failed for {model}: {'; '.join(errors)}")


# ── Rate Limiter ──────────────────────────────────────────────────────────


class _RateLimiter:
    """Sliding-window rate limiter for per-user request control.

    Uses a simple in-memory deque of timestamps per user.
    """

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, user_id: str, tier: RateTier) -> bool:
        """Check if user is within their rate limit.

        Args:
            user_id: User identifier.
            tier: User's subscription tier.

        Returns:
            ``True`` if request is allowed.

        Raises:
            RateLimitExceededError: If the user has exceeded their limit.
        """
        limit = TIER_RATE_LIMITS.get(tier, TIER_RATE_LIMITS[RateTier.FREE])
        now = time.monotonic()
        window_start = now - 60.0

        async with self._lock:
            timestamps = self._windows[user_id]
            # Prune old entries
            self._windows[user_id] = [t for t in timestamps if t > window_start]
            timestamps = self._windows[user_id]

            if len(timestamps) >= limit:
                raise RateLimitExceededError(user_id, tier, limit)

            timestamps.append(now)

        return True

    def reset(self, user_id: str | None = None) -> None:
        """Reset rate limit state.

        Args:
            user_id: Specific user to reset, or ``None`` for all.
        """
        if user_id is not None:
            self._windows.pop(user_id, None)
        else:
            self._windows.clear()

    def current_count(self, user_id: str) -> int:
        """Get current request count in the window for a user.

        Args:
            user_id: User identifier.

        Returns:
            Number of requests in the current 60-second window.
        """
        now = time.monotonic()
        window_start = now - 60.0
        timestamps = self._windows.get(user_id, [])
        return sum(1 for t in timestamps if t > window_start)


# ── Metering Store ────────────────────────────────────────────────────────


class MeteringStore:
    """Abstract metering store for usage records."""

    async def record(self, usage: UsageRecord) -> None:
        """Record a usage entry.

        Args:
            usage: The usage record to store.
        """
        raise NotImplementedError  # pragma: no cover

    async def get_snapshot(self, user_id: str, period: date) -> MeteringSnapshot:
        """Get aggregated metering for a user on a given date.

        Args:
            user_id: Account identifier.
            period: Date to aggregate.

        Returns:
            Aggregated metering snapshot.
        """
        raise NotImplementedError  # pragma: no cover

    async def get_daily_tokens(self, user_id: str, day: date | None = None) -> int:
        """Get total tokens used by a user on a given day.

        Args:
            user_id: Account identifier.
            day: Date to query (defaults to today).

        Returns:
            Total tokens consumed.
        """
        raise NotImplementedError  # pragma: no cover


class InMemoryMeteringStore(MeteringStore):
    """In-memory metering store for testing and development."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        """Record a usage entry in memory.

        Args:
            usage: The usage record to store.
        """
        self._records.append(usage)

    async def get_snapshot(self, user_id: str, period: date) -> MeteringSnapshot:
        """Get aggregated metering for a user on a given date.

        Args:
            user_id: Account identifier.
            period: Date to aggregate.

        Returns:
            Aggregated metering snapshot.
        """
        matching = [
            r for r in self._records if r.user_id == user_id and r.timestamp.date() == period
        ]

        by_model: dict[str, int] = defaultdict(int)
        cost_by_model: dict[str, float] = defaultdict(float)
        total_tokens = 0
        total_cost = 0.0
        failed = 0

        for rec in matching:
            by_model[rec.model] += rec.total_tokens
            cost_by_model[rec.model] += rec.cost_usd
            total_tokens += rec.total_tokens
            total_cost += rec.cost_usd
            if not rec.success:
                failed += 1

        return MeteringSnapshot(
            user_id=user_id,
            period=period,
            total_requests=len(matching),
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            by_model=dict(by_model),
            cost_by_model=dict(cost_by_model),
            failed_requests=failed,
        )

    async def get_daily_tokens(self, user_id: str, day: date | None = None) -> int:
        """Get total tokens used by a user on a given day.

        Args:
            user_id: Account identifier.
            day: Date to query (defaults to today).

        Returns:
            Total tokens consumed.
        """
        target = day or date.today()
        return sum(
            r.total_tokens
            for r in self._records
            if r.user_id == user_id and r.timestamp.date() == target
        )

    @property
    def records(self) -> list[UsageRecord]:
        """All stored records (read-only access for testing)."""
        return list(self._records)


# ── LLM Provider Interface ───────────────────────────────────────────────


class LLMProviderBackend:
    """Abstract backend for LLM provider calls.

    In production, wraps LiteLLM. In tests, easily mocked.
    """

    async def completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        api_key: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        drop_params: bool = True,
    ) -> dict[str, Any]:
        """Call LLM provider for completion.

        Args:
            model: Provider model identifier (e.g. ``anthropic/claude-sonnet-4-20250514``).
            messages: Chat messages in OpenAI format.
            api_key: Optional user-provided API key (BYOK).
            timeout: Request timeout in seconds.
            max_retries: Number of retries.
            drop_params: Drop unsupported parameters silently.

        Returns:
            Raw response dict with ``choices``, ``usage``, ``model`` keys.

        Raises:
            ProxyError: On provider failure.
        """
        raise NotImplementedError  # pragma: no cover


class LiteLLMBackend(LLMProviderBackend):
    """Production backend using LiteLLM for unified provider access."""

    async def completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        api_key: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        drop_params: bool = True,
    ) -> dict[str, Any]:
        """Call LiteLLM for completion.

        Args:
            model: Provider model identifier.
            messages: Chat messages.
            api_key: Optional BYOK key.
            timeout: Timeout in seconds.
            max_retries: Retry count.
            drop_params: Drop unsupported params.

        Returns:
            Response dict with choices, usage, model.
        """
        try:
            import litellm  # noqa: PLC0415
        except ImportError as exc:
            msg = "litellm is required for LLMProxyService. Install with: pip install litellm"
            raise ProxyError(msg) from exc

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": timeout,
            "num_retries": max_retries,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        if drop_params:
            litellm.drop_params = True

        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            msg = f"LiteLLM call failed for {model}: {exc}"
            raise ProxyError(msg) from exc

        # Normalize response to dict
        usage = getattr(response, "usage", None)
        choices = getattr(response, "choices", [])
        content = ""
        finish_reason = "stop"
        if choices:
            choice = choices[0]
            message = getattr(choice, "message", None)
            content = getattr(message, "content", "") if message else ""
            finish_reason = getattr(choice, "finish_reason", "stop") or "stop"

        cost = getattr(response, "_hidden_params", {}).get("response_cost", 0.0)

        return {
            "content": content,
            "model": getattr(response, "model", model),
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            "cost": cost if isinstance(cost, float) else 0.0,
            "finish_reason": finish_reason,
        }


# ── LLM Proxy Service ────────────────────────────────────────────────────


class LLMProxyService:
    """Multi-provider LLM proxy with routing, metering, and rate limiting.

    Wraps LiteLLM to provide:
    - Model alias resolution (``sovyx/default`` → configured providers)
    - Failover across providers for the same alias
    - Per-user, per-model token metering
    - Tier-based rate limiting
    - BYOK support (user provides their own API key)

    Args:
        config: Proxy configuration.
        backend: LLM provider backend (LiteLLM in prod, mock in tests).
        metering: Metering store for usage tracking.
    """

    def __init__(
        self,
        config: ProxyConfig | None = None,
        backend: LLMProviderBackend | None = None,
        metering: MeteringStore | None = None,
    ) -> None:
        self._config = config or ProxyConfig()
        self._backend = backend or LiteLLMBackend()
        self._metering = metering or InMemoryMeteringStore()
        self._rate_limiter = _RateLimiter()

    @property
    def config(self) -> ProxyConfig:
        """Current proxy configuration."""
        return self._config

    @property
    def metering(self) -> MeteringStore:
        """Metering store."""
        return self._metering

    @property
    def rate_limiter(self) -> _RateLimiter:
        """Rate limiter instance."""
        return self._rate_limiter

    def resolve_model(self, model: str) -> list[str]:
        """Resolve a model alias to provider model list.

        Args:
            model: Model name or alias (e.g. ``sovyx/default``).

        Returns:
            List of provider model identifiers to try in order.

        Raises:
            ModelNotFoundError: If the model/alias is not configured.
        """
        # Direct alias match
        if model in self._config.model_aliases:
            return list(self._config.model_aliases[model])

        # Check if it's a direct provider model (e.g. "anthropic/claude-3-opus")
        if "/" in model:
            return [model]

        raise ModelNotFoundError(model)

    def get_fallbacks(self, model: str) -> list[str]:
        """Get fallback models for a given model.

        Args:
            model: Model name or alias.

        Returns:
            List of fallback model names (may be empty).
        """
        return list(self._config.fallbacks.get(model, []))

    async def route_request(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        user_id: str = "anonymous",
        tier: RateTier = RateTier.FREE,
        api_key: str | None = None,
    ) -> ProxyResponse:
        """Route an LLM request through the proxy.

        Resolves model aliases, applies rate limiting, tries providers
        with failover, meters usage, and returns the response.

        Args:
            model: Model name or alias.
            messages: Chat messages in OpenAI format.
            user_id: User/account identifier for metering.
            tier: User's subscription tier for rate limiting.
            api_key: Optional BYOK API key.

        Returns:
            Proxy response with content and usage data.

        Raises:
            RateLimitExceededError: If user exceeds tier rate limit.
            ModelNotFoundError: If model is not configured.
            AllProvidersFailedError: If all providers fail.
        """
        # Rate limit check
        await self._rate_limiter.check(user_id, tier)

        # Build provider chain: resolved models + fallbacks
        try:
            provider_models = self.resolve_model(model)
        except ModelNotFoundError:
            raise

        # Add fallback models
        fallback_aliases = self.get_fallbacks(model)
        for fb_alias in fallback_aliases:
            try:
                fb_models = self.resolve_model(fb_alias)
                provider_models = [*provider_models, *fb_models]
            except ModelNotFoundError:
                continue

        # Try each provider in order
        errors: list[str] = []
        start_time = time.monotonic()

        for provider_model in provider_models:
            try:
                result = await self._backend.completion(
                    model=provider_model,
                    messages=messages,
                    api_key=api_key,
                    timeout=self._config.timeout_s,
                    max_retries=self._config.max_retries,
                    drop_params=self._config.drop_unsupported_params,
                )

                elapsed_ms = (time.monotonic() - start_time) * 1000

                response = ProxyResponse(
                    content=result.get("content", ""),
                    model=model,
                    provider_model=result.get("model", provider_model),
                    prompt_tokens=result.get("prompt_tokens", 0),
                    completion_tokens=result.get("completion_tokens", 0),
                    cost_usd=result.get("cost", 0.0),
                    latency_ms=elapsed_ms,
                    finish_reason=result.get("finish_reason", "stop"),
                )

                # Record success metering
                await self._metering.record(
                    UsageRecord(
                        user_id=user_id,
                        model=model,
                        provider_model=response.provider_model,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        cost_usd=response.cost_usd,
                        latency_ms=elapsed_ms,
                        success=True,
                    )
                )

                logger.debug(
                    "llm_proxy_success",
                    user=user_id,
                    model=model,
                    provider=provider_model,
                    tokens=response.total_tokens,
                    cost=f"${response.cost_usd:.6f}",
                    latency_ms=round(elapsed_ms),
                )

                return response

            except ProxyError as exc:
                errors.append(str(exc))
                logger.warning(
                    "llm_proxy_provider_failed",
                    provider=provider_model,
                    error=str(exc),
                )
                continue

        # All providers failed — record failure metering
        elapsed_ms = (time.monotonic() - start_time) * 1000
        await self._metering.record(
            UsageRecord(
                user_id=user_id,
                model=model,
                provider_model="none",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                latency_ms=elapsed_ms,
                success=False,
                error="; ".join(errors),
            )
        )

        raise AllProvidersFailedError(model, errors)

    async def get_usage(self, user_id: str, day: date | None = None) -> MeteringSnapshot:
        """Get usage snapshot for a user.

        Args:
            user_id: Account identifier.
            day: Date to query (defaults to today).

        Returns:
            Metering snapshot with aggregated usage data.
        """
        target = day or date.today()
        return await self._metering.get_snapshot(user_id, target)

    async def get_daily_tokens(self, user_id: str, day: date | None = None) -> int:
        """Get total tokens used by a user today.

        Args:
            user_id: Account identifier.
            day: Date to query (defaults to today).

        Returns:
            Total tokens consumed.
        """
        return await self._metering.get_daily_tokens(user_id, day)
