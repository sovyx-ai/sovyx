"""Tests for FlexBalanceService (V05-14).

Covers: get_balance, deduct, topup, auto-topup, configure_auto_topup,
error paths, concurrent access, audit transactions, and edge cases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.flex import (
    DEFAULT_AUTO_TOPUP_AMOUNT_CENTS,
    DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS,
    MAX_BALANCE_CENTS,
    MIN_BALANCE_CENTS,
    VALID_TOPUP_AMOUNTS_CENTS,
    BalanceTransaction,
    FlexBalance,
    FlexBalanceService,
    FlexError,
    InMemoryFlexStore,
    InsufficientBalanceError,
    InvalidTopupAmountError,
    MaxBalanceExceededError,
    PaymentError,
    TopupResult,
    TopupStatus,
    TransactionType,
)

# ── Fixtures ──────────────────────────────────────────────────────────────

FIXED_NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)


def _make_service(
    store: InMemoryFlexStore | None = None,
    gateway: AsyncMock | None = None,
) -> tuple[FlexBalanceService, InMemoryFlexStore]:
    """Create a FlexBalanceService with in-memory store and optional mock gateway."""
    s = store or InMemoryFlexStore()
    svc = FlexBalanceService(s, gateway, now_fn=lambda: FIXED_NOW)
    return svc, s


async def _seed_balance(
    store: InMemoryFlexStore,
    account_id: str,
    balance_cents: int,
    *,
    auto_topup_enabled: bool = False,
    auto_topup_threshold_cents: int = DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS,
    auto_topup_amount_cents: int = DEFAULT_AUTO_TOPUP_AMOUNT_CENTS,
) -> None:
    """Seed a balance into the store for testing."""
    await store.save_balance(
        account_id,
        FlexBalance(
            account_id=account_id,
            balance_cents=balance_cents,
            auto_topup_enabled=auto_topup_enabled,
            auto_topup_threshold_cents=auto_topup_threshold_cents,
            auto_topup_amount_cents=auto_topup_amount_cents,
            updated_at=FIXED_NOW,
        ),
    )


# ── Data class tests ─────────────────────────────────────────────────────


class TestDataClasses:
    """Tests for frozen data classes and enums."""

    def test_flex_balance_defaults(self) -> None:
        bal = FlexBalance(account_id="acc-1")
        assert bal.balance_cents == 0
        assert bal.auto_topup_enabled is False
        assert bal.auto_topup_threshold_cents == DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS
        assert bal.auto_topup_amount_cents == DEFAULT_AUTO_TOPUP_AMOUNT_CENTS

    def test_flex_balance_frozen(self) -> None:
        bal = FlexBalance(account_id="acc-1", balance_cents=500)
        with pytest.raises(AttributeError):
            bal.balance_cents = 600  # type: ignore[misc]

    def test_topup_result_frozen(self) -> None:
        tr = TopupResult(success=True, amount_cents=1000, new_balance_cents=1000)
        with pytest.raises(AttributeError):
            tr.success = False  # type: ignore[misc]

    def test_balance_transaction_fields(self) -> None:
        tx = BalanceTransaction(
            account_id="acc-1",
            transaction_type=TransactionType.TOPUP,
            amount_cents=1000,
            balance_before_cents=0,
            balance_after_cents=1000,
            reference_id="pi_test123",
        )
        assert tx.reference_id == "pi_test123"
        assert tx.transaction_type == TransactionType.TOPUP

    def test_topup_status_values(self) -> None:
        assert TopupStatus.COMPLETED.value == "completed"
        assert TopupStatus.PENDING.value == "pending"
        assert TopupStatus.FAILED.value == "failed"
        assert TopupStatus.REFUNDED.value == "refunded"

    def test_transaction_type_values(self) -> None:
        assert TransactionType.TOPUP.value == "topup"
        assert TransactionType.DEDUCTION.value == "deduction"
        assert TransactionType.AUTO_TOPUP.value == "auto_topup"
        assert TransactionType.REFUND.value == "refund"
        assert TransactionType.ADJUSTMENT.value == "adjustment"

    def test_valid_topup_amounts(self) -> None:
        assert {500, 1000, 2500, 5000, 10000} == VALID_TOPUP_AMOUNTS_CENTS

    def test_constants(self) -> None:
        assert MIN_BALANCE_CENTS == 0
        assert MAX_BALANCE_CENTS == 100_000
        assert DEFAULT_AUTO_TOPUP_THRESHOLD_CENTS == 200
        assert DEFAULT_AUTO_TOPUP_AMOUNT_CENTS == 1000


# ── InMemoryFlexStore tests ──────────────────────────────────────────────


class TestInMemoryFlexStore:
    """Tests for InMemoryFlexStore."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self) -> None:
        store = InMemoryFlexStore()
        assert await store.get_balance("nonexistent") is None

    @pytest.mark.asyncio
    async def test_save_and_get(self) -> None:
        store = InMemoryFlexStore()
        bal = FlexBalance(account_id="acc-1", balance_cents=5000)
        await store.save_balance("acc-1", bal)
        result = await store.get_balance("acc-1")
        assert result is not None
        assert result.balance_cents == 5000

    @pytest.mark.asyncio
    async def test_transactions_recorded(self) -> None:
        store = InMemoryFlexStore()
        tx = BalanceTransaction(
            account_id="acc-1",
            transaction_type=TransactionType.TOPUP,
            amount_cents=1000,
            balance_before_cents=0,
            balance_after_cents=1000,
        )
        await store.add_transaction(tx)
        assert len(store.transactions) == 1
        assert store.transactions[0].amount_cents == 1000


# ── FlexBalanceService: get_balance ──────────────────────────────────────


class TestGetBalance:
    """Tests for FlexBalanceService.get_balance."""

    @pytest.mark.asyncio
    async def test_nonexistent_account_returns_zero(self) -> None:
        svc, _ = _make_service()
        balance = await svc.get_balance("acc-new")
        assert balance == 0.0

    @pytest.mark.asyncio
    async def test_existing_balance_in_dollars(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 5050)
        balance = await svc.get_balance("acc-1")
        assert balance == 50.50

    @pytest.mark.asyncio
    async def test_get_balance_details_default(self) -> None:
        svc, _ = _make_service()
        details = await svc.get_balance_details("acc-new")
        assert details.account_id == "acc-new"
        assert details.balance_cents == 0
        assert details.auto_topup_enabled is False

    @pytest.mark.asyncio
    async def test_get_balance_details_existing(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 2500, auto_topup_enabled=True)
        details = await svc.get_balance_details("acc-1")
        assert details.balance_cents == 2500
        assert details.auto_topup_enabled is True


# ── FlexBalanceService: topup ────────────────────────────────────────────


class TestTopup:
    """Tests for FlexBalanceService.topup."""

    @pytest.mark.asyncio
    async def test_topup_5_dollars(self) -> None:
        svc, store = _make_service()
        new_bal = await svc.topup("acc-1", 5.0, "pi_test_5")
        assert new_bal == 5.0
        bal = await store.get_balance("acc-1")
        assert bal is not None
        assert bal.balance_cents == 500

    @pytest.mark.asyncio
    async def test_topup_10_dollars(self) -> None:
        svc, store = _make_service()
        new_bal = await svc.topup("acc-1", 10.0, "pi_test_10")
        assert new_bal == 10.0

    @pytest.mark.asyncio
    async def test_topup_25_dollars(self) -> None:
        svc, _ = _make_service()
        new_bal = await svc.topup("acc-1", 25.0, "pi_test_25")
        assert new_bal == 25.0

    @pytest.mark.asyncio
    async def test_topup_50_dollars(self) -> None:
        svc, _ = _make_service()
        new_bal = await svc.topup("acc-1", 50.0, "pi_test_50")
        assert new_bal == 50.0

    @pytest.mark.asyncio
    async def test_topup_100_dollars(self) -> None:
        svc, _ = _make_service()
        new_bal = await svc.topup("acc-1", 100.0, "pi_test_100")
        assert new_bal == 100.0

    @pytest.mark.asyncio
    async def test_topup_adds_to_existing_balance(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 2500)
        new_bal = await svc.topup("acc-1", 10.0, "pi_test_add")
        assert new_bal == 35.0  # 25 + 10

    @pytest.mark.asyncio
    async def test_topup_invalid_amount_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(InvalidTopupAmountError, match="Invalid topup amount"):
            await svc.topup("acc-1", 7.0, "pi_test_invalid")

    @pytest.mark.asyncio
    async def test_topup_zero_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(InvalidTopupAmountError):
            await svc.topup("acc-1", 0.0, "pi_test_zero")

    @pytest.mark.asyncio
    async def test_topup_negative_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(InvalidTopupAmountError):
            await svc.topup("acc-1", -5.0, "pi_test_neg")

    @pytest.mark.asyncio
    async def test_topup_exceeds_max_balance_raises(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", MAX_BALANCE_CENTS - 100)
        with pytest.raises(MaxBalanceExceededError, match="exceed maximum"):
            await svc.topup("acc-1", 5.0, "pi_test_overflow")

    @pytest.mark.asyncio
    async def test_topup_records_transaction(self) -> None:
        svc, store = _make_service()
        await svc.topup("acc-1", 10.0, "pi_tx_audit")
        assert len(store.transactions) == 1
        tx = store.transactions[0]
        assert tx.transaction_type == TransactionType.TOPUP
        assert tx.amount_cents == 1000
        assert tx.balance_before_cents == 0
        assert tx.balance_after_cents == 1000
        assert tx.reference_id == "pi_tx_audit"
        assert tx.account_id == "acc-1"

    @pytest.mark.asyncio
    async def test_topup_preserves_auto_topup_config(self) -> None:
        svc, store = _make_service()
        await _seed_balance(
            store, "acc-1", 500, auto_topup_enabled=True, auto_topup_threshold_cents=300
        )
        await svc.topup("acc-1", 10.0, "pi_preserve")
        bal = await store.get_balance("acc-1")
        assert bal is not None
        assert bal.auto_topup_enabled is True
        assert bal.auto_topup_threshold_cents == 300


# ── FlexBalanceService: deduct ───────────────────────────────────────────


class TestDeduct:
    """Tests for FlexBalanceService.deduct."""

    @pytest.mark.asyncio
    async def test_deduct_from_funded_account(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 5000)
        result = await svc.deduct("acc-1", 10.0)
        assert result is True
        assert await svc.get_balance("acc-1") == 40.0

    @pytest.mark.asyncio
    async def test_deduct_exact_balance(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 1000)
        result = await svc.deduct("acc-1", 10.0)
        assert result is True
        assert await svc.get_balance("acc-1") == 0.0

    @pytest.mark.asyncio
    async def test_deduct_insufficient_balance(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 500)
        result = await svc.deduct("acc-1", 10.0)
        assert result is False
        # Balance unchanged
        assert await svc.get_balance("acc-1") == 5.0

    @pytest.mark.asyncio
    async def test_deduct_from_empty_account(self) -> None:
        svc, _ = _make_service()
        result = await svc.deduct("acc-new", 1.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_deduct_zero_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(ValueError, match="positive"):
            await svc.deduct("acc-1", 0.0)

    @pytest.mark.asyncio
    async def test_deduct_negative_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(ValueError, match="positive"):
            await svc.deduct("acc-1", -5.0)

    @pytest.mark.asyncio
    async def test_deduct_records_transaction(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 5000)
        await svc.deduct("acc-1", 10.0)
        assert len(store.transactions) == 1
        tx = store.transactions[0]
        assert tx.transaction_type == TransactionType.DEDUCTION
        assert tx.amount_cents == -1000
        assert tx.balance_before_cents == 5000
        assert tx.balance_after_cents == 4000

    @pytest.mark.asyncio
    async def test_deduct_small_amount(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 100)
        result = await svc.deduct("acc-1", 0.50)
        assert result is True
        assert await svc.get_balance("acc-1") == 0.50

    @pytest.mark.asyncio
    async def test_deduct_no_transaction_on_failure(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 100)
        await svc.deduct("acc-1", 10.0)  # insufficient
        assert len(store.transactions) == 0


# ── FlexBalanceService: auto-topup ───────────────────────────────────────


class TestAutoTopup:
    """Tests for auto-topup triggered by deduction."""

    @pytest.mark.asyncio
    async def test_auto_topup_triggered_on_low_balance(self) -> None:
        gateway = AsyncMock()
        gateway.charge_saved_method = AsyncMock(return_value="pi_auto_1")
        svc, store = _make_service(gateway=gateway)
        await _seed_balance(
            store,
            "acc-1",
            500,  # $5 balance
            auto_topup_enabled=True,
            auto_topup_threshold_cents=300,  # threshold $3
            auto_topup_amount_cents=1000,  # auto-topup $10
        )
        # Deduct $3.50 → balance becomes $1.50 (< $3 threshold)
        result = await svc.deduct("acc-1", 3.50)
        assert result is True
        gateway.charge_saved_method.assert_awaited_once_with("acc-1", 1000)
        # Balance = $1.50 + $10 = $11.50
        assert await svc.get_balance("acc-1") == 11.50

    @pytest.mark.asyncio
    async def test_auto_topup_not_triggered_above_threshold(self) -> None:
        gateway = AsyncMock()
        svc, store = _make_service(gateway=gateway)
        await _seed_balance(
            store,
            "acc-1",
            5000,
            auto_topup_enabled=True,
            auto_topup_threshold_cents=200,
        )
        await svc.deduct("acc-1", 10.0)
        # Balance = $40, well above $2 threshold
        gateway.charge_saved_method.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_topup_disabled(self) -> None:
        gateway = AsyncMock()
        svc, store = _make_service(gateway=gateway)
        await _seed_balance(store, "acc-1", 300, auto_topup_enabled=False)
        await svc.deduct("acc-1", 2.0)
        gateway.charge_saved_method.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_topup_no_gateway(self) -> None:
        svc, store = _make_service(gateway=None)
        await _seed_balance(
            store, "acc-1", 300, auto_topup_enabled=True, auto_topup_threshold_cents=200
        )
        # Should not raise, just skip
        result = await svc.deduct("acc-1", 2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_auto_topup_gateway_failure_logged_not_raised(self) -> None:
        gateway = AsyncMock()
        gateway.charge_saved_method = AsyncMock(side_effect=PaymentError("card declined"))
        svc, store = _make_service(gateway=gateway)
        await _seed_balance(
            store,
            "acc-1",
            500,
            auto_topup_enabled=True,
            auto_topup_threshold_cents=300,
            auto_topup_amount_cents=1000,
        )
        # Deduct should succeed even if auto-topup fails
        result = await svc.deduct("acc-1", 3.50)
        assert result is True
        # Balance is just the deducted amount, no auto-topup credit
        assert await svc.get_balance("acc-1") == 1.50

    @pytest.mark.asyncio
    async def test_auto_topup_records_transaction(self) -> None:
        gateway = AsyncMock()
        gateway.charge_saved_method = AsyncMock(return_value="pi_auto_tx")
        svc, store = _make_service(gateway=gateway)
        await _seed_balance(
            store,
            "acc-1",
            500,
            auto_topup_enabled=True,
            auto_topup_threshold_cents=300,
            auto_topup_amount_cents=1000,
        )
        await svc.deduct("acc-1", 3.50)
        # Should have deduction + auto-topup transaction
        assert len(store.transactions) == 2
        deduct_tx = store.transactions[0]
        topup_tx = store.transactions[1]
        assert deduct_tx.transaction_type == TransactionType.DEDUCTION
        assert topup_tx.transaction_type == TransactionType.AUTO_TOPUP
        assert topup_tx.reference_id == "pi_auto_tx"

    @pytest.mark.asyncio
    async def test_auto_topup_skipped_if_would_exceed_max(self) -> None:
        gateway = AsyncMock()
        gateway.charge_saved_method = AsyncMock(return_value="pi_auto_skip")
        svc, store = _make_service(gateway=gateway)
        # Balance near max
        await _seed_balance(
            store,
            "acc-1",
            MAX_BALANCE_CENTS - 50,
            auto_topup_enabled=True,
            auto_topup_threshold_cents=MAX_BALANCE_CENTS,  # always triggers
            auto_topup_amount_cents=500,
        )
        await svc.deduct("acc-1", 0.30)
        # Gateway called but balance not updated (would exceed max)
        gateway.charge_saved_method.assert_awaited_once()
        # Balance should just be deducted, not topped up
        bal = await store.get_balance("acc-1")
        assert bal is not None
        assert bal.balance_cents == MAX_BALANCE_CENTS - 50 - 30


# ── FlexBalanceService: configure_auto_topup ─────────────────────────────


class TestConfigureAutoTopup:
    """Tests for FlexBalanceService.configure_auto_topup."""

    @pytest.mark.asyncio
    async def test_enable_auto_topup(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 1000)
        result = await svc.configure_auto_topup(
            "acc-1", enabled=True, threshold_cents=500, amount_cents=2500
        )
        assert result.auto_topup_enabled is True
        assert result.auto_topup_threshold_cents == 500
        assert result.auto_topup_amount_cents == 2500
        assert result.balance_cents == 1000  # unchanged

    @pytest.mark.asyncio
    async def test_disable_auto_topup(self) -> None:
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 1000, auto_topup_enabled=True)
        result = await svc.configure_auto_topup("acc-1", enabled=False)
        assert result.auto_topup_enabled is False

    @pytest.mark.asyncio
    async def test_configure_nonexistent_account(self) -> None:
        svc, _ = _make_service()
        result = await svc.configure_auto_topup("acc-new", enabled=True, amount_cents=1000)
        assert result.auto_topup_enabled is True
        assert result.balance_cents == 0

    @pytest.mark.asyncio
    async def test_configure_negative_threshold_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(ValueError, match="non-negative"):
            await svc.configure_auto_topup("acc-1", enabled=True, threshold_cents=-100)

    @pytest.mark.asyncio
    async def test_configure_invalid_amount_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(InvalidTopupAmountError):
            await svc.configure_auto_topup(
                "acc-1", enabled=True, amount_cents=777
            )

    @pytest.mark.asyncio
    async def test_disable_with_invalid_amount_ok(self) -> None:
        """When disabling, amount validation is skipped."""
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 1000, auto_topup_enabled=True)
        # Disabling doesn't validate amount
        result = await svc.configure_auto_topup(
            "acc-1", enabled=False, amount_cents=777
        )
        assert result.auto_topup_enabled is False


# ── FlexBalanceService: get_status ───────────────────────────────────────


class TestGetStatus:
    """Tests for FlexBalanceService.get_status."""

    @pytest.mark.asyncio
    async def test_status_new_account(self) -> None:
        svc, _ = _make_service()
        status = await svc.get_status("acc-new")
        assert status["account_id"] == "acc-new"
        assert status["balance_cents"] == 0
        assert status["balance_usd"] == 0.0
        assert status["auto_topup_enabled"] is False
        assert status["max_balance_cents"] == MAX_BALANCE_CENTS
        assert status["valid_topup_amounts_cents"] == sorted(VALID_TOPUP_AMOUNTS_CENTS)

    @pytest.mark.asyncio
    async def test_status_funded_account(self) -> None:
        svc, store = _make_service()
        await _seed_balance(
            store, "acc-1", 5000, auto_topup_enabled=True, auto_topup_threshold_cents=300
        )
        status = await svc.get_status("acc-1")
        assert status["balance_cents"] == 5000
        assert status["balance_usd"] == 50.0
        assert status["auto_topup_enabled"] is True
        assert status["auto_topup_threshold_cents"] == 300


# ── Concurrent access ────────────────────────────────────────────────────


class TestConcurrency:
    """Tests for thread-safety via per-account locks."""

    @pytest.mark.asyncio
    async def test_concurrent_deductions(self) -> None:
        """Multiple concurrent deductions should not overdraw."""
        import asyncio

        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 1000)  # $10

        # 20 concurrent deductions of $0.60 each = $12 total
        # Only ~16 should succeed ($10 / $0.60 ≈ 16)
        results = await asyncio.gather(
            *[svc.deduct("acc-1", 0.60) for _ in range(20)]
        )
        success_count = sum(1 for r in results if r is True)
        final_balance = await svc.get_balance("acc-1")

        # Balance must never go negative
        assert final_balance >= 0.0
        # At most 16 should succeed (1000 / 60 = 16.66)
        assert success_count <= 17
        assert success_count >= 1

    @pytest.mark.asyncio
    async def test_concurrent_topup_and_deduct(self) -> None:
        """Concurrent topup and deduct should maintain consistency."""
        import asyncio

        svc, store = _make_service()
        await _seed_balance(store, "acc-1", 5000)

        async def topup_task() -> float:
            return await svc.topup("acc-1", 10.0, "pi_concurrent")

        async def deduct_task() -> bool:
            return await svc.deduct("acc-1", 5.0)

        await asyncio.gather(topup_task(), deduct_task())
        final = await svc.get_balance("acc-1")
        # Started at $50, added $10, deducted $5 = $55
        assert final == 55.0


# ── Integration: topup + deduct flow ─────────────────────────────────────


class TestIntegrationFlow:
    """End-to-end balance lifecycle tests."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Topup → deductions → auto-topup → check status."""
        gateway = AsyncMock()
        gateway.charge_saved_method = AsyncMock(return_value="pi_lifecycle")
        svc, store = _make_service(gateway=gateway)

        # 1. Initial top-up
        bal = await svc.topup("acc-1", 25.0, "pi_initial")
        assert bal == 25.0

        # 2. Configure auto-topup
        await svc.configure_auto_topup(
            "acc-1",
            enabled=True,
            threshold_cents=500,
            amount_cents=1000,
        )

        # 3. Series of deductions
        assert await svc.deduct("acc-1", 5.0) is True  # $20 remaining
        assert await svc.deduct("acc-1", 10.0) is True  # $10 remaining
        assert await svc.deduct("acc-1", 7.0) is True  # $3 → auto-topup triggered

        # 4. Auto-topup should have fired
        gateway.charge_saved_method.assert_awaited_once()

        # 5. Check status
        status = await svc.get_status("acc-1")
        assert status["balance_cents"] == 1300  # $3 + $10 = $13
        assert status["auto_topup_enabled"] is True

        # 6. Verify transaction audit trail
        assert len(store.transactions) == 5  # topup + 3 deductions + auto-topup

    @pytest.mark.asyncio
    async def test_multiple_topups(self) -> None:
        svc, _ = _make_service()
        await svc.topup("acc-1", 5.0, "pi_1")
        await svc.topup("acc-1", 10.0, "pi_2")
        await svc.topup("acc-1", 25.0, "pi_3")
        assert await svc.get_balance("acc-1") == 40.0


# ── Property-based tests ─────────────────────────────────────────────────


class TestPropertyBased:
    """Hypothesis property-based tests for invariants."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(amount_cents=st.sampled_from(sorted(VALID_TOPUP_AMOUNTS_CENTS)))
    @pytest.mark.asyncio
    async def test_topup_always_increases_balance(self, amount_cents: int) -> None:
        """Topup always increases balance by exactly the topup amount."""
        svc, store = _make_service()
        initial = 1000
        await _seed_balance(store, "acc-1", initial)
        new_bal = await svc.topup("acc-1", amount_cents / 100, f"pi_prop_{amount_cents}")
        assert new_bal == (initial + amount_cents) / 100

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        balance=st.integers(min_value=100, max_value=50000),
        deduct_cents=st.integers(min_value=1, max_value=50000),
    )
    @pytest.mark.asyncio
    async def test_deduct_never_goes_negative(
        self, balance: int, deduct_cents: int
    ) -> None:
        """Balance never goes below zero after deduction."""
        svc, store = _make_service()
        await _seed_balance(store, "acc-1", balance)
        await svc.deduct("acc-1", deduct_cents / 100)
        final = await svc.get_balance("acc-1")
        assert final >= 0.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(invalid_cents=st.integers(min_value=1, max_value=99999).filter(
        lambda x: x not in VALID_TOPUP_AMOUNTS_CENTS
    ))
    @pytest.mark.asyncio
    async def test_invalid_topup_always_rejected(self, invalid_cents: int) -> None:
        """Non-valid topup amounts are always rejected."""
        svc, _ = _make_service()
        with pytest.raises(InvalidTopupAmountError):
            await svc.topup("acc-1", invalid_cents / 100, "pi_invalid")


# ── Exception hierarchy ──────────────────────────────────────────────────


class TestExceptions:
    """Tests for exception types."""

    def test_flex_error_is_base(self) -> None:
        assert issubclass(InvalidTopupAmountError, FlexError)
        assert issubclass(InsufficientBalanceError, FlexError)
        assert issubclass(MaxBalanceExceededError, FlexError)
        assert issubclass(PaymentError, FlexError)

    def test_exception_messages(self) -> None:
        err = InvalidTopupAmountError("bad amount")
        assert str(err) == "bad amount"
