"""Flex balance service — pre-paid balance management with Stripe top-up.

Users pre-pay a dollar balance (stored in cents) via Stripe Payment Intents.
The balance is consumed by the UsageCascade when included tokens are exhausted.
Optional auto-topup charges the user's saved payment method when balance falls
below a configurable threshold.

Topup amounts (fixed options): $5, $10, $25, $50, $100.

References:
    - IMPL-SUP-006 §flex: Flex balance specification
    - V05-14: FlexBalanceService task definition
    - V05-13: UsageCascade (consumer of flex balance)
"""

from __future__ import annotations

import asyncio
import enum
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sovyx.engine.errors import CloudError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

VALID_TOPUP_AMOUNTS_CENTS: frozenset[int] = frozenset({500, 1000, 2500, 5000, 10000})
"""Valid top-up amounts in cents: $5, $10, $25, $50, $100."""

DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS = 200
"""Default auto-topup trigger: balance < $2.00."""

DEFAULT_AUTO_TOPUP_AMOUNT_CENTS = 1000
"""Default auto-topup charge: $10.00."""

MIN_BALANCE_CENTS = 0
"""Balance cannot go below zero."""

MAX_BALANCE_CENTS = 100_000
"""Maximum flex balance: $1,000.00 (fraud protection)."""


# ── Enums ─────────────────────────────────────────────────────────────────


class TopupStatus(enum.Enum):
    """Status of a top-up transaction."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class TransactionType(enum.Enum):
    """Type of balance transaction."""

    TOPUP = "topup"
    DEDUCTION = "deduction"
    AUTO_TOPUP = "auto_topup"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FlexBalance:
    """Current flex balance for an account.

    Attributes:
        account_id: Owner account identifier.
        balance_cents: Current balance in US cents.
        auto_topup_enabled: Whether automatic top-up is active.
        auto_topup_threshold_cents: Balance threshold triggering auto-topup.
        auto_topup_amount_cents: Amount charged on auto-topup.
        updated_at: Last balance modification timestamp.
    """

    account_id: str
    balance_cents: int = 0
    auto_topup_enabled: bool = False
    auto_topup_threshold_cents: int = DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS
    auto_topup_amount_cents: int = DEFAULT_AUTO_TOPUP_AMOUNT_CENTS
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class TopupResult:
    """Result of a top-up operation.

    Attributes:
        success: Whether the top-up was applied.
        amount_cents: Amount added (0 if failed).
        new_balance_cents: Balance after the operation.
        payment_intent_id: Stripe payment intent ID (if applicable).
        error: Error message if the operation failed.
    """

    success: bool
    amount_cents: int
    new_balance_cents: int
    payment_intent_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeductionResult:
    """Result of a balance deduction.

    Attributes:
        success: Whether the deduction was applied.
        amount_cents: Amount deducted (0 if insufficient).
        new_balance_cents: Balance after deduction.
        auto_topup_triggered: Whether auto-topup was triggered after deduction.
    """

    success: bool
    amount_cents: int
    new_balance_cents: int
    auto_topup_triggered: bool = False


@dataclass(frozen=True, slots=True)
class BalanceTransaction:
    """Audit record for a balance change.

    Attributes:
        account_id: Owner account.
        transaction_type: Type of transaction.
        amount_cents: Signed amount (+topup, -deduction).
        balance_before_cents: Balance before the transaction.
        balance_after_cents: Balance after the transaction.
        reference_id: External reference (e.g., Stripe payment intent).
        created_at: Transaction timestamp.
    """

    account_id: str
    transaction_type: TransactionType
    amount_cents: int
    balance_before_cents: int
    balance_after_cents: int
    reference_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ── Protocols ─────────────────────────────────────────────────────────────


class FlexStore(Protocol):
    """Persistent storage for flex balance data."""

    async def get_balance(self, account_id: str) -> FlexBalance | None:
        """Load balance for an account."""
        ...

    async def save_balance(self, account_id: str, balance: FlexBalance) -> None:
        """Persist balance for an account."""
        ...

    async def add_transaction(self, tx: BalanceTransaction) -> None:
        """Record a balance transaction for audit."""
        ...


class StripePaymentGateway(Protocol):
    """Stripe payment gateway for processing top-ups.

    Returns the payment intent ID on success, or raises on failure.
    """

    async def create_payment_intent(
        self,
        account_id: str,
        amount_cents: int,
        *,
        payment_method_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Create and confirm a Stripe payment intent.

        Args:
            account_id: Customer account.
            amount_cents: Amount in cents.
            payment_method_id: Optional saved payment method.
            idempotency_key: Optional Stripe idempotency key.

        Returns:
            The payment intent ID.

        Raises:
            PaymentError: On Stripe API failure.
        """
        ...

    async def charge_saved_method(
        self,
        account_id: str,
        amount_cents: int,
    ) -> str:
        """Charge the customer's saved default payment method.

        Returns:
            The payment intent ID.

        Raises:
            PaymentError: If no saved method or charge fails.
        """
        ...


# ── Exceptions ────────────────────────────────────────────────────────────


class FlexError(CloudError):
    """Base exception for flex balance operations."""


class InvalidTopupAmountError(FlexError):
    """Raised when top-up amount is not in the valid set."""


class InsufficientBalanceError(FlexError):
    """Raised when balance is insufficient for deduction."""


class MaxBalanceExceededError(FlexError):
    """Raised when top-up would exceed maximum balance."""


class PaymentError(FlexError):
    """Raised when Stripe payment fails."""


# ── In-memory store (testing / dev) ──────────────────────────────────────


class InMemoryFlexStore:
    """In-memory implementation of FlexStore for testing and development."""

    def __init__(self) -> None:
        self._balances: dict[str, FlexBalance] = {}
        self._transactions: list[BalanceTransaction] = []

    async def get_balance(self, account_id: str) -> FlexBalance | None:
        """Load balance from memory."""
        return self._balances.get(account_id)

    async def save_balance(self, account_id: str, balance: FlexBalance) -> None:
        """Save balance to memory."""
        self._balances[account_id] = balance

    async def add_transaction(self, tx: BalanceTransaction) -> None:
        """Record transaction in memory."""
        self._transactions.append(tx)

    @property
    def transactions(self) -> list[BalanceTransaction]:
        """Access recorded transactions (testing)."""
        return list(self._transactions)


# ── FlexBalanceService ────────────────────────────────────────────────────


class FlexBalanceService:
    """Manages pre-paid flex balances with Stripe top-up integration.

    Provides get_balance, deduct, and topup operations with per-account
    locking, auto-topup capability, and full transaction audit trail.

    Args:
        store: Persistent storage backend for balance data.
        gateway: Optional Stripe payment gateway for processing payments.
        now_fn: Optional clock function for testing.
    """

    def __init__(
        self,
        store: FlexStore,
        gateway: StripePaymentGateway | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    async def get_balance(self, account_id: str) -> float:
        """Get the current flex balance in dollars.

        Args:
            account_id: The account to query.

        Returns:
            Current balance as a float in USD (e.g., 10.50).
        """
        bal = await self._store.get_balance(account_id)
        if bal is None:
            return 0.0
        return bal.balance_cents / 100.0

    async def get_balance_details(self, account_id: str) -> FlexBalance:
        """Get full balance details including auto-topup config.

        Args:
            account_id: The account to query.

        Returns:
            FlexBalance with full configuration. Returns default if not found.
        """
        bal = await self._store.get_balance(account_id)
        if bal is None:
            return FlexBalance(account_id=account_id)
        return bal

    async def deduct(self, account_id: str, amount: float) -> bool:
        """Deduct from flex balance.

        Thread-safe per-account operation. If auto-topup is enabled and
        the post-deduction balance falls below threshold, an auto-topup
        is triggered asynchronously.

        Args:
            account_id: The account to deduct from.
            amount: Amount in USD to deduct (e.g., 0.50).

        Returns:
            True if deduction succeeded, False if insufficient balance.

        Raises:
            ValueError: If amount is not positive.
        """
        if amount <= 0:
            msg = f"deduction amount must be positive, got {amount}"
            raise ValueError(msg)

        amount_cents = round(amount * 100)

        async with self._locks[account_id]:
            return await self._deduct_locked(account_id, amount_cents)

    async def _deduct_locked(self, account_id: str, amount_cents: int) -> bool:
        """Execute deduction under account lock."""
        bal = await self._store.get_balance(account_id)
        if bal is None:
            bal = FlexBalance(account_id=account_id)

        if bal.balance_cents < amount_cents:
            logger.debug(
                "flex_deduct_insufficient",
                account_id=account_id,
                requested=amount_cents,
                available=bal.balance_cents,
            )
            return False

        balance_before = bal.balance_cents
        new_balance = bal.balance_cents - amount_cents

        updated = FlexBalance(
            account_id=account_id,
            balance_cents=new_balance,
            auto_topup_enabled=bal.auto_topup_enabled,
            auto_topup_threshold_cents=bal.auto_topup_threshold_cents,
            auto_topup_amount_cents=bal.auto_topup_amount_cents,
            updated_at=self._now_fn(),
        )
        await self._store.save_balance(account_id, updated)

        tx = BalanceTransaction(
            account_id=account_id,
            transaction_type=TransactionType.DEDUCTION,
            amount_cents=-amount_cents,
            balance_before_cents=balance_before,
            balance_after_cents=new_balance,
            created_at=self._now_fn(),
        )
        await self._store.add_transaction(tx)

        logger.debug(
            "flex_deduct",
            account_id=account_id,
            amount_cents=amount_cents,
            new_balance=new_balance,
        )

        # Check auto-topup threshold
        if (
            updated.auto_topup_enabled
            and new_balance < updated.auto_topup_threshold_cents
            and self._gateway is not None
        ):
            await self._auto_topup(account_id, updated)

        return True

    async def topup(
        self,
        account_id: str,
        amount: float,
        stripe_payment_intent: str,
    ) -> float:
        """Add funds from a confirmed Stripe payment.

        Args:
            account_id: The account to credit.
            amount: Amount in USD to add (must be a valid topup amount).
            stripe_payment_intent: Stripe payment intent ID for audit.

        Returns:
            New balance in USD.

        Raises:
            InvalidTopupAmountError: If amount is not in the valid set.
            MaxBalanceExceededError: If new balance would exceed maximum.
        """
        amount_cents = round(amount * 100)

        if amount_cents not in VALID_TOPUP_AMOUNTS_CENTS:
            valid = ", ".join(f"${c / 100:.0f}" for c in sorted(VALID_TOPUP_AMOUNTS_CENTS))
            msg = f"Invalid topup amount ${amount:.2f}. Valid amounts: {valid}"
            raise InvalidTopupAmountError(msg)

        async with self._locks[account_id]:
            return await self._topup_locked(account_id, amount_cents, stripe_payment_intent)

    async def _topup_locked(
        self,
        account_id: str,
        amount_cents: int,
        payment_intent_id: str,
    ) -> float:
        """Execute top-up under account lock."""
        bal = await self._store.get_balance(account_id)
        if bal is None:
            bal = FlexBalance(account_id=account_id)

        balance_before = bal.balance_cents
        new_balance = bal.balance_cents + amount_cents

        if new_balance > MAX_BALANCE_CENTS:
            msg = (
                f"Topup would exceed maximum balance. "
                f"Current: ${bal.balance_cents / 100:.2f}, "
                f"Topup: ${amount_cents / 100:.2f}, "
                f"Max: ${MAX_BALANCE_CENTS / 100:.2f}"
            )
            raise MaxBalanceExceededError(msg)

        updated = FlexBalance(
            account_id=account_id,
            balance_cents=new_balance,
            auto_topup_enabled=bal.auto_topup_enabled,
            auto_topup_threshold_cents=bal.auto_topup_threshold_cents,
            auto_topup_amount_cents=bal.auto_topup_amount_cents,
            updated_at=self._now_fn(),
        )
        await self._store.save_balance(account_id, updated)

        tx = BalanceTransaction(
            account_id=account_id,
            transaction_type=TransactionType.TOPUP,
            amount_cents=amount_cents,
            balance_before_cents=balance_before,
            balance_after_cents=new_balance,
            reference_id=payment_intent_id,
            created_at=self._now_fn(),
        )
        await self._store.add_transaction(tx)

        logger.info(
            "flex_topup",
            account_id=account_id,
            amount_cents=amount_cents,
            new_balance=new_balance,
            payment_intent_id=payment_intent_id,
        )

        return new_balance / 100.0

    async def configure_auto_topup(
        self,
        account_id: str,
        *,
        enabled: bool,
        threshold_cents: int = DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS,
        amount_cents: int = DEFAULT_AUTO_TOPUP_AMOUNT_CENTS,
    ) -> FlexBalance:
        """Configure auto-topup settings for an account.

        Args:
            account_id: The account to configure.
            enabled: Whether auto-topup should be active.
            threshold_cents: Balance threshold that triggers auto-topup.
            amount_cents: Amount to charge on each auto-topup.

        Returns:
            Updated FlexBalance.

        Raises:
            InvalidTopupAmountError: If amount is not in the valid set.
            ValueError: If threshold is negative.
        """
        if threshold_cents < 0:
            msg = f"threshold must be non-negative, got {threshold_cents}"
            raise ValueError(msg)

        if enabled and amount_cents not in VALID_TOPUP_AMOUNTS_CENTS:
            valid = ", ".join(f"${c / 100:.0f}" for c in sorted(VALID_TOPUP_AMOUNTS_CENTS))
            msg = f"Invalid auto-topup amount ${amount_cents / 100:.2f}. Valid amounts: {valid}"
            raise InvalidTopupAmountError(msg)

        async with self._locks[account_id]:
            bal = await self._store.get_balance(account_id)
            if bal is None:
                bal = FlexBalance(account_id=account_id)

            updated = FlexBalance(
                account_id=account_id,
                balance_cents=bal.balance_cents,
                auto_topup_enabled=enabled,
                auto_topup_threshold_cents=threshold_cents,
                auto_topup_amount_cents=amount_cents,
                updated_at=self._now_fn(),
            )
            await self._store.save_balance(account_id, updated)

            logger.info(
                "flex_auto_topup_configured",
                account_id=account_id,
                enabled=enabled,
                threshold_cents=threshold_cents,
                amount_cents=amount_cents,
            )

            return updated

    async def _auto_topup(
        self,
        account_id: str,
        balance: FlexBalance,
    ) -> None:
        """Trigger auto-topup using saved payment method.

        Called internally when post-deduction balance falls below threshold.
        Failures are logged but do not propagate (best-effort).
        """
        if self._gateway is None:
            return

        try:
            payment_intent_id = await self._gateway.charge_saved_method(
                account_id,
                balance.auto_topup_amount_cents,
            )

            # Re-read balance (we're still under lock)
            current = await self._store.get_balance(account_id)
            if current is None:
                current = FlexBalance(account_id=account_id)

            balance_before = current.balance_cents
            new_balance = current.balance_cents + balance.auto_topup_amount_cents

            if new_balance > MAX_BALANCE_CENTS:
                logger.warning(
                    "flex_auto_topup_max_exceeded",
                    account_id=account_id,
                    current=current.balance_cents,
                    topup=balance.auto_topup_amount_cents,
                )
                return

            updated = FlexBalance(
                account_id=account_id,
                balance_cents=new_balance,
                auto_topup_enabled=current.auto_topup_enabled,
                auto_topup_threshold_cents=current.auto_topup_threshold_cents,
                auto_topup_amount_cents=current.auto_topup_amount_cents,
                updated_at=self._now_fn(),
            )
            await self._store.save_balance(account_id, updated)

            tx = BalanceTransaction(
                account_id=account_id,
                transaction_type=TransactionType.AUTO_TOPUP,
                amount_cents=balance.auto_topup_amount_cents,
                balance_before_cents=balance_before,
                balance_after_cents=new_balance,
                reference_id=payment_intent_id,
                created_at=self._now_fn(),
            )
            await self._store.add_transaction(tx)

            logger.info(
                "flex_auto_topup_success",
                account_id=account_id,
                amount_cents=balance.auto_topup_amount_cents,
                new_balance=new_balance,
                payment_intent_id=payment_intent_id,
            )

        except Exception:
            logger.exception(
                "flex_auto_topup_failed",
                account_id=account_id,
                amount_cents=balance.auto_topup_amount_cents,
            )

    async def get_status(self, account_id: str) -> dict[str, Any]:
        """Return full flex balance status for an account.

        Returns:
            Dict with balance, auto-topup config, and valid topup amounts.
        """
        bal = await self._store.get_balance(account_id)
        if bal is None:
            bal = FlexBalance(account_id=account_id)

        return {
            "account_id": account_id,
            "balance_cents": bal.balance_cents,
            "balance_usd": bal.balance_cents / 100.0,
            "auto_topup_enabled": bal.auto_topup_enabled,
            "auto_topup_threshold_cents": bal.auto_topup_threshold_cents,
            "auto_topup_amount_cents": bal.auto_topup_amount_cents,
            "valid_topup_amounts_cents": sorted(VALID_TOPUP_AMOUNTS_CENTS),
            "max_balance_cents": MAX_BALANCE_CENTS,
            "updated_at": bal.updated_at.isoformat(),
        }
