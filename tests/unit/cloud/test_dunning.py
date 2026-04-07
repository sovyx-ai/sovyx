"""Tests for DunningService (V05-15).

Covers the 4-email dunning sequence, state machine transitions,
smart retry backoff, payment recovery, and edge cases.
"""

from __future__ import annotations

import time

import pytest

from sovyx.cloud.dunning import (
    DUNNING_EMAILS,
    EMAIL_SCHEDULE_DAYS,
    GRACE_PERIOD_DAYS,
    RETRY_DELAYS_SECONDS,
    STATE_EMAIL_MAP,
    CustomerResolver,
    DunningEmail,
    DunningRecord,
    DunningService,
    DunningState,
    DunningStore,
    EmailSender,
    EmailType,
    InMemoryCustomerResolver,
    InMemoryDunningStore,
    InMemoryEmailSender,
    NoopSubscriptionDowngrader,
    SubscriptionDowngrader,
    _days_to_state,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> InMemoryDunningStore:
    return InMemoryDunningStore()


@pytest.fixture()
def email_sender() -> InMemoryEmailSender:
    return InMemoryEmailSender()


@pytest.fixture()
def downgrader() -> NoopSubscriptionDowngrader:
    return NoopSubscriptionDowngrader()


@pytest.fixture()
def resolver() -> InMemoryCustomerResolver:
    return InMemoryCustomerResolver(
        {
            "cus_123": "user@example.com",
            "cus_456": "other@example.com",
        }
    )


class _FakeClock:
    """Controllable clock for testing time-dependent dunning logic."""

    def __init__(self, start: float | None = None) -> None:
        self._now = start or time.time()

    def __call__(self) -> float:
        return self._now

    def advance_days(self, days: float) -> None:
        self._now += days * 86400

    def advance_hours(self, hours: float) -> None:
        self._now += hours * 3600


@pytest.fixture()
def clock() -> _FakeClock:
    return _FakeClock(start=1_700_000_000.0)


@pytest.fixture()
def service(
    store: InMemoryDunningStore,
    email_sender: InMemoryEmailSender,
    resolver: InMemoryCustomerResolver,
    downgrader: NoopSubscriptionDowngrader,
    clock: _FakeClock,
) -> DunningService:
    return DunningService(
        store=store,
        email_sender=email_sender,
        customer_resolver=resolver,
        downgrader=downgrader,
        now_fn=clock,
    )


# ── DunningState enum ───────────────────────────────────────────────────


class TestDunningState:
    def test_all_states_have_values(self) -> None:
        assert len(DunningState) == 6

    def test_state_values(self) -> None:
        assert DunningState.ACTIVE.value == "active"
        assert DunningState.PAST_DUE_DAY1.value == "past_due_day1"
        assert DunningState.PAST_DUE_DAY3.value == "past_due_day3"
        assert DunningState.PAST_DUE_DAY7.value == "past_due_day7"
        assert DunningState.PAST_DUE_DAY14.value == "past_due_day14"
        assert DunningState.CANCELED.value == "canceled"


# ── EmailType enum ───────────────────────────────────────────────────────


class TestEmailType:
    def test_all_email_types(self) -> None:
        assert len(EmailType) == 4

    def test_email_type_values(self) -> None:
        assert EmailType.FRIENDLY_REMINDER.value == "friendly_reminder"
        assert EmailType.ACTION_NEEDED.value == "action_needed"
        assert EmailType.SERVICE_AT_RISK.value == "service_at_risk"
        assert EmailType.FINAL_NOTICE.value == "final_notice"


# ── Constants ────────────────────────────────────────────────────────────


class TestConstants:
    def test_retry_delays(self) -> None:
        assert RETRY_DELAYS_SECONDS == (3600, 14400, 86400, 259200)

    def test_email_schedule(self) -> None:
        assert EMAIL_SCHEDULE_DAYS == (1, 3, 7, 14)

    def test_grace_period(self) -> None:
        assert GRACE_PERIOD_DAYS == 14

    def test_state_email_map_complete(self) -> None:
        """All PAST_DUE states map to an email type."""
        past_due_states = [s for s in DunningState if s.value.startswith("past_due")]
        for state in past_due_states:
            assert state in STATE_EMAIL_MAP

    def test_dunning_emails_complete(self) -> None:
        """All email types have a template."""
        for email_type in EmailType:
            assert email_type in DUNNING_EMAILS
            email = DUNNING_EMAILS[email_type]
            assert email.subject
            assert email.template_id


# ── _days_to_state helper ────────────────────────────────────────────────


class TestDaysToState:
    def test_day_0(self) -> None:
        assert _days_to_state(0) == DunningState.ACTIVE

    def test_day_1(self) -> None:
        assert _days_to_state(1) == DunningState.PAST_DUE_DAY1

    def test_day_2(self) -> None:
        assert _days_to_state(2) == DunningState.PAST_DUE_DAY1

    def test_day_3(self) -> None:
        assert _days_to_state(3) == DunningState.PAST_DUE_DAY3

    def test_day_5(self) -> None:
        assert _days_to_state(5) == DunningState.PAST_DUE_DAY3

    def test_day_7(self) -> None:
        assert _days_to_state(7) == DunningState.PAST_DUE_DAY7

    def test_day_10(self) -> None:
        assert _days_to_state(10) == DunningState.PAST_DUE_DAY7

    def test_day_14(self) -> None:
        assert _days_to_state(14) == DunningState.PAST_DUE_DAY14

    def test_day_30(self) -> None:
        assert _days_to_state(30) == DunningState.PAST_DUE_DAY14


# ── DunningRecord ────────────────────────────────────────────────────────


class TestDunningRecord:
    def test_default_values(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
        )
        assert record.state == DunningState.PAST_DUE_DAY1
        assert record.retry_count == 0
        assert record.emails_sent == []

    def test_next_retry_delay_first(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            retry_count=0,
        )
        assert record.next_retry_delay == 3600  # 1 hour

    def test_next_retry_delay_second(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            retry_count=1,
        )
        assert record.next_retry_delay == 14400  # 4 hours

    def test_next_retry_delay_caps_at_max(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            retry_count=100,
        )
        assert record.next_retry_delay == RETRY_DELAYS_SECONDS[-1]

    def test_should_retry_no_last_retry(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            last_retry_at=0.0,
        )
        assert record.should_retry is True

    def test_should_retry_canceled(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            state=DunningState.CANCELED,
        )
        assert record.should_retry is False

    def test_should_retry_exhausted(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            retry_count=len(RETRY_DELAYS_SECONDS),
        )
        assert record.should_retry is False

    def test_days_elapsed_zero_when_no_failure(self) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            first_failed_at=0.0,
        )
        assert record.days_elapsed == 0.0


# ── DunningEmail dataclass ───────────────────────────────────────────────


class TestDunningEmail:
    def test_frozen(self) -> None:
        email = DunningEmail(
            email_type=EmailType.FRIENDLY_REMINDER,
            subject="Test",
            template_id="test",
        )
        with pytest.raises(AttributeError):
            email.subject = "changed"  # type: ignore[misc]

    def test_metadata_default(self) -> None:
        email = DunningEmail(
            email_type=EmailType.FRIENDLY_REMINDER,
            subject="Test",
            template_id="test",
        )
        assert email.metadata == {}


# ── InMemoryDunningStore ─────────────────────────────────────────────────


class TestInMemoryDunningStore:
    @pytest.mark.asyncio()
    async def test_get_missing(self, store: InMemoryDunningStore) -> None:
        result = await store.get("sub_missing")
        assert result is None

    @pytest.mark.asyncio()
    async def test_save_and_get(self, store: InMemoryDunningStore) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
        )
        await store.save(record)
        got = await store.get("sub_1")
        assert got is not None
        assert got.subscription_id == "sub_1"

    @pytest.mark.asyncio()
    async def test_delete_existing(self, store: InMemoryDunningStore) -> None:
        record = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
        )
        await store.save(record)
        assert await store.delete("sub_1") is True
        assert await store.get("sub_1") is None

    @pytest.mark.asyncio()
    async def test_delete_missing(self, store: InMemoryDunningStore) -> None:
        assert await store.delete("sub_nope") is False

    @pytest.mark.asyncio()
    async def test_list_active_filters_canceled(
        self,
        store: InMemoryDunningStore,
    ) -> None:
        active = DunningRecord(
            subscription_id="sub_1",
            customer_id="cus_1",
            invoice_id="inv_1",
            state=DunningState.PAST_DUE_DAY1,
        )
        canceled = DunningRecord(
            subscription_id="sub_2",
            customer_id="cus_2",
            invoice_id="inv_2",
            state=DunningState.CANCELED,
        )
        await store.save(active)
        await store.save(canceled)
        result = await store.list_active()
        assert len(result) == 1
        assert result[0].subscription_id == "sub_1"


# ── InMemoryEmailSender ─────────────────────────────────────────────────


class TestInMemoryEmailSender:
    @pytest.mark.asyncio()
    async def test_records_sent(self, email_sender: InMemoryEmailSender) -> None:
        ok = await email_sender.send("a@b.com", "Subject", "tpl", {"key": "val"})
        assert ok is True
        assert len(email_sender.sent) == 1
        assert email_sender.sent[0]["to"] == "a@b.com"
        assert email_sender.sent[0]["template_id"] == "tpl"


# ── NoopSubscriptionDowngrader ───────────────────────────────────────────


class TestNoopSubscriptionDowngrader:
    @pytest.mark.asyncio()
    async def test_records_downgrade(
        self,
        downgrader: NoopSubscriptionDowngrader,
    ) -> None:
        ok = await downgrader.downgrade_to_free("sub_1", "cus_1")
        assert ok is True
        assert len(downgrader.downgrades) == 1
        assert downgrader.downgrades[0]["subscription_id"] == "sub_1"


# ── InMemoryCustomerResolver ────────────────────────────────────────────


class TestInMemoryCustomerResolver:
    @pytest.mark.asyncio()
    async def test_found(self, resolver: InMemoryCustomerResolver) -> None:
        email = await resolver.get_email("cus_123")
        assert email == "user@example.com"

    @pytest.mark.asyncio()
    async def test_not_found(self, resolver: InMemoryCustomerResolver) -> None:
        email = await resolver.get_email("cus_unknown")
        assert email is None


# ── DunningService — handle_payment_failed ───────────────────────────────


class TestHandlePaymentFailed:
    @pytest.mark.asyncio()
    async def test_creates_record_on_first_failure(
        self,
        service: DunningService,
        store: InMemoryDunningStore,
    ) -> None:
        record = await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        assert record.state == DunningState.PAST_DUE_DAY1
        assert record.retry_count == 0

        stored = await store.get("sub_1")
        assert stored is not None

    @pytest.mark.asyncio()
    async def test_sends_day1_email_on_first_failure(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        assert len(email_sender.sent) == 1
        assert email_sender.sent[0]["template_id"] == "dunning_day1"
        assert email_sender.sent[0]["to"] == "user@example.com"

    @pytest.mark.asyncio()
    async def test_increments_retry_on_subsequent_failure(
        self,
        service: DunningService,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        record = await service.handle_payment_failed("sub_1", "inv_2", "cus_123")
        assert record.retry_count == 1

    @pytest.mark.asyncio()
    async def test_updates_invoice_on_subsequent_failure(
        self,
        service: DunningService,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        record = await service.handle_payment_failed("sub_1", "inv_2", "cus_123")
        assert record.invoice_id == "inv_2"

    @pytest.mark.asyncio()
    async def test_does_not_send_duplicate_email(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        await service.handle_payment_failed("sub_1", "inv_2", "cus_123")
        # Only 1 email — day1 was already sent
        assert len(email_sender.sent) == 1

    @pytest.mark.asyncio()
    async def test_no_email_if_customer_not_found(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_unknown")
        assert len(email_sender.sent) == 0


# ── DunningService — handle_payment_succeeded ───────────────────────────


class TestHandlePaymentSucceeded:
    @pytest.mark.asyncio()
    async def test_clears_dunning_record(
        self,
        service: DunningService,
        store: InMemoryDunningStore,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        result = await service.handle_payment_succeeded("sub_1")
        assert result is True
        assert await store.get("sub_1") is None

    @pytest.mark.asyncio()
    async def test_returns_false_if_no_dunning(
        self,
        service: DunningService,
    ) -> None:
        result = await service.handle_payment_succeeded("sub_none")
        assert result is False

    @pytest.mark.asyncio()
    async def test_fires_callback_on_recovery(
        self,
        store: InMemoryDunningStore,
        email_sender: InMemoryEmailSender,
        resolver: InMemoryCustomerResolver,
        downgrader: NoopSubscriptionDowngrader,
        clock: _FakeClock,
    ) -> None:
        callback_calls: list[tuple[DunningRecord, str]] = []

        async def on_change(record: DunningRecord, reason: str) -> None:
            callback_calls.append((record, reason))

        svc = DunningService(
            store=store,
            email_sender=email_sender,
            customer_resolver=resolver,
            downgrader=downgrader,
            on_state_change=on_change,
            now_fn=clock,
        )
        await svc.handle_payment_failed("sub_1", "inv_1", "cus_123")
        await svc.handle_payment_succeeded("sub_1")
        assert len(callback_calls) == 1
        assert callback_calls[0][0].state == DunningState.ACTIVE
        assert callback_calls[0][1] == "recovered"


# ── DunningService — process_dunning_cycle ──────────────────────────────


class TestProcessDunningCycle:
    @pytest.mark.asyncio()
    async def test_advances_to_day3(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(3)
        processed = await service.process_dunning_cycle()
        assert len(processed) == 1
        assert processed[0].state == DunningState.PAST_DUE_DAY3
        # 2 emails: day1 + day3
        assert len(email_sender.sent) == 2
        assert email_sender.sent[1]["template_id"] == "dunning_day3"

    @pytest.mark.asyncio()
    async def test_advances_to_day7(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(7)
        processed = await service.process_dunning_cycle()
        assert len(processed) == 1
        assert processed[0].state == DunningState.PAST_DUE_DAY7

    @pytest.mark.asyncio()
    async def test_day14_downgrades_and_cancels(
        self,
        service: DunningService,
        downgrader: NoopSubscriptionDowngrader,
        store: InMemoryDunningStore,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(14)
        processed = await service.process_dunning_cycle()
        assert len(processed) == 1
        assert processed[0].state == DunningState.CANCELED
        # Downgrader was called
        assert len(downgrader.downgrades) == 1
        assert downgrader.downgrades[0]["reason"] == "dunning_grace_period_expired"
        # Record marked as canceled in store
        rec = await store.get("sub_1")
        assert rec is not None
        assert rec.state == DunningState.CANCELED

    @pytest.mark.asyncio()
    async def test_no_change_within_same_state(
        self,
        service: DunningService,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_hours(12)  # Still day 1
        processed = await service.process_dunning_cycle()
        assert len(processed) == 0

    @pytest.mark.asyncio()
    async def test_skips_canceled_records(
        self,
        service: DunningService,
        store: InMemoryDunningStore,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(14)
        await service.process_dunning_cycle()  # cancels
        clock.advance_days(1)
        processed = await service.process_dunning_cycle()
        assert len(processed) == 0

    @pytest.mark.asyncio()
    async def test_processes_multiple_records(
        self,
        service: DunningService,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        await service.handle_payment_failed("sub_2", "inv_2", "cus_456")
        clock.advance_days(3)
        processed = await service.process_dunning_cycle()
        assert len(processed) == 2

    @pytest.mark.asyncio()
    async def test_full_lifecycle(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
        downgrader: NoopSubscriptionDowngrader,
        clock: _FakeClock,
    ) -> None:
        """Test the full day 1 → 3 → 7 → 14 → canceled lifecycle."""
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        assert len(email_sender.sent) == 1  # day1

        clock.advance_days(3)
        await service.process_dunning_cycle()
        assert len(email_sender.sent) == 2  # day3

        clock.advance_days(4)  # now at day 7
        await service.process_dunning_cycle()
        assert len(email_sender.sent) == 3  # day7

        clock.advance_days(7)  # now at day 14
        await service.process_dunning_cycle()
        assert len(email_sender.sent) == 4  # day14
        assert len(downgrader.downgrades) == 1

    @pytest.mark.asyncio()
    async def test_state_change_callback_on_advance(
        self,
        store: InMemoryDunningStore,
        email_sender: InMemoryEmailSender,
        resolver: InMemoryCustomerResolver,
        downgrader: NoopSubscriptionDowngrader,
        clock: _FakeClock,
    ) -> None:
        callback_calls: list[tuple[DunningRecord, str]] = []

        async def on_change(record: DunningRecord, reason: str) -> None:
            callback_calls.append((record, reason))

        svc = DunningService(
            store=store,
            email_sender=email_sender,
            customer_resolver=resolver,
            downgrader=downgrader,
            on_state_change=on_change,
            now_fn=clock,
        )
        await svc.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(3)
        await svc.process_dunning_cycle()
        assert len(callback_calls) == 1
        assert "past_due_day1->past_due_day3" in callback_calls[0][1]


# ── DunningService — get_status ──────────────────────────────────────────


class TestGetStatus:
    @pytest.mark.asyncio()
    async def test_returns_record(self, service: DunningService) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        status = await service.get_status("sub_1")
        assert status is not None
        assert status.subscription_id == "sub_1"

    @pytest.mark.asyncio()
    async def test_returns_none_if_not_in_dunning(
        self,
        service: DunningService,
    ) -> None:
        status = await service.get_status("sub_nope")
        assert status is None


# ── DunningService — email deduplication ─────────────────────────────────


class TestEmailDeduplication:
    @pytest.mark.asyncio()
    async def test_same_state_email_not_sent_twice(
        self,
        service: DunningService,
        email_sender: InMemoryEmailSender,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        # Simulate re-processing without state change
        record = await service.get_status("sub_1")
        assert record is not None
        assert "friendly_reminder" in record.emails_sent
        # Only 1 email total
        assert len(email_sender.sent) == 1


# ── DunningService — recovery at different stages ────────────────────────


class TestRecoveryAtDifferentStages:
    @pytest.mark.asyncio()
    async def test_recovery_at_day3(
        self,
        service: DunningService,
        store: InMemoryDunningStore,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(3)
        await service.process_dunning_cycle()
        result = await service.handle_payment_succeeded("sub_1")
        assert result is True
        assert await store.get("sub_1") is None

    @pytest.mark.asyncio()
    async def test_recovery_at_day7(
        self,
        service: DunningService,
        store: InMemoryDunningStore,
        clock: _FakeClock,
    ) -> None:
        await service.handle_payment_failed("sub_1", "inv_1", "cus_123")
        clock.advance_days(7)
        await service.process_dunning_cycle()
        result = await service.handle_payment_succeeded("sub_1")
        assert result is True
        assert await store.get("sub_1") is None


# ── Abstract base classes raise NotImplementedError ──────────────────────


class TestAbstractBases:
    @pytest.mark.asyncio()
    async def test_email_sender_not_implemented(self) -> None:
        sender = EmailSender()
        with pytest.raises(NotImplementedError):
            await sender.send("a@b.com", "S", "tpl", {})

    @pytest.mark.asyncio()
    async def test_dunning_store_get_not_implemented(self) -> None:
        store = DunningStore()
        with pytest.raises(NotImplementedError):
            await store.get("sub_1")

    @pytest.mark.asyncio()
    async def test_dunning_store_save_not_implemented(self) -> None:
        store = DunningStore()
        with pytest.raises(NotImplementedError):
            await store.save(DunningRecord("s", "c", "i"))

    @pytest.mark.asyncio()
    async def test_dunning_store_delete_not_implemented(self) -> None:
        store = DunningStore()
        with pytest.raises(NotImplementedError):
            await store.delete("sub_1")

    @pytest.mark.asyncio()
    async def test_dunning_store_list_active_not_implemented(self) -> None:
        store = DunningStore()
        with pytest.raises(NotImplementedError):
            await store.list_active()

    @pytest.mark.asyncio()
    async def test_subscription_downgrader_not_implemented(self) -> None:
        dg = SubscriptionDowngrader()
        with pytest.raises(NotImplementedError):
            await dg.downgrade_to_free("sub_1", "cus_1")

    @pytest.mark.asyncio()
    async def test_customer_resolver_not_implemented(self) -> None:
        cr = CustomerResolver()
        with pytest.raises(NotImplementedError):
            await cr.get_email("cus_1")
