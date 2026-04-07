"""Usage cascade — 4-stage token metering: included → flex → auto-topup → hard limit.

Implements the token consumption pipeline for Sovyx cloud services.
Each charge request flows through four stages in order:

1. **Included tokens** — monthly quota from the subscription tier
2. **Flex balance** — pre-paid token balance (top-ups)
3. **Auto-topup** — automatic card charge when flex runs low (if enabled)
4. **Hard limit** — request blocked with HTTP 402

Thread-safe via per-account asyncio locks.

References:
    - IMPL-SUP-006 §cascade: Usage flow specification
    - SPE-033 §CLD-020: LLM Proxy metering
"""

from __future__ import annotations

import asyncio
import enum
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_AUTO_TOPUP_THRESHOLD = 200
"""Default: trigger auto-topup when flex balance drops below 200 tokens."""

DEFAULT_AUTO_TOPUP_AMOUNT_CENTS = 1000
"""Default auto-topup amount: $10.00."""


# ── Tier token quotas ─────────────────────────────────────────────────────


class UsageTier(enum.Enum):
    """Subscription tiers with monthly token quotas."""

    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


TIER_MONTHLY_TOKENS: dict[UsageTier, int] = {
    UsageTier.FREE: 10_000,
    UsageTier.STARTER: 100_000,
    UsageTier.SYNC: 500_000,
    UsageTier.CLOUD: 2_000_000,
    UsageTier.BUSINESS: 10_000_000,
    UsageTier.ENTERPRISE: 50_000_000,
}
"""Monthly included token quota per tier."""


# ── Cascade stage ─────────────────────────────────────────────────────────


class CascadeStage(enum.Enum):
    """Stage at which tokens were charged."""

    INCLUDED = "included"
    FLEX = "flex"
    AUTO_TOPUP = "auto_topup"
    HARD_LIMIT = "hard_limit"


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChargeResult:
    """Result of a usage cascade charge attempt.

    Attributes:
        stage: Which cascade stage fulfilled the charge.
        tokens_charged: Number of tokens actually deducted.
        remaining: Remaining tokens in the stage that was used.
        blocked: Whether the request was blocked (hard limit reached).
        account_id: The account that was charged.
    """

    stage: CascadeStage
    tokens_charged: int
    remaining: int
    blocked: bool
    account_id: str


@dataclass(slots=True)
class AccountUsage:
    """Per-account usage tracking for the current billing period.

    Attributes:
        included_used: Tokens consumed from monthly included quota.
        period_start: Start date of the current billing period.
        tier: Current subscription tier.
    """

    included_used: int = 0
    period_start: date = field(default_factory=lambda: datetime.now(UTC).date())
    tier: UsageTier = UsageTier.FREE


@dataclass(slots=True)
class FlexAccount:
    """Pre-paid flex balance for an account.

    Attributes:
        balance: Current token balance (pre-paid).
        auto_topup_enabled: Whether automatic top-up is active.
        auto_topup_threshold: Trigger threshold for auto-topup.
        auto_topup_amount_cents: Amount in cents to charge on auto-topup.
    """

    balance: int = 0
    auto_topup_enabled: bool = False
    auto_topup_threshold: int = DEFAULT_AUTO_TOPUP_THRESHOLD
    auto_topup_amount_cents: int = DEFAULT_AUTO_TOPUP_AMOUNT_CENTS


# ── Protocols ─────────────────────────────────────────────────────────────


class UsageStore(Protocol):
    """Persistent storage for account usage data."""

    async def get_usage(self, account_id: str) -> AccountUsage | None:
        """Load usage for an account."""
        ...

    async def save_usage(self, account_id: str, usage: AccountUsage) -> None:
        """Persist usage for an account."""
        ...

    async def get_flex(self, account_id: str) -> FlexAccount | None:
        """Load flex balance for an account."""
        ...

    async def save_flex(self, account_id: str, flex: FlexAccount) -> None:
        """Persist flex balance for an account."""
        ...


class AutoTopupCharger(Protocol):
    """Charges the customer's card for auto-topup.

    Returns the number of tokens purchased, or 0 on failure.
    """

    async def charge(self, account_id: str, amount_cents: int) -> int:
        """Charge card and return tokens purchased (0 if failed)."""
        ...


# ── In-memory store (testing / dev) ──────────────────────────────────────


class InMemoryUsageStore:
    """In-memory implementation of UsageStore for testing and development."""

    def __init__(self) -> None:
        self._usage: dict[str, AccountUsage] = {}
        self._flex: dict[str, FlexAccount] = {}

    async def get_usage(self, account_id: str) -> AccountUsage | None:
        """Load usage from memory."""
        return self._usage.get(account_id)

    async def save_usage(self, account_id: str, usage: AccountUsage) -> None:
        """Save usage to memory."""
        self._usage[account_id] = usage

    async def get_flex(self, account_id: str) -> FlexAccount | None:
        """Load flex from memory."""
        return self._flex.get(account_id)

    async def save_flex(self, account_id: str, flex: FlexAccount) -> None:
        """Save flex to memory."""
        self._flex[account_id] = flex


# ── Usage Cascade ─────────────────────────────────────────────────────────


class UsageCascade:
    """4-stage token usage cascade: included → flex → auto-topup → hard limit.

    Thread-safe via per-account asyncio locks. Each call to :meth:`charge`
    walks through the stages in order, consuming from the first available
    source and returning a :class:`ChargeResult`.

    Args:
        store: Persistent storage backend for usage and flex data.
        charger: Optional auto-topup charger (Stripe, etc.).
        now_fn: Optional clock function for testing.
    """

    def __init__(
        self,
        store: UsageStore,
        charger: AutoTopupCharger | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._charger = charger
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    async def charge(
        self,
        account_id: str,
        tokens: int,
        *,
        tier: UsageTier | None = None,
    ) -> ChargeResult:
        """Charge tokens through the 4-stage cascade.

        Args:
            account_id: The account to charge.
            tokens: Number of tokens to consume.
            tier: Override tier (if None, uses stored tier or FREE).

        Returns:
            ChargeResult with stage, tokens charged, remaining, and blocked flag.

        Raises:
            ValueError: If tokens is negative or zero.
        """
        if tokens <= 0:
            msg = f"tokens must be positive, got {tokens}"
            raise ValueError(msg)

        async with self._locks[account_id]:
            return await self._charge_locked(account_id, tokens, tier)

    async def _charge_locked(
        self,
        account_id: str,
        tokens: int,
        tier_override: UsageTier | None,
    ) -> ChargeResult:
        """Execute cascade under account lock."""
        usage = await self._store.get_usage(account_id) or AccountUsage()
        flex = await self._store.get_flex(account_id) or FlexAccount()

        # Reset period if new billing cycle
        current_date = self._now_fn().date()
        month_changed = usage.period_start.month != current_date.month
        year_changed = usage.period_start.year != current_date.year
        if month_changed or year_changed:
            usage.included_used = 0
            usage.period_start = current_date

        # Apply tier override
        if tier_override is not None:
            usage.tier = tier_override

        # Stage 1: Included tokens
        quota = TIER_MONTHLY_TOKENS[usage.tier]
        included_remaining = quota - usage.included_used
        if included_remaining >= tokens:
            usage.included_used += tokens
            await self._store.save_usage(account_id, usage)
            logger.debug(
                "cascade_charge",
                account_id=account_id,
                stage="included",
                tokens=tokens,
                remaining=quota - usage.included_used,
            )
            return ChargeResult(
                stage=CascadeStage.INCLUDED,
                tokens_charged=tokens,
                remaining=quota - usage.included_used,
                blocked=False,
                account_id=account_id,
            )

        # Stage 2: Flex balance
        if flex.balance >= tokens:
            flex.balance -= tokens
            await self._store.save_flex(account_id, flex)
            await self._store.save_usage(account_id, usage)
            logger.debug(
                "cascade_charge",
                account_id=account_id,
                stage="flex",
                tokens=tokens,
                remaining=flex.balance,
            )
            return ChargeResult(
                stage=CascadeStage.FLEX,
                tokens_charged=tokens,
                remaining=flex.balance,
                blocked=False,
                account_id=account_id,
            )

        # Stage 3: Auto-topup
        if flex.auto_topup_enabled and self._charger is not None:
            purchased = await self._charger.charge(account_id, flex.auto_topup_amount_cents)
            if purchased > 0:
                flex.balance += purchased
                if flex.balance >= tokens:
                    flex.balance -= tokens
                    await self._store.save_flex(account_id, flex)
                    await self._store.save_usage(account_id, usage)
                    logger.info(
                        "cascade_auto_topup",
                        account_id=account_id,
                        purchased=purchased,
                        tokens=tokens,
                        remaining=flex.balance,
                    )
                    return ChargeResult(
                        stage=CascadeStage.AUTO_TOPUP,
                        tokens_charged=tokens,
                        remaining=flex.balance,
                        blocked=False,
                        account_id=account_id,
                    )
                # Topup wasn't enough — save the balance but fall through to hard limit
                await self._store.save_flex(account_id, flex)

        # Stage 4: Hard limit
        logger.warning(
            "cascade_hard_limit",
            account_id=account_id,
            tokens_requested=tokens,
            included_remaining=included_remaining,
            flex_balance=flex.balance,
        )
        return ChargeResult(
            stage=CascadeStage.HARD_LIMIT,
            tokens_charged=0,
            remaining=0,
            blocked=True,
            account_id=account_id,
        )

    async def get_account_status(self, account_id: str) -> dict[str, Any]:
        """Return current usage status for an account.

        Returns:
            Dict with included_used, included_remaining, flex_balance,
            auto_topup_enabled, tier, and period_start.
        """
        usage = await self._store.get_usage(account_id) or AccountUsage()
        flex = await self._store.get_flex(account_id) or FlexAccount()
        quota = TIER_MONTHLY_TOKENS[usage.tier]
        return {
            "account_id": account_id,
            "tier": usage.tier.value,
            "period_start": usage.period_start.isoformat(),
            "included_used": usage.included_used,
            "included_remaining": max(0, quota - usage.included_used),
            "included_quota": quota,
            "flex_balance": flex.balance,
            "auto_topup_enabled": flex.auto_topup_enabled,
            "auto_topup_threshold": flex.auto_topup_threshold,
        }

    async def reset_period(self, account_id: str) -> None:
        """Manually reset the billing period for an account (admin action)."""
        async with self._locks[account_id]:
            usage = await self._store.get_usage(account_id) or AccountUsage()
            usage.included_used = 0
            usage.period_start = self._now_fn().date()
            await self._store.save_usage(account_id, usage)
            logger.info("period_reset", account_id=account_id)
