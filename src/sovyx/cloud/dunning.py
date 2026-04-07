"""Dunning service — payment failure recovery with 4-email sequence.

Implements a state machine for payment failure handling:
ACTIVE → PAST_DUE (Day 1→3→7→14) → CANCELED.
Payment success at any stage returns to ACTIVE.

Smart retry with exponential backoff: 1h, 4h, 24h, 72h.

References:
    - IMPL-011 §dunning: Payment recovery flow
    - SPE-033 §CLD-055: DunningService specification
"""

from __future__ import annotations

import enum
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

RETRY_DELAYS_SECONDS: tuple[int, ...] = (
    3600,  # 1 hour
    14400,  # 4 hours
    86400,  # 24 hours
    259200,  # 72 hours
)
"""Exponential backoff intervals for Stripe retry attempts."""

EMAIL_SCHEDULE_DAYS: tuple[int, ...] = (1, 3, 7, 14)
"""Days after initial failure when dunning emails are sent."""

GRACE_PERIOD_DAYS: int = 14
"""Total grace period before downgrade to Free tier."""


# ── Enums ────────────────────────────────────────────────────────────────


class DunningState(enum.Enum):
    """Subscription dunning state machine states."""

    ACTIVE = "active"
    PAST_DUE_DAY1 = "past_due_day1"
    PAST_DUE_DAY3 = "past_due_day3"
    PAST_DUE_DAY7 = "past_due_day7"
    PAST_DUE_DAY14 = "past_due_day14"
    CANCELED = "canceled"


class EmailType(enum.Enum):
    """Dunning email types in the 4-email sequence."""

    FRIENDLY_REMINDER = "friendly_reminder"
    ACTION_NEEDED = "action_needed"
    SERVICE_AT_RISK = "service_at_risk"
    FINAL_NOTICE = "final_notice"


# State → email mapping (ordered)
STATE_EMAIL_MAP: dict[DunningState, EmailType] = {
    DunningState.PAST_DUE_DAY1: EmailType.FRIENDLY_REMINDER,
    DunningState.PAST_DUE_DAY3: EmailType.ACTION_NEEDED,
    DunningState.PAST_DUE_DAY7: EmailType.SERVICE_AT_RISK,
    DunningState.PAST_DUE_DAY14: EmailType.FINAL_NOTICE,
}

# State progression order
_STATE_ORDER: tuple[DunningState, ...] = (
    DunningState.ACTIVE,
    DunningState.PAST_DUE_DAY1,
    DunningState.PAST_DUE_DAY3,
    DunningState.PAST_DUE_DAY7,
    DunningState.PAST_DUE_DAY14,
    DunningState.CANCELED,
)


def _days_to_state(days_elapsed: int) -> DunningState:
    """Map days since first failure to appropriate dunning state.

    Args:
        days_elapsed: Number of days since the first payment failure.

    Returns:
        The dunning state corresponding to the elapsed time.
    """
    if days_elapsed >= EMAIL_SCHEDULE_DAYS[3]:
        return DunningState.PAST_DUE_DAY14
    if days_elapsed >= EMAIL_SCHEDULE_DAYS[2]:
        return DunningState.PAST_DUE_DAY7
    if days_elapsed >= EMAIL_SCHEDULE_DAYS[1]:
        return DunningState.PAST_DUE_DAY3
    if days_elapsed >= EMAIL_SCHEDULE_DAYS[0]:
        return DunningState.PAST_DUE_DAY1
    return DunningState.ACTIVE


# ── Data models ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DunningEmail:
    """Email to send during dunning sequence."""

    email_type: EmailType
    subject: str
    template_id: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DunningRecord:
    """Tracks dunning state for a subscription.

    This is persisted by the DunningStore implementation.
    """

    subscription_id: str
    customer_id: str
    invoice_id: str
    state: DunningState = DunningState.PAST_DUE_DAY1
    first_failed_at: float = 0.0
    last_retry_at: float = 0.0
    retry_count: int = 0
    emails_sent: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def days_elapsed(self) -> float:
        """Days since first failure."""
        if self.first_failed_at <= 0:
            return 0.0
        return (time.time() - self.first_failed_at) / 86400

    @property
    def next_retry_delay(self) -> int:
        """Next retry delay in seconds based on retry count."""
        idx = min(self.retry_count, len(RETRY_DELAYS_SECONDS) - 1)
        return RETRY_DELAYS_SECONDS[idx]

    @property
    def should_retry(self) -> bool:
        """Whether a retry should be attempted now."""
        if self.state == DunningState.CANCELED:
            return False
        if self.retry_count >= len(RETRY_DELAYS_SECONDS):
            return False
        if self.last_retry_at <= 0:
            return True
        return (time.time() - self.last_retry_at) >= self.next_retry_delay


# ── Email templates ──────────────────────────────────────────────────────

DUNNING_EMAILS: dict[EmailType, DunningEmail] = {
    EmailType.FRIENDLY_REMINDER: DunningEmail(
        email_type=EmailType.FRIENDLY_REMINDER,
        subject="Payment issue — we'll retry automatically",
        template_id="dunning_day1",
    ),
    EmailType.ACTION_NEEDED: DunningEmail(
        email_type=EmailType.ACTION_NEEDED,
        subject="Action needed — update your payment method",
        template_id="dunning_day3",
    ),
    EmailType.SERVICE_AT_RISK: DunningEmail(
        email_type=EmailType.SERVICE_AT_RISK,
        subject="Your Sovyx subscription is at risk",
        template_id="dunning_day7",
    ),
    EmailType.FINAL_NOTICE: DunningEmail(
        email_type=EmailType.FINAL_NOTICE,
        subject="Final notice — subscription will be downgraded",
        template_id="dunning_day14",
    ),
}


# ── Protocols / ABCs ────────────────────────────────────────────────────


class EmailSender:
    """Abstract email sender interface.

    Implementations can use SES, Resend, Postmark, etc.
    """

    async def send(
        self,
        to_email: str,
        subject: str,
        template_id: str,
        template_data: dict[str, Any],
    ) -> bool:
        """Send a templated email.

        Args:
            to_email: Recipient email address.
            subject: Email subject line.
            template_id: Template identifier for the email service.
            template_data: Variables to inject into the template.

        Returns:
            True if sent successfully, False otherwise.
        """
        raise NotImplementedError


class DunningStore:
    """Abstract storage for dunning records.

    Implementations should persist to database for production use.
    """

    async def get(self, subscription_id: str) -> DunningRecord | None:
        """Get dunning record for a subscription."""
        raise NotImplementedError

    async def save(self, record: DunningRecord) -> None:
        """Create or update a dunning record."""
        raise NotImplementedError

    async def delete(self, subscription_id: str) -> bool:
        """Delete dunning record (payment recovered).

        Returns:
            True if a record was deleted, False if not found.
        """
        raise NotImplementedError

    async def list_active(self) -> list[DunningRecord]:
        """List all active dunning records (not canceled/resolved)."""
        raise NotImplementedError


class SubscriptionDowngrader:
    """Abstract interface to downgrade a subscription to Free tier."""

    async def downgrade_to_free(
        self,
        subscription_id: str,
        customer_id: str,
        *,
        reason: str = "dunning_expired",
    ) -> bool:
        """Downgrade subscription to Free tier.

        Args:
            subscription_id: Stripe subscription ID.
            customer_id: Stripe customer ID.
            reason: Reason for downgrade (for audit log).

        Returns:
            True if downgraded successfully.
        """
        raise NotImplementedError


class CustomerResolver:
    """Resolve customer email from customer/subscription ID."""

    async def get_email(self, customer_id: str) -> str | None:
        """Get customer email address.

        Args:
            customer_id: Stripe customer ID.

        Returns:
            Email address or None if not found.
        """
        raise NotImplementedError


# ── In-memory implementations (testing) ─────────────────────────────────


class InMemoryDunningStore(DunningStore):
    """In-memory dunning store for testing."""

    def __init__(self) -> None:
        self._records: dict[str, DunningRecord] = {}

    async def get(self, subscription_id: str) -> DunningRecord | None:
        """Get dunning record for a subscription."""
        return self._records.get(subscription_id)

    async def save(self, record: DunningRecord) -> None:
        """Create or update a dunning record."""
        record.updated_at = time.time()
        self._records[record.subscription_id] = record

    async def delete(self, subscription_id: str) -> bool:
        """Delete dunning record."""
        if subscription_id in self._records:
            del self._records[subscription_id]
            return True
        return False

    async def list_active(self) -> list[DunningRecord]:
        """List all non-canceled dunning records."""
        return [r for r in self._records.values() if r.state != DunningState.CANCELED]


class InMemoryEmailSender(EmailSender):
    """In-memory email sender for testing — records sent emails."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        to_email: str,
        subject: str,
        template_id: str,
        template_data: dict[str, Any],
    ) -> bool:
        """Record email as sent."""
        self.sent.append(
            {
                "to": to_email,
                "subject": subject,
                "template_id": template_id,
                "data": template_data,
            }
        )
        return True


class NoopSubscriptionDowngrader(SubscriptionDowngrader):
    """No-op downgrader for testing — records downgrade calls."""

    def __init__(self) -> None:
        self.downgrades: list[dict[str, str]] = []

    async def downgrade_to_free(
        self,
        subscription_id: str,
        customer_id: str,
        *,
        reason: str = "dunning_expired",
    ) -> bool:
        """Record downgrade call."""
        self.downgrades.append(
            {
                "subscription_id": subscription_id,
                "customer_id": customer_id,
                "reason": reason,
            }
        )
        return True


class InMemoryCustomerResolver(CustomerResolver):
    """In-memory customer resolver for testing."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping: dict[str, str] = mapping or {}

    async def get_email(self, customer_id: str) -> str | None:
        """Get customer email from mapping."""
        return self._mapping.get(customer_id)


# ── Event callbacks ──────────────────────────────────────────────────────

# Type alias for dunning event callbacks
DunningCallback = Callable[[DunningRecord, str], Awaitable[None]]


# ── DunningService ───────────────────────────────────────────────────────


class DunningService:
    """Payment failure recovery with 4-email dunning sequence.

    State machine::

        ACTIVE ──payment_failed──→ PAST_DUE_DAY1
        PAST_DUE_DAY1 ──3 days──→ PAST_DUE_DAY3
        PAST_DUE_DAY3 ──4 days──→ PAST_DUE_DAY7
        PAST_DUE_DAY7 ──7 days──→ PAST_DUE_DAY14
        PAST_DUE_DAY14 ──────────→ CANCELED (downgrade to Free)

        Any PAST_DUE_* ──payment_succeeded──→ ACTIVE (delete record)

    Smart retry schedule: 1h → 4h → 24h → 72h (exponential backoff).

    Each state transition triggers the corresponding dunning email:
        - Day 1: Friendly reminder (automatic retry)
        - Day 3: Action needed (update payment method)
        - Day 7: Service at risk
        - Day 14: Final notice + downgrade

    Example::

        service = DunningService(
            store=InMemoryDunningStore(),
            email_sender=InMemoryEmailSender(),
            customer_resolver=InMemoryCustomerResolver({"cus_1": "user@example.com"}),
            downgrader=NoopSubscriptionDowngrader(),
        )
        await service.handle_payment_failed("sub_1", "inv_1", "cus_1")
    """

    def __init__(
        self,
        store: DunningStore,
        email_sender: EmailSender,
        customer_resolver: CustomerResolver,
        downgrader: SubscriptionDowngrader,
        *,
        on_state_change: DunningCallback | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        """Initialize dunning service.

        Args:
            store: Persistent storage for dunning records.
            email_sender: Email sending implementation.
            customer_resolver: Resolves customer IDs to emails.
            downgrader: Handles subscription downgrades.
            on_state_change: Optional callback on state transitions.
            now_fn: Override time.time() for testing.
        """
        self._store = store
        self._email = email_sender
        self._resolver = customer_resolver
        self._downgrader = downgrader
        self._on_state_change = on_state_change
        self._now = now_fn or time.time

    async def handle_payment_failed(
        self,
        subscription_id: str,
        invoice_id: str,
        customer_id: str,
    ) -> DunningRecord:
        """Handle a payment failure event.

        If no dunning record exists, creates one and sends the first email.
        If a record exists, increments retry count and updates last_retry_at.

        Args:
            subscription_id: Stripe subscription ID.
            invoice_id: Stripe invoice ID that failed.
            customer_id: Stripe customer ID.

        Returns:
            The current or newly created DunningRecord.
        """
        now = self._now()
        record = await self._store.get(subscription_id)

        if record is None:
            # First failure — create record, send day-1 email
            record = DunningRecord(
                subscription_id=subscription_id,
                customer_id=customer_id,
                invoice_id=invoice_id,
                state=DunningState.PAST_DUE_DAY1,
                first_failed_at=now,
                last_retry_at=now,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            await self._store.save(record)
            await self._send_dunning_email(record)
            logger.info(
                "dunning_started",
                subscription_id=subscription_id,
                customer_id=customer_id,
            )
        else:
            # Subsequent failure — increment retry
            record.retry_count += 1
            record.last_retry_at = now
            record.invoice_id = invoice_id
            await self._store.save(record)
            logger.info(
                "dunning_retry",
                subscription_id=subscription_id,
                retry_count=record.retry_count,
            )

        return record

    async def handle_payment_succeeded(
        self,
        subscription_id: str,
    ) -> bool:
        """Handle a successful payment — recover from dunning.

        Deletes the dunning record, returning subscription to ACTIVE.

        Args:
            subscription_id: Stripe subscription ID.

        Returns:
            True if a dunning record was cleared, False if none existed.
        """
        record = await self._store.get(subscription_id)
        if record is None:
            return False

        old_state = record.state
        deleted = await self._store.delete(subscription_id)

        if deleted:
            logger.info(
                "dunning_recovered",
                subscription_id=subscription_id,
                from_state=old_state.value,
            )
            if self._on_state_change is not None:
                record.state = DunningState.ACTIVE
                await self._on_state_change(record, "recovered")

        return deleted

    async def process_dunning_cycle(self) -> list[DunningRecord]:
        """Process all active dunning records — advance states and send emails.

        Should be called periodically (e.g., daily via scheduler).
        Checks elapsed time since first failure and advances state machine.

        Returns:
            List of records that were advanced or acted upon.
        """
        active_records = await self._store.list_active()
        processed: list[DunningRecord] = []
        now = self._now()

        for record in active_records:
            days_elapsed = (now - record.first_failed_at) / 86400
            new_state = _days_to_state(int(days_elapsed))

            # Never regress state — only advance forward
            if new_state == record.state or _STATE_ORDER.index(new_state) <= _STATE_ORDER.index(
                record.state
            ):
                continue

            old_state = record.state
            record.state = new_state
            record.updated_at = now

            if new_state == DunningState.PAST_DUE_DAY14:
                # Final notice — send email, then downgrade
                await self._send_dunning_email(record)
                await self._downgrader.downgrade_to_free(
                    record.subscription_id,
                    record.customer_id,
                    reason="dunning_grace_period_expired",
                )
                record.state = DunningState.CANCELED
                record.updated_at = self._now()
                await self._store.save(record)
                logger.warning(
                    "dunning_canceled",
                    subscription_id=record.subscription_id,
                    days_elapsed=int(days_elapsed),
                )
            else:
                # State advanced — send corresponding email
                await self._send_dunning_email(record)
                await self._store.save(record)

            if self._on_state_change is not None:
                await self._on_state_change(record, f"{old_state.value}->{record.state.value}")

            processed.append(record)
            logger.info(
                "dunning_advanced",
                subscription_id=record.subscription_id,
                old_state=old_state.value,
                new_state=record.state.value,
            )

        return processed

    async def get_status(self, subscription_id: str) -> DunningRecord | None:
        """Get current dunning status for a subscription.

        Args:
            subscription_id: Stripe subscription ID.

        Returns:
            DunningRecord if in dunning, None if not.
        """
        return await self._store.get(subscription_id)

    async def _send_dunning_email(self, record: DunningRecord) -> bool:
        """Send the appropriate dunning email for the current state.

        Args:
            record: Current dunning record.

        Returns:
            True if email was sent, False if skipped or failed.
        """
        email_type = STATE_EMAIL_MAP.get(record.state)
        if email_type is None:
            return False

        # Don't send duplicate emails
        if email_type.value in record.emails_sent:
            return False

        email = DUNNING_EMAILS[email_type]

        customer_email = await self._resolver.get_email(record.customer_id)
        if customer_email is None:
            logger.warning(
                "dunning_email_no_address",
                customer_id=record.customer_id,
                email_type=email_type.value,
            )
            return False

        template_data = {
            "subscription_id": record.subscription_id,
            "invoice_id": record.invoice_id,
            "days_elapsed": int(record.days_elapsed),
            "grace_period_days": GRACE_PERIOD_DAYS,
            "retry_count": record.retry_count,
            "state": record.state.value,
        }

        sent = await self._email.send(
            to_email=customer_email,
            subject=email.subject,
            template_id=email.template_id,
            template_data=template_data,
        )

        if sent:
            record.emails_sent.append(email_type.value)
            await self._store.save(record)
            logger.info(
                "dunning_email_sent",
                subscription_id=record.subscription_id,
                email_type=email_type.value,
                to=customer_email,
            )

        return sent
