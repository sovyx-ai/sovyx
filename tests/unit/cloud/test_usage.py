"""Tests for UsageCascade — 4-stage token metering (V05-13).

Covers: included → flex → auto-topup → hard limit cascade,
period reset, account status, thread-safety, edge cases.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.usage import (
    TIER_MONTHLY_TOKENS,
    AccountUsage,
    AutoTopupCharger,
    CascadeStage,
    ChargeResult,
    FlexAccount,
    InMemoryUsageStore,
    UsageCascade,
    UsageTier,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> InMemoryUsageStore:
    """Fresh in-memory store."""
    return InMemoryUsageStore()


@pytest.fixture()
def charger() -> AsyncMock:
    """Mock auto-topup charger."""
    mock = AsyncMock(spec=AutoTopupCharger)
    mock.charge.return_value = 0
    return mock


def _fixed_now(d: date | None = None) -> datetime:
    """Return a fixed datetime for testing."""
    target = d or date(2026, 4, 7)
    return datetime(target.year, target.month, target.day, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def cascade(store: InMemoryUsageStore, charger: AsyncMock) -> UsageCascade:
    """Cascade with in-memory store and mock charger."""
    return UsageCascade(store, charger, now_fn=lambda: _fixed_now())


# ── Stage 1: Included Tokens ─────────────────────────────────────────────


class TestIncludedTokens:
    """Stage 1: charge from monthly included quota."""

    async def test_charge_within_quota(self, cascade: UsageCascade) -> None:
        result = await cascade.charge("acc-1", 100, tier=UsageTier.FREE)
        assert result.stage == CascadeStage.INCLUDED
        assert result.tokens_charged == 100
        assert result.remaining == TIER_MONTHLY_TOKENS[UsageTier.FREE] - 100
        assert result.blocked is False
        assert result.account_id == "acc-1"

    async def test_charge_exact_quota(self, cascade: UsageCascade) -> None:
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]
        result = await cascade.charge("acc-1", quota, tier=UsageTier.FREE)
        assert result.stage == CascadeStage.INCLUDED
        assert result.tokens_charged == quota
        assert result.remaining == 0
        assert result.blocked is False

    async def test_multiple_charges_within_quota(self, cascade: UsageCascade) -> None:
        quota = TIER_MONTHLY_TOKENS[UsageTier.STARTER]
        half = quota // 2
        r1 = await cascade.charge("acc-1", half, tier=UsageTier.STARTER)
        assert r1.stage == CascadeStage.INCLUDED
        assert r1.remaining == quota - half

        r2 = await cascade.charge("acc-1", half, tier=UsageTier.STARTER)
        assert r2.stage == CascadeStage.INCLUDED
        assert r2.remaining == quota - 2 * half

    async def test_all_tiers_have_quotas(self) -> None:
        for tier in UsageTier:
            assert tier in TIER_MONTHLY_TOKENS
            assert TIER_MONTHLY_TOKENS[tier] > 0

    async def test_tier_ordering(self) -> None:
        tiers = [
            UsageTier.FREE,
            UsageTier.STARTER,
            UsageTier.SYNC,
            UsageTier.CLOUD,
            UsageTier.BUSINESS,
            UsageTier.ENTERPRISE,
        ]
        for i in range(len(tiers) - 1):
            assert TIER_MONTHLY_TOKENS[tiers[i]] < TIER_MONTHLY_TOKENS[tiers[i + 1]]


# ── Stage 2: Flex Balance ─────────────────────────────────────────────────


class TestFlexBalance:
    """Stage 2: charge from pre-paid flex balance."""

    async def test_flex_after_included_exhausted(
        self, store: InMemoryUsageStore, charger: AsyncMock
    ) -> None:
        # Exhaust included quota
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        # Add flex balance
        await store.save_flex("acc-1", FlexAccount(balance=5000))

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 1000)
        assert result.stage == CascadeStage.FLEX
        assert result.tokens_charged == 1000
        assert result.remaining == 4000
        assert result.blocked is False

    async def test_flex_exact_balance(
        self, store: InMemoryUsageStore, charger: AsyncMock
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex("acc-1", FlexAccount(balance=500))

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 500)
        assert result.stage == CascadeStage.FLEX
        assert result.tokens_charged == 500
        assert result.remaining == 0

    async def test_flex_insufficient_falls_through(
        self, store: InMemoryUsageStore, charger: AsyncMock
    ) -> None:
        """Flex with insufficient balance should fall to auto-topup or hard limit."""
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex("acc-1", FlexAccount(balance=100))

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 500)
        # No charger success, no auto-topup → hard limit
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True


# ── Stage 3: Auto-Topup ──────────────────────────────────────────────────


class TestAutoTopup:
    """Stage 3: auto-topup via card charge."""

    async def test_auto_topup_success(
        self, store: InMemoryUsageStore,
    ) -> None:
        charger = AsyncMock(spec=AutoTopupCharger)
        charger.charge.return_value = 10_000  # Purchased 10k tokens

        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=True, auto_topup_amount_cents=1000),
        )

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 5000)
        assert result.stage == CascadeStage.AUTO_TOPUP
        assert result.tokens_charged == 5000
        assert result.remaining == 5000  # 10k purchased - 5k charged
        assert result.blocked is False
        charger.charge.assert_awaited_once_with("acc-1", 1000)

    async def test_auto_topup_disabled(
        self, store: InMemoryUsageStore,
    ) -> None:
        charger = AsyncMock(spec=AutoTopupCharger)
        charger.charge.return_value = 10_000

        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=False),
        )

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 5000)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True
        charger.charge.assert_not_awaited()

    async def test_auto_topup_charge_fails(
        self, store: InMemoryUsageStore,
    ) -> None:
        charger = AsyncMock(spec=AutoTopupCharger)
        charger.charge.return_value = 0  # Card charge failed

        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=True, auto_topup_amount_cents=1000),
        )

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 5000)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True

    async def test_auto_topup_insufficient_purchase(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Auto-topup succeeds but purchased tokens still not enough."""
        charger = AsyncMock(spec=AutoTopupCharger)
        charger.charge.return_value = 100  # Only got 100 tokens

        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=True, auto_topup_amount_cents=500),
        )

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 5000)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True

        # Balance should still reflect the topped-up tokens
        flex = await store.get_flex("acc-1")
        assert flex is not None
        assert flex.balance == 100  # Purchased amount saved despite hard limit

    async def test_auto_topup_no_charger(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Auto-topup enabled but no charger configured."""
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=True),
        )

        cascade = UsageCascade(store, charger=None, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 5000)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True


# ── Stage 4: Hard Limit ──────────────────────────────────────────────────


class TestHardLimit:
    """Stage 4: blocked when all stages exhausted."""

    async def test_hard_limit_no_flex(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )

        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        result = await cascade.charge("acc-1", 100)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.tokens_charged == 0
        assert result.remaining == 0
        assert result.blocked is True
        assert result.account_id == "acc-1"

    async def test_hard_limit_request_exceeds_all(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Request larger than included + flex combined."""
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE] - 50,
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex("acc-1", FlexAccount(balance=100))

        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        # 200 tokens needed: 50 included + 100 flex = 150 < 200
        result = await cascade.charge("acc-1", 200)
        # Should use included first (50 remaining), then flex is insufficient for 200
        # Actually: included has 50 remaining < 200, so skip to flex.
        # Flex has 100 < 200, so skip to hard limit.
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True


# ── Period Reset ──────────────────────────────────────────────────────────


class TestPeriodReset:
    """Billing period auto-reset and manual reset."""

    async def test_auto_reset_on_new_month(
        self, store: InMemoryUsageStore,
    ) -> None:
        # Set usage in March
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=5000,
                period_start=date(2026, 3, 1),
                tier=UsageTier.FREE,
            ),
        )

        # Charge in April → should auto-reset
        cascade = UsageCascade(
            store, now_fn=lambda: _fixed_now(date(2026, 4, 7))
        )
        result = await cascade.charge("acc-1", 100)
        assert result.stage == CascadeStage.INCLUDED
        assert result.tokens_charged == 100
        assert result.remaining == TIER_MONTHLY_TOKENS[UsageTier.FREE] - 100

    async def test_auto_reset_year_boundary(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=9999,
                period_start=date(2025, 12, 15),
                tier=UsageTier.FREE,
            ),
        )

        cascade = UsageCascade(
            store, now_fn=lambda: _fixed_now(date(2026, 1, 5))
        )
        result = await cascade.charge("acc-1", 100)
        assert result.stage == CascadeStage.INCLUDED
        assert result.remaining == TIER_MONTHLY_TOKENS[UsageTier.FREE] - 100

    async def test_no_reset_same_month(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=5000,
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )

        cascade = UsageCascade(
            store, now_fn=lambda: _fixed_now(date(2026, 4, 28))
        )
        result = await cascade.charge("acc-1", 100)
        assert result.stage == CascadeStage.INCLUDED
        assert result.remaining == TIER_MONTHLY_TOKENS[UsageTier.FREE] - 5100

    async def test_manual_reset(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=8000,
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )

        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        await cascade.reset_period("acc-1")

        usage = await store.get_usage("acc-1")
        assert usage is not None
        assert usage.included_used == 0


# ── Account Status ────────────────────────────────────────────────────────


class TestAccountStatus:
    """get_account_status returns correct usage summary."""

    async def test_status_new_account(self, cascade: UsageCascade) -> None:
        status = await cascade.get_account_status("new-acc")
        assert status["account_id"] == "new-acc"
        assert status["tier"] == "free"
        assert status["included_used"] == 0
        assert status["included_remaining"] == TIER_MONTHLY_TOKENS[UsageTier.FREE]
        assert status["flex_balance"] == 0
        assert status["auto_topup_enabled"] is False

    async def test_status_after_charges(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=3000,
                period_start=date(2026, 4, 1),
                tier=UsageTier.CLOUD,
            ),
        )
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=5000, auto_topup_enabled=True, auto_topup_threshold=500),
        )

        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        status = await cascade.get_account_status("acc-1")
        assert status["tier"] == "cloud"
        assert status["included_used"] == 3000
        assert status["included_remaining"] == TIER_MONTHLY_TOKENS[UsageTier.CLOUD] - 3000
        assert status["included_quota"] == TIER_MONTHLY_TOKENS[UsageTier.CLOUD]
        assert status["flex_balance"] == 5000
        assert status["auto_topup_enabled"] is True
        assert status["auto_topup_threshold"] == 500


# ── Validation ────────────────────────────────────────────────────────────


class TestValidation:
    """Input validation."""

    async def test_zero_tokens_raises(self, cascade: UsageCascade) -> None:
        with pytest.raises(ValueError, match="positive"):
            await cascade.charge("acc-1", 0)

    async def test_negative_tokens_raises(self, cascade: UsageCascade) -> None:
        with pytest.raises(ValueError, match="positive"):
            await cascade.charge("acc-1", -10)


# ── Thread Safety ─────────────────────────────────────────────────────────


class TestThreadSafety:
    """Concurrent access safety via per-account locks."""

    async def test_concurrent_charges_same_account(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Multiple concurrent charges on same account shouldn't exceed quota."""
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]

        # Fire 20 concurrent charges of quota/10 each
        chunk = quota // 10
        results = await asyncio.gather(
            *[cascade.charge("acc-1", chunk, tier=UsageTier.FREE) for _ in range(20)]
        )

        included_charges = [r for r in results if r.stage == CascadeStage.INCLUDED]
        assert len(included_charges) == 10  # Only 10 fit in quota

        hard_limits = [r for r in results if r.stage == CascadeStage.HARD_LIMIT]
        assert len(hard_limits) == 10

        total_charged = sum(r.tokens_charged for r in results)
        assert total_charged == quota  # Exactly quota consumed

    async def test_concurrent_charges_different_accounts(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Different accounts should not interfere with each other."""
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]

        results = await asyncio.gather(
            cascade.charge("acc-1", quota, tier=UsageTier.FREE),
            cascade.charge("acc-2", quota, tier=UsageTier.FREE),
        )

        assert all(r.stage == CascadeStage.INCLUDED for r in results)
        assert all(r.tokens_charged == quota for r in results)


# ── In-Memory Store ───────────────────────────────────────────────────────


class TestInMemoryUsageStore:
    """InMemoryUsageStore correctness."""

    async def test_roundtrip_usage(self) -> None:
        store = InMemoryUsageStore()
        usage = AccountUsage(included_used=42, tier=UsageTier.CLOUD)
        await store.save_usage("x", usage)
        loaded = await store.get_usage("x")
        assert loaded is not None
        assert loaded.included_used == 42
        assert loaded.tier == UsageTier.CLOUD

    async def test_roundtrip_flex(self) -> None:
        store = InMemoryUsageStore()
        flex = FlexAccount(balance=9999, auto_topup_enabled=True)
        await store.save_flex("x", flex)
        loaded = await store.get_flex("x")
        assert loaded is not None
        assert loaded.balance == 9999
        assert loaded.auto_topup_enabled is True

    async def test_missing_returns_none(self) -> None:
        store = InMemoryUsageStore()
        assert await store.get_usage("nope") is None
        assert await store.get_flex("nope") is None


# ── Tier Override ─────────────────────────────────────────────────────────


class TestTierOverride:
    """Tier can be overridden per-charge."""

    async def test_override_upgrades_quota(
        self, store: InMemoryUsageStore,
    ) -> None:
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        free_quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]

        # First charge as FREE
        await cascade.charge("acc-1", free_quota, tier=UsageTier.FREE)

        # Upgrade to CLOUD — should have new quota
        result = await cascade.charge("acc-1", 100, tier=UsageTier.CLOUD)
        # Period hasn't reset (same month), but tier changed → quota expanded
        # included_used is still free_quota, but cloud quota is much larger
        cloud_quota = TIER_MONTHLY_TOKENS[UsageTier.CLOUD]
        assert result.stage == CascadeStage.INCLUDED
        assert result.remaining == cloud_quota - free_quota - 100


# ── Full Cascade Flow ────────────────────────────────────────────────────


class TestFullCascadeFlow:
    """End-to-end cascade through all stages."""

    async def test_included_then_flex_then_hard_limit(
        self, store: InMemoryUsageStore,
    ) -> None:
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]

        # Stage 1: use all included
        r1 = await cascade.charge("acc-1", quota, tier=UsageTier.FREE)
        assert r1.stage == CascadeStage.INCLUDED

        # Add flex balance
        await store.save_flex("acc-1", FlexAccount(balance=500))

        # Stage 2: use flex
        r2 = await cascade.charge("acc-1", 500)
        assert r2.stage == CascadeStage.FLEX
        assert r2.remaining == 0

        # Stage 4: hard limit (no auto-topup)
        r3 = await cascade.charge("acc-1", 100)
        assert r3.stage == CascadeStage.HARD_LIMIT
        assert r3.blocked is True

    async def test_included_then_auto_topup(
        self, store: InMemoryUsageStore,
    ) -> None:
        charger = AsyncMock(spec=AutoTopupCharger)
        charger.charge.return_value = 50_000

        cascade = UsageCascade(store, charger, now_fn=lambda: _fixed_now())
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]

        # Exhaust included
        await cascade.charge("acc-1", quota, tier=UsageTier.FREE)

        # Enable auto-topup with zero flex
        await store.save_flex(
            "acc-1",
            FlexAccount(balance=0, auto_topup_enabled=True, auto_topup_amount_cents=2500),
        )

        # Should trigger auto-topup
        r = await cascade.charge("acc-1", 1000)
        assert r.stage == CascadeStage.AUTO_TOPUP
        assert r.tokens_charged == 1000
        assert r.remaining == 49_000
        charger.charge.assert_awaited_once_with("acc-1", 2500)


# ── ChargeResult Frozen ───────────────────────────────────────────────────


class TestChargeResultFrozen:
    """ChargeResult is immutable."""

    def test_frozen(self) -> None:
        result = ChargeResult(
            stage=CascadeStage.INCLUDED,
            tokens_charged=100,
            remaining=900,
            blocked=False,
            account_id="x",
        )
        with pytest.raises(AttributeError):
            result.tokens_charged = 999  # type: ignore[misc]


# ── Property-Based Tests ─────────────────────────────────────────────────


class TestPropertyBased:
    """Property-based tests with Hypothesis."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        tokens=st.integers(min_value=1, max_value=1_000_000),
        tier=st.sampled_from(list(UsageTier)),
    )
    async def test_charge_never_exceeds_quota(
        self, tokens: int, tier: UsageTier
    ) -> None:
        """Charge result is always valid: either tokens charged or blocked."""
        store = InMemoryUsageStore()
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        result = await cascade.charge("prop-acc", tokens, tier=tier)

        if result.blocked:
            assert result.tokens_charged == 0
            assert result.stage == CascadeStage.HARD_LIMIT
        else:
            assert result.tokens_charged == tokens
            assert result.remaining >= 0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        n_charges=st.integers(min_value=1, max_value=50),
    )
    async def test_total_charged_never_exceeds_quota(self, n_charges: int) -> None:
        """Sum of all charged tokens on included never exceeds quota."""
        store = InMemoryUsageStore()
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        quota = TIER_MONTHLY_TOKENS[UsageTier.FREE]
        chunk = max(1, quota // n_charges)

        total = 0
        for _ in range(n_charges + 5):  # Try more than quota allows
            result = await cascade.charge("prop-acc", chunk, tier=UsageTier.FREE)
            total += result.tokens_charged

        assert total <= quota


# ── Edge Cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Misc edge cases."""

    async def test_new_account_defaults_to_free(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Charging a brand new account uses FREE tier defaults."""
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        result = await cascade.charge("brand-new", 100)
        assert result.stage == CascadeStage.INCLUDED
        assert result.remaining == TIER_MONTHLY_TOKENS[UsageTier.FREE] - 100

    async def test_single_token_charge(self, cascade: UsageCascade) -> None:
        result = await cascade.charge("acc-1", 1, tier=UsageTier.FREE)
        assert result.tokens_charged == 1
        assert result.blocked is False

    async def test_large_token_charge_exceeds_free(
        self, store: InMemoryUsageStore,
    ) -> None:
        """Single charge larger than entire FREE quota → hard limit."""
        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        big = TIER_MONTHLY_TOKENS[UsageTier.FREE] + 1
        result = await cascade.charge("acc-1", big, tier=UsageTier.FREE)
        assert result.stage == CascadeStage.HARD_LIMIT
        assert result.blocked is True

    async def test_flex_balance_persisted_after_charge(
        self, store: InMemoryUsageStore,
    ) -> None:
        await store.save_usage(
            "acc-1",
            AccountUsage(
                included_used=TIER_MONTHLY_TOKENS[UsageTier.FREE],
                period_start=date(2026, 4, 1),
                tier=UsageTier.FREE,
            ),
        )
        await store.save_flex("acc-1", FlexAccount(balance=1000))

        cascade = UsageCascade(store, now_fn=lambda: _fixed_now())
        await cascade.charge("acc-1", 300)

        flex = await store.get_flex("acc-1")
        assert flex is not None
        assert flex.balance == 700
