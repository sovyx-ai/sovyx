"""Tests for LLMProxyService — multi-provider routing, metering, rate limiting (V05-12).

Covers LLMProxyService, ProxyConfig, rate limiting, model resolution,
failover, metering store, BYOK, and error handling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.llm_proxy import (
    DEFAULT_FALLBACKS,
    DEFAULT_MODEL_ALIASES,
    DEFAULT_TIMEOUT_S,
    TIER_RATE_LIMITS,
    AllProvidersFailedError,
    InMemoryMeteringStore,
    LiteLLMBackend,
    LLMProviderBackend,
    LLMProxyService,
    MeteringSnapshot,
    MeteringStore,
    ModelNotFoundError,
    ProxyConfig,
    ProxyError,
    ProxyResponse,
    RateLimitExceededError,
    RateTier,
    UsageRecord,
    _RateLimiter,
)

# ── Helpers ───────────────────────────────────────────────────────────────


class MockBackend(LLMProviderBackend):
    """Mock LLM backend for testing."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        fail: bool = False,
        fail_models: set[str] | None = None,
    ) -> None:
        self._response = response or {
            "content": "Hello, world!",
            "model": "anthropic/claude-sonnet-4-20250514",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "cost": 0.001,
            "finish_reason": "stop",
        }
        self._fail = fail
        self._fail_models: set[str] = fail_models or set()
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        api_key: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
        drop_params: bool = True,
    ) -> dict[str, Any]:
        self.calls.append({
            "model": model,
            "messages": messages,
            "api_key": api_key,
            "timeout": timeout,
        })
        if self._fail or model in self._fail_models:
            msg = f"Provider error for {model}"
            raise ProxyError(msg)
        return dict(self._response)


def _make_service(
    backend: LLMProviderBackend | None = None,
    config: ProxyConfig | None = None,
    metering: MeteringStore | None = None,
) -> LLMProxyService:
    return LLMProxyService(
        config=config or ProxyConfig(),
        backend=backend or MockBackend(),
        metering=metering or InMemoryMeteringStore(),
    )


def _simple_messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "Hello"}]


# ── UsageRecord Tests ─────────────────────────────────────────────────────


class TestUsageRecord:
    def test_total_tokens(self) -> None:
        record = UsageRecord(
            user_id="u1",
            model="sovyx/default",
            provider_model="anthropic/claude-sonnet-4-20250514",
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.001,
            latency_ms=100.0,
            success=True,
        )
        assert record.total_tokens == 30

    def test_timestamp_default(self) -> None:
        record = UsageRecord(
            user_id="u1",
            model="m",
            provider_model="p",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=0.0,
            success=True,
        )
        assert record.timestamp.tzinfo == UTC

    def test_error_field(self) -> None:
        record = UsageRecord(
            user_id="u1",
            model="m",
            provider_model="p",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=0.0,
            success=False,
            error="timeout",
        )
        assert record.error == "timeout"
        assert not record.success

    def test_frozen(self) -> None:
        record = UsageRecord(
            user_id="u1",
            model="m",
            provider_model="p",
            prompt_tokens=5,
            completion_tokens=5,
            cost_usd=0.0,
            latency_ms=0.0,
            success=True,
        )
        with pytest.raises(AttributeError):
            record.user_id = "u2"  # type: ignore[misc]


class TestProxyResponse:
    def test_total_tokens(self) -> None:
        resp = ProxyResponse(
            content="hi",
            model="sovyx/default",
            provider_model="anthropic/claude-sonnet-4-20250514",
            prompt_tokens=15,
            completion_tokens=25,
            cost_usd=0.002,
            latency_ms=50.0,
        )
        assert resp.total_tokens == 40

    def test_default_finish_reason(self) -> None:
        resp = ProxyResponse(
            content="hi",
            model="m",
            provider_model="p",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=0.0,
        )
        assert resp.finish_reason == "stop"


# ── ProxyConfig Tests ─────────────────────────────────────────────────────


class TestProxyConfig:
    def test_defaults(self) -> None:
        config = ProxyConfig()
        assert config.timeout_s == DEFAULT_TIMEOUT_S
        assert config.max_retries == 3
        assert config.drop_unsupported_params is True
        assert "sovyx/default" in config.model_aliases
        assert "sovyx/fast" in config.model_aliases

    def test_custom_config(self) -> None:
        config = ProxyConfig(
            timeout_s=60,
            max_retries=5,
            model_aliases={"my/model": ["openai/gpt-4"]},
            fallbacks={},
        )
        assert config.timeout_s == 60
        assert config.max_retries == 5
        assert "my/model" in config.model_aliases
        assert "sovyx/default" not in config.model_aliases

    def test_frozen(self) -> None:
        config = ProxyConfig()
        with pytest.raises(AttributeError):
            config.timeout_s = 999  # type: ignore[misc]


# ── RateTier Tests ────────────────────────────────────────────────────────


class TestRateTier:
    def test_all_tiers_have_limits(self) -> None:
        for tier in RateTier:
            assert tier in TIER_RATE_LIMITS

    def test_business_higher_than_sync(self) -> None:
        assert TIER_RATE_LIMITS[RateTier.BUSINESS] > TIER_RATE_LIMITS[RateTier.SYNC]

    def test_enterprise_highest(self) -> None:
        max_limit = max(TIER_RATE_LIMITS.values())
        assert TIER_RATE_LIMITS[RateTier.ENTERPRISE] == max_limit

    def test_tier_values(self) -> None:
        assert RateTier.FREE.value == "free"
        assert RateTier.CLOUD.value == "cloud"


# ── Rate Limiter Tests ────────────────────────────────────────────────────


class TestRateLimiter:
    async def test_allows_within_limit(self) -> None:
        limiter = _RateLimiter()
        result = await limiter.check("user1", RateTier.SYNC)
        assert result is True

    async def test_blocks_over_limit(self) -> None:
        limiter = _RateLimiter()
        # Fill up the limit for FREE tier (10/min)
        for _ in range(10):
            await limiter.check("user1", RateTier.FREE)
        with pytest.raises(RateLimitExceededError) as exc_info:
            await limiter.check("user1", RateTier.FREE)
        assert exc_info.value.user_id == "user1"
        assert exc_info.value.tier == RateTier.FREE
        assert exc_info.value.limit == 10

    async def test_different_users_independent(self) -> None:
        limiter = _RateLimiter()
        for _ in range(10):
            await limiter.check("user1", RateTier.FREE)
        # user2 should still be fine
        result = await limiter.check("user2", RateTier.FREE)
        assert result is True

    async def test_reset_user(self) -> None:
        limiter = _RateLimiter()
        for _ in range(10):
            await limiter.check("user1", RateTier.FREE)
        limiter.reset("user1")
        # Should be able to make requests again
        result = await limiter.check("user1", RateTier.FREE)
        assert result is True

    async def test_reset_all(self) -> None:
        limiter = _RateLimiter()
        for _ in range(10):
            await limiter.check("user1", RateTier.FREE)
        for _ in range(10):
            await limiter.check("user2", RateTier.FREE)
        limiter.reset()
        assert await limiter.check("user1", RateTier.FREE) is True
        assert await limiter.check("user2", RateTier.FREE) is True

    async def test_current_count(self) -> None:
        limiter = _RateLimiter()
        assert limiter.current_count("user1") == 0
        for _ in range(5):
            await limiter.check("user1", RateTier.SYNC)
        assert limiter.current_count("user1") == 5

    async def test_higher_tier_more_requests(self) -> None:
        limiter = _RateLimiter()
        # Business tier allows 300/min — should handle 100 easily
        for _ in range(100):
            await limiter.check("biz_user", RateTier.BUSINESS)
        # Should still be within limit
        result = await limiter.check("biz_user", RateTier.BUSINESS)
        assert result is True


# ── InMemoryMeteringStore Tests ───────────────────────────────────────────


class TestInMemoryMeteringStore:
    async def test_record_and_snapshot(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        record = UsageRecord(
            user_id="u1",
            model="sovyx/default",
            provider_model="anthropic/claude-sonnet-4-20250514",
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.001,
            latency_ms=100.0,
            success=True,
            timestamp=now,
        )
        await store.record(record)

        snap = await store.get_snapshot("u1", now.date())
        assert snap.user_id == "u1"
        assert snap.total_requests == 1
        assert snap.total_tokens == 30
        assert snap.total_cost_usd == 0.001
        assert snap.by_model["sovyx/default"] == 30
        assert snap.failed_requests == 0

    async def test_snapshot_filters_by_user(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        for uid in ["u1", "u2"]:
            await store.record(UsageRecord(
                user_id=uid,
                model="m",
                provider_model="p",
                prompt_tokens=10,
                completion_tokens=0,
                cost_usd=0.01,
                latency_ms=50.0,
                success=True,
                timestamp=now,
            ))

        snap = await store.get_snapshot("u1", now.date())
        assert snap.total_requests == 1

    async def test_snapshot_filters_by_date(self) -> None:
        store = InMemoryMeteringStore()
        today = datetime.now(UTC)
        yesterday = datetime(2020, 1, 1, tzinfo=UTC)
        await store.record(UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=10, completion_tokens=0, cost_usd=0.01,
            latency_ms=50.0, success=True, timestamp=today,
        ))
        await store.record(UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=5, completion_tokens=0, cost_usd=0.005,
            latency_ms=50.0, success=True, timestamp=yesterday,
        ))

        snap = await store.get_snapshot("u1", today.date())
        assert snap.total_requests == 1
        assert snap.total_tokens == 10

    async def test_snapshot_counts_failures(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        await store.record(UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
            latency_ms=50.0, success=False, error="timeout",
            timestamp=now,
        ))
        snap = await store.get_snapshot("u1", now.date())
        assert snap.failed_requests == 1

    async def test_daily_tokens(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        await store.record(UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=100, completion_tokens=200, cost_usd=0.01,
            latency_ms=50.0, success=True, timestamp=now,
        ))
        tokens = await store.get_daily_tokens("u1", now.date())
        assert tokens == 300

    async def test_daily_tokens_default_today(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        await store.record(UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=50, completion_tokens=50, cost_usd=0.005,
            latency_ms=50.0, success=True, timestamp=now,
        ))
        tokens = await store.get_daily_tokens("u1")
        assert tokens == 100

    async def test_records_property(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        rec = UsageRecord(
            user_id="u1", model="m", provider_model="p",
            prompt_tokens=10, completion_tokens=10, cost_usd=0.001,
            latency_ms=50.0, success=True, timestamp=now,
        )
        await store.record(rec)
        assert len(store.records) == 1
        assert store.records[0] == rec

    async def test_empty_snapshot(self) -> None:
        store = InMemoryMeteringStore()
        snap = await store.get_snapshot("nobody", date.today())
        assert snap.total_requests == 0
        assert snap.total_tokens == 0
        assert snap.total_cost_usd == 0.0
        assert snap.by_model == {}
        assert snap.failed_requests == 0

    async def test_multiple_models_aggregation(self) -> None:
        store = InMemoryMeteringStore()
        now = datetime.now(UTC)
        await store.record(UsageRecord(
            user_id="u1", model="sovyx/default", provider_model="p1",
            prompt_tokens=10, completion_tokens=10, cost_usd=0.01,
            latency_ms=50.0, success=True, timestamp=now,
        ))
        await store.record(UsageRecord(
            user_id="u1", model="sovyx/fast", provider_model="p2",
            prompt_tokens=5, completion_tokens=5, cost_usd=0.005,
            latency_ms=30.0, success=True, timestamp=now,
        ))
        snap = await store.get_snapshot("u1", now.date())
        assert snap.total_requests == 2
        assert snap.by_model["sovyx/default"] == 20
        assert snap.by_model["sovyx/fast"] == 10
        assert snap.cost_by_model["sovyx/default"] == 0.01
        assert snap.cost_by_model["sovyx/fast"] == 0.005


# ── Model Resolution Tests ───────────────────────────────────────────────


class TestModelResolution:
    def test_resolve_default_alias(self) -> None:
        service = _make_service()
        models = service.resolve_model("sovyx/default")
        assert len(models) >= 1
        assert models == DEFAULT_MODEL_ALIASES["sovyx/default"]

    def test_resolve_fast_alias(self) -> None:
        service = _make_service()
        models = service.resolve_model("sovyx/fast")
        assert models == DEFAULT_MODEL_ALIASES["sovyx/fast"]

    def test_resolve_direct_provider_model(self) -> None:
        service = _make_service()
        models = service.resolve_model("openai/gpt-4o")
        assert models == ["openai/gpt-4o"]

    def test_resolve_unknown_raises(self) -> None:
        service = _make_service()
        with pytest.raises(ModelNotFoundError) as exc_info:
            service.resolve_model("unknown_model")
        assert exc_info.value.model == "unknown_model"

    def test_get_fallbacks_default(self) -> None:
        service = _make_service()
        fb = service.get_fallbacks("sovyx/default")
        assert fb == DEFAULT_FALLBACKS["sovyx/default"]

    def test_get_fallbacks_none(self) -> None:
        service = _make_service()
        fb = service.get_fallbacks("sovyx/local")
        assert fb == []

    def test_custom_aliases(self) -> None:
        config = ProxyConfig(
            model_aliases={"custom/model": ["openai/gpt-4"]},
            fallbacks={},
        )
        service = _make_service(config=config)
        models = service.resolve_model("custom/model")
        assert models == ["openai/gpt-4"]


# ── LLMProxyService Route Request Tests ───────────────────────────────────


class TestRouteRequest:
    async def test_basic_route(self) -> None:
        backend = MockBackend()
        service = _make_service(backend=backend)
        resp = await service.route_request(
            "sovyx/default",
            _simple_messages(),
            user_id="u1",
            tier=RateTier.SYNC,
        )
        assert resp.content == "Hello, world!"
        assert resp.model == "sovyx/default"
        assert resp.prompt_tokens == 10
        assert resp.completion_tokens == 20
        assert resp.cost_usd == 0.001
        assert resp.latency_ms > 0
        assert len(backend.calls) == 1

    async def test_metering_recorded_on_success(self) -> None:
        metering = InMemoryMeteringStore()
        service = _make_service(metering=metering)
        await service.route_request(
            "sovyx/default",
            _simple_messages(),
            user_id="test_user",
            tier=RateTier.CLOUD,
        )
        assert len(metering.records) == 1
        rec = metering.records[0]
        assert rec.user_id == "test_user"
        assert rec.success is True
        assert rec.prompt_tokens == 10
        assert rec.completion_tokens == 20

    async def test_rate_limit_enforced(self) -> None:
        service = _make_service()
        # Exhaust FREE tier (10/min)
        for _ in range(10):
            await service.route_request(
                "sovyx/default",
                _simple_messages(),
                user_id="limited_user",
                tier=RateTier.FREE,
            )
        with pytest.raises(RateLimitExceededError):
            await service.route_request(
                "sovyx/default",
                _simple_messages(),
                user_id="limited_user",
                tier=RateTier.FREE,
            )

    async def test_byok_passes_api_key(self) -> None:
        backend = MockBackend()
        service = _make_service(backend=backend)
        await service.route_request(
            "sovyx/default",
            _simple_messages(),
            user_id="u1",
            api_key="sk-user-key-123",
        )
        assert backend.calls[0]["api_key"] == "sk-user-key-123"

    async def test_failover_to_next_provider(self) -> None:
        # First provider fails, second succeeds
        backend = MockBackend(
            fail_models={"anthropic/claude-sonnet-4-20250514"},
            response={
                "content": "Fallback response",
                "model": "openai/gpt-4o",
                "prompt_tokens": 5,
                "completion_tokens": 10,
                "cost": 0.0005,
                "finish_reason": "stop",
            },
        )
        service = _make_service(backend=backend)
        resp = await service.route_request(
            "sovyx/default",
            _simple_messages(),
            user_id="u1",
        )
        assert resp.content == "Fallback response"
        assert len(backend.calls) == 2  # tried first, then second

    async def test_all_providers_fail(self) -> None:
        backend = MockBackend(fail=True)
        metering = InMemoryMeteringStore()
        service = _make_service(backend=backend, metering=metering)
        with pytest.raises(AllProvidersFailedError) as exc_info:
            await service.route_request(
                "sovyx/default",
                _simple_messages(),
                user_id="u1",
            )
        assert exc_info.value.model == "sovyx/default"
        assert len(exc_info.value.errors) > 0
        # Failure should be metered
        assert len(metering.records) == 1
        assert metering.records[0].success is False

    async def test_unknown_model_raises(self) -> None:
        service = _make_service()
        with pytest.raises(ModelNotFoundError):
            await service.route_request(
                "nonexistent_model",
                _simple_messages(),
                user_id="u1",
            )

    async def test_direct_provider_model_works(self) -> None:
        backend = MockBackend()
        service = _make_service(backend=backend)
        resp = await service.route_request(
            "openai/gpt-4o",
            _simple_messages(),
            user_id="u1",
        )
        assert resp.content == "Hello, world!"
        assert backend.calls[0]["model"] == "openai/gpt-4o"

    async def test_fallback_chain(self) -> None:
        # sovyx/default → fail all default providers → fallback to sovyx/fast
        default_models = DEFAULT_MODEL_ALIASES["sovyx/default"]
        fail_set = set(default_models)
        backend = MockBackend(
            fail_models=fail_set,
            response={
                "content": "Fast fallback",
                "model": "anthropic/claude-haiku-3",
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "cost": 0.0001,
                "finish_reason": "stop",
            },
        )
        service = _make_service(backend=backend)
        resp = await service.route_request(
            "sovyx/default",
            _simple_messages(),
            user_id="u1",
        )
        assert resp.content == "Fast fallback"
        # Should have tried default models then fallback
        assert len(backend.calls) == len(default_models) + 1

    async def test_finish_reason_propagated(self) -> None:
        backend = MockBackend(response={
            "content": "partial",
            "model": "m",
            "prompt_tokens": 5,
            "completion_tokens": 100,
            "cost": 0.01,
            "finish_reason": "length",
        })
        service = _make_service(backend=backend)
        resp = await service.route_request(
            "sovyx/default", _simple_messages(), user_id="u1",
        )
        assert resp.finish_reason == "length"

    async def test_config_timeout_passed_to_backend(self) -> None:
        config = ProxyConfig(timeout_s=120, max_retries=5)
        backend = MockBackend()
        service = _make_service(backend=backend, config=config)
        await service.route_request("sovyx/default", _simple_messages(), user_id="u1")
        assert backend.calls[0]["timeout"] == 120


# ── Usage / Metering Query Tests ──────────────────────────────────────────


class TestUsageQueries:
    async def test_get_usage(self) -> None:
        metering = InMemoryMeteringStore()
        service = _make_service(metering=metering)
        await service.route_request(
            "sovyx/default", _simple_messages(), user_id="u1",
        )
        snap = await service.get_usage("u1")
        assert snap.total_requests == 1
        assert snap.total_tokens == 30

    async def test_get_daily_tokens(self) -> None:
        metering = InMemoryMeteringStore()
        service = _make_service(metering=metering)
        await service.route_request(
            "sovyx/default", _simple_messages(), user_id="u1",
        )
        tokens = await service.get_daily_tokens("u1")
        assert tokens == 30

    async def test_get_usage_empty(self) -> None:
        service = _make_service()
        snap = await service.get_usage("nobody")
        assert snap.total_requests == 0


# ── Exception Tests ───────────────────────────────────────────────────────


class TestExceptions:
    def test_rate_limit_error_message(self) -> None:
        err = RateLimitExceededError("u1", RateTier.FREE, 10)
        assert "u1" in str(err)
        assert "10" in str(err)
        assert "free" in str(err)

    def test_model_not_found_error_message(self) -> None:
        err = ModelNotFoundError("bad/model")
        assert "bad/model" in str(err)

    def test_all_providers_failed_error(self) -> None:
        err = AllProvidersFailedError("sovyx/default", ["err1", "err2"])
        assert "sovyx/default" in str(err)
        assert "err1" in str(err)
        assert "err2" in str(err)

    def test_proxy_error_base(self) -> None:
        err = ProxyError("generic error")
        assert str(err) == "generic error"


# ── Property Tests ────────────────────────────────────────────────────────


class TestProperties:
    def test_service_config_property(self) -> None:
        config = ProxyConfig(timeout_s=99)
        service = _make_service(config=config)
        assert service.config.timeout_s == 99

    def test_service_metering_property(self) -> None:
        metering = InMemoryMeteringStore()
        service = _make_service(metering=metering)
        assert service.metering is metering

    def test_service_rate_limiter_property(self) -> None:
        service = _make_service()
        assert service.rate_limiter is not None


# ── MeteringSnapshot Tests ────────────────────────────────────────────────


class TestMeteringSnapshot:
    def test_fields(self) -> None:
        snap = MeteringSnapshot(
            user_id="u1",
            period=date.today(),
            total_requests=5,
            total_tokens=1000,
            total_cost_usd=0.05,
            by_model={"sovyx/default": 800, "sovyx/fast": 200},
            cost_by_model={"sovyx/default": 0.04, "sovyx/fast": 0.01},
            failed_requests=1,
        )
        assert snap.user_id == "u1"
        assert snap.total_requests == 5
        assert snap.total_tokens == 1000
        assert snap.failed_requests == 1
        assert len(snap.by_model) == 2


# ── Hypothesis Property-Based Tests ──────────────────────────────────────


class TestPropertyBased:
    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        prompt_tokens=st.integers(min_value=0, max_value=100000),
        completion_tokens=st.integers(min_value=0, max_value=100000),
    )
    def test_usage_record_total_tokens_invariant(
        self, prompt_tokens: int, completion_tokens: int,
    ) -> None:
        record = UsageRecord(
            user_id="u1",
            model="m",
            provider_model="p",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=0.0,
            latency_ms=0.0,
            success=True,
        )
        assert record.total_tokens == prompt_tokens + completion_tokens
        assert record.total_tokens >= 0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        prompt_tokens=st.integers(min_value=0, max_value=100000),
        completion_tokens=st.integers(min_value=0, max_value=100000),
    )
    def test_proxy_response_total_tokens_invariant(
        self, prompt_tokens: int, completion_tokens: int,
    ) -> None:
        resp = ProxyResponse(
            content="test",
            model="m",
            provider_model="p",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=0.0,
            latency_ms=0.0,
        )
        assert resp.total_tokens == prompt_tokens + completion_tokens

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(cost=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False))
    def test_metering_cost_non_negative(self, cost: float) -> None:
        snap = MeteringSnapshot(
            user_id="u1",
            period=date.today(),
            total_requests=1,
            total_tokens=100,
            total_cost_usd=cost,
            by_model={},
            cost_by_model={},
            failed_requests=0,
        )
        assert snap.total_cost_usd >= 0.0


# ── LiteLLMBackend Tests (import guard) ──────────────────────────────────


class TestLiteLLMBackend:
    def test_instantiation(self) -> None:
        backend = LiteLLMBackend()
        assert isinstance(backend, LLMProviderBackend)


# ── Integration-style Tests ───────────────────────────────────────────────


class TestIntegration:
    async def test_multiple_users_metering(self) -> None:
        metering = InMemoryMeteringStore()
        service = _make_service(metering=metering)
        for user in ["alice", "bob", "charlie"]:
            await service.route_request(
                "sovyx/default", _simple_messages(),
                user_id=user, tier=RateTier.CLOUD,
            )
        assert len(metering.records) == 3
        for user in ["alice", "bob", "charlie"]:
            snap = await service.get_usage(user)
            assert snap.total_requests == 1

    async def test_mixed_success_and_failure(self) -> None:
        # First request succeeds, then provider starts failing
        call_count = 0
        original_backend = MockBackend()

        class FlakeyBackend(LLMProviderBackend):
            async def completion(  # type: ignore[override]
                self,
                model: str,
                messages: list[dict[str, str]],
                **kwargs: object,
            ) -> dict[str, Any]:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    return await original_backend.completion(model, messages, **kwargs)
                raise ProxyError("flake")

        metering = InMemoryMeteringStore()
        # Use direct model to avoid fallback chain complexity
        config = ProxyConfig(model_aliases={"test/model": ["provider/m1"]}, fallbacks={})
        service = _make_service(backend=FlakeyBackend(), metering=metering, config=config)

        # First request succeeds
        resp = await service.route_request(
            "test/model", _simple_messages(), user_id="u1",
        )
        assert resp.content == "Hello, world!"

        # Second request also succeeds
        resp2 = await service.route_request(
            "test/model", _simple_messages(), user_id="u1",
        )
        assert resp2.content == "Hello, world!"

        # Third request fails (all providers fail)
        with pytest.raises(AllProvidersFailedError):
            await service.route_request(
                "test/model", _simple_messages(), user_id="u1",
            )

        assert len(metering.records) == 3
        assert metering.records[0].success is True
        assert metering.records[1].success is True
        assert metering.records[2].success is False

    async def test_concurrent_requests_rate_limiting(self) -> None:
        service = _make_service()
        # Send requests concurrently — some should fail at FREE tier
        tasks = [
            service.route_request(
                "sovyx/default", _simple_messages(),
                user_id="concurrent_user", tier=RateTier.FREE,
            )
            for _ in range(15)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = [r for r in results if isinstance(r, ProxyResponse)]
        failures = [r for r in results if isinstance(r, RateLimitExceededError)]
        assert len(successes) == 10
        assert len(failures) == 5
