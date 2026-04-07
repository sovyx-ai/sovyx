"""Stripe billing — checkout, portal sessions, and webhook processing.

Implements Stripe Checkout for subscription purchases across 6 tiers,
customer portal for self-service management, and webhook handling for
lifecycle events with signature verification and idempotency.

References:
    - IMPL-011: Stripe Connect — Marketplace Billing
    - SPE-033 §CLD-012: Stripe Billing
    - SPE-033 §CLD-013: Stripe Webhook Handler
    - IMPL-SUP-006: 6 pricing tiers

Tier pricing (IMPL-SUP-006):
    Free: $0 | Starter: $3.99 | Sync: $5.99
    Cloud: $9.99 | Business: $99 | Enterprise: custom
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from typing import Protocol

    class _StripeClient(Protocol):
        """Minimal Stripe SDK interface for type checking."""

        api_key: str

        @property
        def checkout(self) -> Any: ...  # noqa: ANN401

        @property
        def billing_portal(self) -> Any: ...  # noqa: ANN401


logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

WEBHOOK_TOLERANCE_SECONDS = 300
"""Maximum age of webhook signature (replay protection)."""

STRIPE_SIGNATURE_PREFIX = "v1"
"""Stripe uses v1 HMAC-SHA256 signatures."""


# ── Tier definitions ─────────────────────────────────────────────────────


class SubscriptionTier(enum.Enum):
    """Sovyx subscription tiers with pricing in cents (USD)."""

    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


TIER_PRICES: dict[SubscriptionTier, int] = {
    SubscriptionTier.FREE: 0,
    SubscriptionTier.STARTER: 399,
    SubscriptionTier.SYNC: 599,
    SubscriptionTier.CLOUD: 999,
    SubscriptionTier.BUSINESS: 9900,
    SubscriptionTier.ENTERPRISE: 0,  # Custom pricing
}

TIER_NAMES: dict[SubscriptionTier, str] = {
    SubscriptionTier.FREE: "Sovyx Free",
    SubscriptionTier.STARTER: "Sovyx Starter",
    SubscriptionTier.SYNC: "Sovyx Sync",
    SubscriptionTier.CLOUD: "Sovyx Cloud",
    SubscriptionTier.BUSINESS: "Sovyx Business",
    SubscriptionTier.ENTERPRISE: "Sovyx Enterprise",
}


# ── Data models ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BillingConfig:
    """Configuration for Stripe billing integration."""

    secret_key: str
    webhook_secret: str
    success_url: str = "https://sovyx.ai/billing/success"
    cancel_url: str = "https://sovyx.ai/billing/cancel"
    portal_return_url: str = "https://sovyx.ai/billing"
    currency: str = "usd"
    tax_product_code: str = "txcd_10000000"  # Digital goods / software


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Result of creating a checkout session."""

    session_id: str
    url: str
    tier: SubscriptionTier
    amount_cents: int


@dataclass(frozen=True, slots=True)
class PortalResult:
    """Result of creating a portal session."""

    url: str
    customer_id: str


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Parsed and verified webhook event."""

    event_id: str
    event_type: str
    data: dict[str, Any]
    created: int
    api_version: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Result of webhook processing."""

    status: str  # "ok" | "already_processed" | "unhandled" | "error"
    event_id: str = ""
    event_type: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    """Subscription details extracted from webhook events."""

    subscription_id: str
    customer_id: str
    tier: SubscriptionTier | None
    status: str
    current_period_start: int | None = None
    current_period_end: int | None = None
    trial_end: int | None = None
    amount_cents: int = 0
    interval: str = "month"


# ── Webhook signature verification ──────────────────────────────────────


class WebhookSignatureError(Exception):
    """Raised when webhook signature verification fails."""


class WebhookPayloadError(Exception):
    """Raised when webhook payload is invalid."""


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
    _now: float | None = None,
) -> None:
    """Verify Stripe webhook signature (HMAC-SHA256).

    Uses raw request body bytes — parsing changes whitespace and
    would invalidate the signature.

    Args:
        raw_body: Raw HTTP request body (bytes).
        signature_header: Value of Stripe-Signature header.
        secret: Webhook signing secret (whsec_...).
        tolerance: Maximum age in seconds (default 300).
        _now: Override current time for testing.

    Raises:
        WebhookSignatureError: If signature is invalid or too old.
        WebhookPayloadError: If header format is invalid.
    """
    if not signature_header:
        msg = "Missing Stripe-Signature header"
        raise WebhookSignatureError(msg)

    # Parse header: t=timestamp,v1=signature[,v1=signature2,...]
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key_value = item.strip().split("=", 1)
        if len(key_value) != 2:  # noqa: PLR2004
            msg = f"Invalid signature header format: {item!r}"
            raise WebhookPayloadError(msg)
        key, value = key_value
        parts.setdefault(key, []).append(value)

    timestamps = parts.get("t")
    signatures = parts.get(STRIPE_SIGNATURE_PREFIX)

    if not timestamps or not signatures:
        msg = "Missing timestamp or signature in header"
        raise WebhookSignatureError(msg)

    try:
        timestamp = int(timestamps[0])
    except (ValueError, IndexError) as exc:
        msg = "Invalid timestamp in signature header"
        raise WebhookSignatureError(msg) from exc

    # Check tolerance (replay protection)
    now = _now if _now is not None else time.time()
    if abs(now - timestamp) > tolerance:
        msg = f"Webhook timestamp too old: {int(abs(now - timestamp))}s > {tolerance}s"
        raise WebhookSignatureError(msg)

    # Compute expected signature
    signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison against all provided signatures
    matched = any(hmac.compare_digest(expected, sig) for sig in signatures)
    if not matched:
        msg = "Webhook signature mismatch"
        raise WebhookSignatureError(msg)


# ── Event store protocol ────────────────────────────────────────────────


class EventStore:
    """Interface for webhook event idempotency tracking.

    Implementations should persist event IDs to prevent duplicate processing.
    """

    async def is_processed(self, event_id: str) -> bool:
        """Check if event has already been processed."""
        raise NotImplementedError

    async def mark_processed(
        self,
        event_id: str,
        event_type: str,
        *,
        error: str | None = None,
    ) -> None:
        """Record that an event has been processed."""
        raise NotImplementedError


class InMemoryEventStore(EventStore):
    """In-memory event store for testing and lightweight usage."""

    def __init__(self) -> None:
        self._processed: dict[str, dict[str, Any]] = {}

    async def is_processed(self, event_id: str) -> bool:
        """Check if event has already been processed."""
        return event_id in self._processed

    async def mark_processed(
        self,
        event_id: str,
        event_type: str,
        *,
        error: str | None = None,
    ) -> None:
        """Record that an event has been processed."""
        self._processed[event_id] = {
            "event_type": event_type,
            "error": error,
            "processed_at": time.time(),
        }


# ── Stripe status mapping ───────────────────────────────────────────────

STRIPE_STATUS_MAP: dict[str, str] = {
    "trialing": "trial",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "canceled",
    "incomplete": "pending",
    "incomplete_expired": "expired",
    "paused": "paused",
}


def map_stripe_status(stripe_status: str) -> str:
    """Map Stripe subscription status to Sovyx status.

    Args:
        stripe_status: Raw Stripe status string.

    Returns:
        Sovyx-internal status string.
    """
    return STRIPE_STATUS_MAP.get(stripe_status, stripe_status)


def tier_from_price_id(
    price_id: str,
    price_map: dict[str, SubscriptionTier],
) -> SubscriptionTier | None:
    """Resolve tier from a Stripe price ID.

    Args:
        price_id: Stripe price identifier (price_...).
        price_map: Mapping of price IDs to tiers (configured at runtime).

    Returns:
        Matching tier or None if not found.
    """
    return price_map.get(price_id)


# ── Webhook handler ─────────────────────────────────────────────────────

# Type alias for event handler callbacks
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WebhookHandler:
    """Process Stripe webhooks with signature verification and idempotency.

    Flow:
        1. Verify HMAC-SHA256 signature on raw body
        2. Parse JSON payload into WebhookEvent
        3. Check idempotency (skip if already processed)
        4. Dispatch to registered handler
        5. Mark as processed
        6. Return result

    Rules:
        - Use raw_body bytes for verification (NOT parsed JSON)
        - Return quickly — queue heavy processing
        - Log unhandled events (Stripe sends many event types)

    Example::

        handler = WebhookHandler(
            webhook_secret="whsec_...",
            event_store=InMemoryEventStore(),
        )
        handler.register("checkout.session.completed", my_callback)
        result = await handler.process(raw_body, sig_header)
    """

    def __init__(
        self,
        webhook_secret: str,
        event_store: EventStore,
    ) -> None:
        self._secret = webhook_secret
        self._store = event_store
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a specific event type.

        Args:
            event_type: Stripe event type (e.g. "checkout.session.completed").
            handler: Async callable receiving the event data dict.
        """
        self._handlers[event_type] = handler

    @property
    def registered_events(self) -> frozenset[str]:
        """Set of event types with registered handlers."""
        return frozenset(self._handlers.keys())

    async def process(
        self,
        raw_body: bytes,
        signature_header: str,
        *,
        _now: float | None = None,
    ) -> WebhookResult:
        """Process an incoming Stripe webhook.

        Args:
            raw_body: Raw HTTP request body bytes.
            signature_header: Stripe-Signature header value.
            _now: Override current time (testing only).

        Returns:
            WebhookResult with processing status.

        Raises:
            WebhookSignatureError: If signature verification fails.
            WebhookPayloadError: If payload parsing fails.
        """
        # 1. Verify signature
        verify_webhook_signature(
            raw_body,
            signature_header,
            self._secret,
            _now=_now,
        )

        # 2. Parse payload
        import json

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = "Failed to parse webhook payload"
            raise WebhookPayloadError(msg) from exc

        event = WebhookEvent(
            event_id=payload.get("id", ""),
            event_type=payload.get("type", ""),
            data=payload.get("data", {}).get("object", {}),
            created=payload.get("created", 0),
            api_version=payload.get("api_version"),
        )

        if not event.event_id:
            msg = "Missing event ID in webhook payload"
            raise WebhookPayloadError(msg)

        # 3. Idempotency check
        if await self._store.is_processed(event.event_id):
            logger.debug("webhook_duplicate", event_id=event.event_id)
            return WebhookResult(
                status="already_processed",
                event_id=event.event_id,
                event_type=event.event_type,
            )

        # 4. Dispatch to handler
        handler = self._handlers.get(event.event_type)

        if handler is not None:
            try:
                await handler(event.data)
            except Exception:
                logger.exception(
                    "webhook_handler_error",
                    event_type=event.event_type,
                    event_id=event.event_id,
                )
                await self._store.mark_processed(
                    event.event_id,
                    event.event_type,
                    error="handler_exception",
                )
                return WebhookResult(
                    status="error",
                    event_id=event.event_id,
                    event_type=event.event_type,
                    error="handler_exception",
                )
        else:
            logger.info("webhook_unhandled", event_type=event.event_type)

        # 5. Mark processed
        await self._store.mark_processed(event.event_id, event.event_type)

        return WebhookResult(
            status="ok" if handler is not None else "unhandled",
            event_id=event.event_id,
            event_type=event.event_type,
        )


# ── Billing service ─────────────────────────────────────────────────────


class BillingService:
    """Stripe Checkout + Customer Portal for Sovyx subscriptions.

    Creates checkout sessions for the 6 subscription tiers and
    portal sessions for self-service subscription management.

    This service wraps the Stripe Python SDK and is designed to be
    testable by mocking the ``stripe_client`` parameter.

    Example::

        billing = BillingService(
            config=BillingConfig(secret_key="sk_...", webhook_secret="whsec_..."),
            stripe_client=stripe,
        )
        result = await billing.create_checkout(
            tier=SubscriptionTier.CLOUD,
            customer_id="cus_...",
        )
    """

    def __init__(
        self,
        config: BillingConfig,
        stripe_client: _StripeClient | None = None,
        price_map: dict[SubscriptionTier, str] | None = None,
    ) -> None:
        """Initialize billing service.

        Args:
            config: Stripe billing configuration.
            stripe_client: Stripe SDK module or mock (for testability).
            price_map: Mapping of tiers to Stripe price IDs.
                       If None, uses product_data inline pricing.
        """
        self._config = config
        self._stripe = stripe_client
        self._price_map = price_map or {}

        if self._stripe is not None:
            self._stripe.api_key = config.secret_key

    async def create_checkout(
        self,
        tier: SubscriptionTier,
        customer_id: str,
        *,
        trial_days: int = 0,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> CheckoutResult:
        """Create a Stripe Checkout session for a subscription tier.

        Args:
            tier: Target subscription tier.
            customer_id: Stripe customer ID (cus_...).
            trial_days: Free trial period in days (0 = no trial).
            success_url: Override success redirect URL.
            cancel_url: Override cancel redirect URL.
            metadata: Additional metadata for the session.

        Returns:
            CheckoutResult with session ID and redirect URL.

        Raises:
            ValueError: If tier is FREE or ENTERPRISE (no checkout).
            RuntimeError: If Stripe client is not configured.
        """
        if tier == SubscriptionTier.FREE:
            msg = "Free tier does not require checkout"
            raise ValueError(msg)
        if tier == SubscriptionTier.ENTERPRISE:
            msg = "Enterprise tier requires custom pricing — contact sales"
            raise ValueError(msg)
        if self._stripe is None:
            msg = "Stripe client not configured"
            raise RuntimeError(msg)

        amount = TIER_PRICES[tier]
        name = TIER_NAMES[tier]

        session_metadata = {"sovyx_tier": tier.value}
        if metadata:
            session_metadata.update(metadata)

        # Build line items — use price_map if available, otherwise inline
        if tier in self._price_map:
            line_items = [{"price": self._price_map[tier], "quantity": 1}]
        else:
            line_items = [
                {
                    "price_data": {
                        "currency": self._config.currency,
                        "product_data": {
                            "name": name,
                            "tax_code": self._config.tax_product_code,
                        },
                        "unit_amount": amount,
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                },
            ]

        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": line_items,
            "success_url": (success_url or self._config.success_url)
            + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url or self._config.cancel_url,
            "automatic_tax": {"enabled": True},
            "metadata": session_metadata,
        }

        if trial_days > 0:
            params["subscription_data"] = {
                "trial_period_days": trial_days,
                "metadata": session_metadata,
            }

        session = self._stripe.checkout.Session.create(**params)

        return CheckoutResult(
            session_id=session.id,
            url=session.url,
            tier=tier,
            amount_cents=amount,
        )

    async def create_portal_session(
        self,
        customer_id: str,
        *,
        return_url: str | None = None,
    ) -> PortalResult:
        """Create a Stripe Customer Portal session.

        The portal lets customers:
        - View and manage their subscription
        - Update payment method
        - Change tier (upgrade/downgrade)
        - Cancel subscription
        - View invoice history

        Args:
            customer_id: Stripe customer ID (cus_...).
            return_url: Override return URL after portal exit.

        Returns:
            PortalResult with redirect URL.

        Raises:
            RuntimeError: If Stripe client is not configured.
        """
        if self._stripe is None:
            msg = "Stripe client not configured"
            raise RuntimeError(msg)

        session = self._stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url or self._config.portal_return_url,
        )

        return PortalResult(
            url=session.url,
            customer_id=customer_id,
        )

    def extract_subscription_info(
        self,
        data: dict[str, Any],
        price_id_map: dict[str, SubscriptionTier] | None = None,
    ) -> SubscriptionInfo:
        """Extract subscription details from a webhook event data object.

        Args:
            data: Stripe subscription data object from webhook.
            price_id_map: Reverse mapping of price IDs to tiers.

        Returns:
            Parsed SubscriptionInfo.
        """
        tier: SubscriptionTier | None = None
        amount_cents = 0
        interval = "month"

        items = data.get("items", {}).get("data", [])
        if items:
            price = items[0].get("price", {})
            price_id = price.get("id", "")
            amount_cents = price.get("unit_amount", 0)
            recurring = price.get("recurring", {})
            interval = recurring.get("interval", "month")

            if price_id_map:
                tier = price_id_map.get(price_id)

        # Fallback: check metadata
        if tier is None:
            tier_value = data.get("metadata", {}).get("sovyx_tier", "")
            if tier_value:
                with contextlib.suppress(ValueError):
                    tier = SubscriptionTier(tier_value)

        return SubscriptionInfo(
            subscription_id=data.get("id", ""),
            customer_id=data.get("customer", ""),
            tier=tier,
            status=map_stripe_status(data.get("status", "")),
            current_period_start=data.get("current_period_start"),
            current_period_end=data.get("current_period_end"),
            trial_end=data.get("trial_end"),
            amount_cents=amount_cents,
            interval=interval,
        )
