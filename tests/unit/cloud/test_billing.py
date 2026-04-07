"""Tests for Stripe billing — checkout, portal, and webhooks (V05-11).

Covers BillingService, WebhookHandler, signature verification,
event store, status mapping, and tier definitions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.billing import (
    STRIPE_STATUS_MAP,
    TIER_NAMES,
    TIER_PRICES,
    WEBHOOK_TOLERANCE_SECONDS,
    BillingConfig,
    BillingService,
    CheckoutResult,
    EventStore,
    InMemoryEventStore,
    PortalResult,
    SubscriptionInfo,
    SubscriptionTier,
    WebhookEvent,
    WebhookHandler,
    WebhookPayloadError,
    WebhookResult,
    WebhookSignatureError,
    map_stripe_status,
    tier_from_price_id,
    verify_webhook_signature,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_config(**overrides: str) -> BillingConfig:
    defaults = {
        "secret_key": "sk_test_abc123",
        "webhook_secret": "whsec_test_secret",
    }
    defaults.update(overrides)
    return BillingConfig(**defaults)


def _make_stripe_mock() -> MagicMock:
    """Create a mock Stripe client with checkout and portal support."""
    stripe = MagicMock()
    stripe.checkout.Session.create.return_value = MagicMock(
        id="cs_test_session_123",
        url="https://checkout.stripe.com/c/pay/cs_test_session_123",
    )
    stripe.billing_portal.Session.create.return_value = MagicMock(
        url="https://billing.stripe.com/p/session/test_portal_456",
    )
    return stripe


def _sign_payload(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate a valid Stripe-Signature header."""
    ts = timestamp or int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _make_event_payload(
    event_id: str = "evt_test_123",
    event_type: str = "checkout.session.completed",
    data: dict[str, Any] | None = None,
) -> bytes:
    """Create a JSON webhook event payload."""
    payload = {
        "id": event_id,
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": data or {}},
        "api_version": "2024-12-18.acacia",
    }
    return json.dumps(payload).encode()


# ── Tier definitions ─────────────────────────────────────────────────────


class TestSubscriptionTier:
    """Tests for tier enum and pricing constants."""

    def test_six_tiers_defined(self) -> None:
        assert len(SubscriptionTier) == 6

    def test_all_tiers_have_prices(self) -> None:
        for tier in SubscriptionTier:
            assert tier in TIER_PRICES

    def test_all_tiers_have_names(self) -> None:
        for tier in SubscriptionTier:
            assert tier in TIER_NAMES

    def test_prices_correct(self) -> None:
        assert TIER_PRICES[SubscriptionTier.FREE] == 0
        assert TIER_PRICES[SubscriptionTier.STARTER] == 399
        assert TIER_PRICES[SubscriptionTier.SYNC] == 599
        assert TIER_PRICES[SubscriptionTier.CLOUD] == 999
        assert TIER_PRICES[SubscriptionTier.BUSINESS] == 9900
        assert TIER_PRICES[SubscriptionTier.ENTERPRISE] == 0

    def test_tier_values(self) -> None:
        assert SubscriptionTier.FREE.value == "free"
        assert SubscriptionTier.CLOUD.value == "cloud"

    def test_tier_from_value(self) -> None:
        assert SubscriptionTier("sync") == SubscriptionTier.SYNC

    def test_invalid_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            SubscriptionTier("invalid")


# ── Status mapping ───────────────────────────────────────────────────────


class TestStatusMapping:
    """Tests for Stripe → Sovyx status mapping."""

    def test_active_maps_to_active(self) -> None:
        assert map_stripe_status("active") == "active"

    def test_trialing_maps_to_trial(self) -> None:
        assert map_stripe_status("trialing") == "trial"

    def test_past_due_preserved(self) -> None:
        assert map_stripe_status("past_due") == "past_due"

    def test_canceled_maps(self) -> None:
        assert map_stripe_status("canceled") == "canceled"

    def test_unpaid_maps_to_canceled(self) -> None:
        assert map_stripe_status("unpaid") == "canceled"

    def test_incomplete_maps_to_pending(self) -> None:
        assert map_stripe_status("incomplete") == "pending"

    def test_incomplete_expired_maps(self) -> None:
        assert map_stripe_status("incomplete_expired") == "expired"

    def test_paused_maps(self) -> None:
        assert map_stripe_status("paused") == "paused"

    def test_unknown_status_passthrough(self) -> None:
        assert map_stripe_status("custom_status") == "custom_status"

    def test_all_known_statuses_mapped(self) -> None:
        assert len(STRIPE_STATUS_MAP) == 8


# ── tier_from_price_id ───────────────────────────────────────────────────


class TestTierFromPriceId:
    """Tests for price ID to tier resolution."""

    def test_known_price_resolves(self) -> None:
        pm = {"price_123": SubscriptionTier.CLOUD}
        assert tier_from_price_id("price_123", pm) == SubscriptionTier.CLOUD

    def test_unknown_price_returns_none(self) -> None:
        pm = {"price_123": SubscriptionTier.CLOUD}
        assert tier_from_price_id("price_unknown", pm) is None

    def test_empty_map_returns_none(self) -> None:
        assert tier_from_price_id("price_123", {}) is None


# ── Webhook signature verification ──────────────────────────────────────


class TestWebhookSignatureVerification:
    """Tests for HMAC-SHA256 webhook signature verification."""

    def test_valid_signature_passes(self) -> None:
        body = b'{"test": "payload"}'
        secret = "whsec_test123"
        now = time.time()
        header = _sign_payload(body, secret, int(now))
        verify_webhook_signature(body, header, secret, _now=now)

    def test_invalid_signature_raises(self) -> None:
        body = b'{"test": "payload"}'
        secret = "whsec_test123"
        header = _sign_payload(body, "wrong_secret", int(time.time()))
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_webhook_signature(body, header, secret)

    def test_empty_header_raises(self) -> None:
        with pytest.raises(WebhookSignatureError, match="Missing"):
            verify_webhook_signature(b"body", "", "secret")

    def test_malformed_header_raises(self) -> None:
        with pytest.raises(WebhookPayloadError, match="Invalid signature header"):
            verify_webhook_signature(b"body", "garbage", "secret")

    def test_missing_timestamp_raises(self) -> None:
        with pytest.raises(WebhookSignatureError, match="Missing timestamp"):
            verify_webhook_signature(b"body", "v1=abc123", "secret")

    def test_missing_signature_raises(self) -> None:
        with pytest.raises(WebhookSignatureError, match="Missing timestamp"):
            verify_webhook_signature(b"body", "t=12345", "secret")

    def test_expired_timestamp_raises(self) -> None:
        body = b"payload"
        secret = "whsec_test"
        old_ts = int(time.time()) - WEBHOOK_TOLERANCE_SECONDS - 100
        header = _sign_payload(body, secret, old_ts)
        with pytest.raises(WebhookSignatureError, match="too old"):
            verify_webhook_signature(body, header, secret)

    def test_future_timestamp_raises(self) -> None:
        body = b"payload"
        secret = "whsec_test"
        future_ts = int(time.time()) + WEBHOOK_TOLERANCE_SECONDS + 100
        header = _sign_payload(body, secret, future_ts)
        with pytest.raises(WebhookSignatureError, match="too old"):
            verify_webhook_signature(body, header, secret)

    def test_tampered_body_fails(self) -> None:
        body = b'{"amount": 100}'
        secret = "whsec_test"
        header = _sign_payload(body, secret, int(time.time()))
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_webhook_signature(b'{"amount": 999}', header, secret)

    def test_custom_tolerance(self) -> None:
        body = b"payload"
        secret = "whsec_test"
        now = time.time()
        ts = int(now) - 50
        header = _sign_payload(body, secret, ts)
        # Should fail with 30s tolerance
        with pytest.raises(WebhookSignatureError, match="too old"):
            verify_webhook_signature(body, header, secret, tolerance=30, _now=now)
        # Should pass with 60s tolerance
        verify_webhook_signature(body, header, secret, tolerance=60, _now=now)

    def test_multiple_signatures_one_valid(self) -> None:
        body = b"payload"
        secret = "whsec_test"
        ts = int(time.time())
        signed = f"{ts}.".encode() + body
        valid_sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        header = f"t={ts},v1=invalid_sig_aaa,v1={valid_sig}"
        verify_webhook_signature(body, header, secret)

    def test_invalid_timestamp_format(self) -> None:
        with pytest.raises(WebhookSignatureError, match="Invalid timestamp"):
            verify_webhook_signature(b"body", "t=notanumber,v1=abc", "secret")

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(body=st.binary(min_size=1, max_size=1000))
    def test_roundtrip_any_body(self, body: bytes) -> None:
        secret = "whsec_prop_test"
        now = time.time()
        header = _sign_payload(body, secret, int(now))
        verify_webhook_signature(body, header, secret, _now=now)


# ── InMemoryEventStore ───────────────────────────────────────────────────


class TestInMemoryEventStore:
    """Tests for the in-memory event store."""

    async def test_new_event_not_processed(self) -> None:
        store = InMemoryEventStore()
        assert not await store.is_processed("evt_new")

    async def test_mark_then_check(self) -> None:
        store = InMemoryEventStore()
        await store.mark_processed("evt_1", "test.event")
        assert await store.is_processed("evt_1")

    async def test_different_events_independent(self) -> None:
        store = InMemoryEventStore()
        await store.mark_processed("evt_1", "test.event")
        assert not await store.is_processed("evt_2")

    async def test_mark_with_error(self) -> None:
        store = InMemoryEventStore()
        await store.mark_processed("evt_err", "test.event", error="boom")
        assert await store.is_processed("evt_err")
        assert store._processed["evt_err"]["error"] == "boom"


# ── EventStore base class ────────────────────────────────────────────────


class TestEventStoreBase:
    """Tests for the abstract EventStore."""

    async def test_is_processed_not_implemented(self) -> None:
        store = EventStore()
        with pytest.raises(NotImplementedError):
            await store.is_processed("evt_1")

    async def test_mark_processed_not_implemented(self) -> None:
        store = EventStore()
        with pytest.raises(NotImplementedError):
            await store.mark_processed("evt_1", "type")


# ── WebhookHandler ───────────────────────────────────────────────────────


class TestWebhookHandler:
    """Tests for the webhook handler dispatch logic."""

    def _make_handler(
        self,
        secret: str = "whsec_test_secret",
        store: EventStore | None = None,
    ) -> WebhookHandler:
        return WebhookHandler(
            webhook_secret=secret,
            event_store=store or InMemoryEventStore(),
        )

    async def test_valid_event_dispatched(self) -> None:
        handler = self._make_handler()
        callback = AsyncMock()
        handler.register("checkout.session.completed", callback)

        body = _make_event_payload(event_type="checkout.session.completed", data={"id": "cs_1"})
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        result = await handler.process(body, sig, _now=now)
        assert result.status == "ok"
        assert result.event_type == "checkout.session.completed"
        callback.assert_awaited_once_with({"id": "cs_1"})

    async def test_unhandled_event_returns_unhandled(self) -> None:
        handler = self._make_handler()
        body = _make_event_payload(event_type="unknown.event")
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        result = await handler.process(body, sig, _now=now)
        assert result.status == "unhandled"

    async def test_duplicate_event_skipped(self) -> None:
        store = InMemoryEventStore()
        handler = self._make_handler(store=store)
        callback = AsyncMock()
        handler.register("test.event", callback)

        body = _make_event_payload(event_id="evt_dup", event_type="test.event")
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        # First call — processed
        r1 = await handler.process(body, sig, _now=now)
        assert r1.status == "ok"

        # Second call — skipped
        r2 = await handler.process(body, sig, _now=now)
        assert r2.status == "already_processed"
        callback.assert_awaited_once()

    async def test_handler_error_returns_error_status(self) -> None:
        handler = self._make_handler()

        async def bad_handler(data: dict[str, Any]) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        handler.register("test.event", bad_handler)

        body = _make_event_payload(event_type="test.event")
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        result = await handler.process(body, sig, _now=now)
        assert result.status == "error"
        assert result.error == "handler_exception"

    async def test_invalid_signature_raises(self) -> None:
        handler = self._make_handler()
        body = _make_event_payload()
        with pytest.raises(WebhookSignatureError):
            await handler.process(body, "t=0,v1=bad")

    async def test_invalid_json_raises(self) -> None:
        handler = self._make_handler()
        body = b"not json"
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))
        # Valid signature but invalid JSON
        with pytest.raises(WebhookPayloadError, match="Failed to parse"):
            await handler.process(body, sig, _now=now)

    async def test_missing_event_id_raises(self) -> None:
        handler = self._make_handler()
        body = json.dumps({"type": "test", "data": {"object": {}}}).encode()
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))
        with pytest.raises(WebhookPayloadError, match="Missing event ID"):
            await handler.process(body, sig, _now=now)

    def test_registered_events_property(self) -> None:
        handler = self._make_handler()
        handler.register("a.event", AsyncMock())
        handler.register("b.event", AsyncMock())
        assert handler.registered_events == frozenset({"a.event", "b.event"})

    async def test_event_marked_processed_after_error(self) -> None:
        store = InMemoryEventStore()
        handler = self._make_handler(store=store)

        async def bad(data: dict[str, Any]) -> None:
            msg = "fail"
            raise ValueError(msg)

        handler.register("fail.event", bad)

        body = _make_event_payload(event_id="evt_fail", event_type="fail.event")
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        await handler.process(body, sig, _now=now)
        # Should be marked processed even after error
        assert await store.is_processed("evt_fail")

    async def test_webhook_event_fields_parsed(self) -> None:
        handler = self._make_handler()
        captured: list[dict[str, Any]] = []

        async def capture(data: dict[str, Any]) -> None:
            captured.append(data)

        handler.register("test.event", capture)

        data = {"customer": "cus_test", "amount": 999}
        body = _make_event_payload(
            event_id="evt_parse",
            event_type="test.event",
            data=data,
        )
        now = time.time()
        sig = _sign_payload(body, "whsec_test_secret", int(now))

        result = await handler.process(body, sig, _now=now)
        assert result.status == "ok"
        assert result.event_id == "evt_parse"
        assert captured[0]["customer"] == "cus_test"
        assert captured[0]["amount"] == 999


# ── BillingService — Checkout ────────────────────────────────────────────


class TestBillingServiceCheckout:
    """Tests for BillingService.create_checkout."""

    def _make_service(
        self,
        stripe_mock: MagicMock | None = None,
        **config_kw: str,
    ) -> BillingService:
        return BillingService(
            config=_make_config(**config_kw),
            stripe_client=stripe_mock or _make_stripe_mock(),
        )

    async def test_checkout_starter(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        result = await svc.create_checkout(
            tier=SubscriptionTier.STARTER,
            customer_id="cus_test_1",
        )
        assert isinstance(result, CheckoutResult)
        assert result.session_id == "cs_test_session_123"
        assert "checkout.stripe.com" in result.url
        assert result.tier == SubscriptionTier.STARTER
        assert result.amount_cents == 399

        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["mode"] == "subscription"
        assert call_kwargs["customer"] == "cus_test_1"
        assert call_kwargs["automatic_tax"] == {"enabled": True}

    async def test_checkout_cloud(self) -> None:
        svc = self._make_service()
        result = await svc.create_checkout(
            tier=SubscriptionTier.CLOUD,
            customer_id="cus_cloud",
        )
        assert result.amount_cents == 999
        assert result.tier == SubscriptionTier.CLOUD

    async def test_checkout_business(self) -> None:
        svc = self._make_service()
        result = await svc.create_checkout(
            tier=SubscriptionTier.BUSINESS,
            customer_id="cus_biz",
        )
        assert result.amount_cents == 9900

    async def test_checkout_free_raises(self) -> None:
        svc = self._make_service()
        with pytest.raises(ValueError, match="Free tier"):
            await svc.create_checkout(
                tier=SubscriptionTier.FREE,
                customer_id="cus_test",
            )

    async def test_checkout_enterprise_raises(self) -> None:
        svc = self._make_service()
        with pytest.raises(ValueError, match="Enterprise"):
            await svc.create_checkout(
                tier=SubscriptionTier.ENTERPRISE,
                customer_id="cus_test",
            )

    async def test_checkout_no_stripe_raises(self) -> None:
        svc = BillingService(config=_make_config(), stripe_client=None)
        with pytest.raises(RuntimeError, match="Stripe client"):
            await svc.create_checkout(
                tier=SubscriptionTier.SYNC,
                customer_id="cus_test",
            )

    async def test_checkout_with_trial(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.SYNC,
            customer_id="cus_trial",
            trial_days=14,
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["subscription_data"]["trial_period_days"] == 14

    async def test_checkout_no_trial_no_subscription_data(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.SYNC,
            customer_id="cus_no_trial",
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert "subscription_data" not in call_kwargs

    async def test_checkout_custom_urls(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.CLOUD,
            customer_id="cus_url",
            success_url="https://custom.com/ok",
            cancel_url="https://custom.com/cancel",
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["success_url"].startswith("https://custom.com/ok")
        assert call_kwargs["cancel_url"] == "https://custom.com/cancel"

    async def test_checkout_metadata(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.CLOUD,
            customer_id="cus_meta",
            metadata={"campaign": "launch"},
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["metadata"]["sovyx_tier"] == "cloud"
        assert call_kwargs["metadata"]["campaign"] == "launch"

    async def test_checkout_uses_price_map(self) -> None:
        stripe = _make_stripe_mock()
        svc = BillingService(
            config=_make_config(),
            stripe_client=stripe,
            price_map={SubscriptionTier.CLOUD: "price_cloud_monthly"},
        )
        await svc.create_checkout(
            tier=SubscriptionTier.CLOUD,
            customer_id="cus_pm",
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        line = call_kwargs["line_items"][0]
        assert line["price"] == "price_cloud_monthly"
        assert "price_data" not in line

    async def test_checkout_inline_pricing_without_map(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.SYNC,
            customer_id="cus_inline",
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        line = call_kwargs["line_items"][0]
        assert "price_data" in line
        assert line["price_data"]["unit_amount"] == 599
        assert line["price_data"]["recurring"]["interval"] == "month"

    async def test_checkout_success_url_includes_session_id(self) -> None:
        stripe = _make_stripe_mock()
        svc = self._make_service(stripe)
        await svc.create_checkout(
            tier=SubscriptionTier.STARTER,
            customer_id="cus_url2",
        )
        call_kwargs = stripe.checkout.Session.create.call_args[1]
        assert "{CHECKOUT_SESSION_ID}" in call_kwargs["success_url"]

    async def test_checkout_sets_api_key(self) -> None:
        stripe = _make_stripe_mock()
        BillingService(config=_make_config(), stripe_client=stripe)
        assert stripe.api_key == "sk_test_abc123"


# ── BillingService — Portal ──────────────────────────────────────────────


class TestBillingServicePortal:
    """Tests for BillingService.create_portal_session."""

    async def test_portal_session(self) -> None:
        stripe = _make_stripe_mock()
        svc = BillingService(config=_make_config(), stripe_client=stripe)
        result = await svc.create_portal_session(customer_id="cus_portal")
        assert isinstance(result, PortalResult)
        assert "billing.stripe.com" in result.url
        assert result.customer_id == "cus_portal"

        call_kwargs = stripe.billing_portal.Session.create.call_args[1]
        assert call_kwargs["customer"] == "cus_portal"
        assert call_kwargs["return_url"] == "https://sovyx.ai/billing"

    async def test_portal_custom_return_url(self) -> None:
        stripe = _make_stripe_mock()
        svc = BillingService(config=_make_config(), stripe_client=stripe)
        await svc.create_portal_session(
            customer_id="cus_p2",
            return_url="https://custom.com/return",
        )
        call_kwargs = stripe.billing_portal.Session.create.call_args[1]
        assert call_kwargs["return_url"] == "https://custom.com/return"

    async def test_portal_no_stripe_raises(self) -> None:
        svc = BillingService(config=_make_config(), stripe_client=None)
        with pytest.raises(RuntimeError, match="Stripe client"):
            await svc.create_portal_session(customer_id="cus_test")


# ── BillingService — extract_subscription_info ───────────────────────────


class TestExtractSubscriptionInfo:
    """Tests for BillingService.extract_subscription_info."""

    def _svc(self) -> BillingService:
        return BillingService(config=_make_config(), stripe_client=None)

    def test_basic_extraction(self) -> None:
        data = {
            "id": "sub_test_123",
            "customer": "cus_test_456",
            "status": "active",
            "current_period_start": 1700000000,
            "current_period_end": 1702592000,
            "trial_end": None,
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_cloud",
                            "unit_amount": 999,
                            "recurring": {"interval": "month"},
                        },
                    },
                ],
            },
            "metadata": {"sovyx_tier": "cloud"},
        }
        info = self._svc().extract_subscription_info(data)
        assert isinstance(info, SubscriptionInfo)
        assert info.subscription_id == "sub_test_123"
        assert info.customer_id == "cus_test_456"
        assert info.status == "active"
        assert info.amount_cents == 999
        assert info.interval == "month"
        assert info.current_period_start == 1700000000
        assert info.trial_end is None

    def test_tier_from_price_map(self) -> None:
        data = {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "active",
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_sync_monthly",
                            "unit_amount": 599,
                            "recurring": {"interval": "month"},
                        },
                    },
                ],
            },
        }
        info = self._svc().extract_subscription_info(
            data,
            price_id_map={"price_sync_monthly": SubscriptionTier.SYNC},
        )
        assert info.tier == SubscriptionTier.SYNC

    def test_tier_from_metadata_fallback(self) -> None:
        data = {
            "id": "sub_2",
            "customer": "cus_2",
            "status": "trialing",
            "items": {"data": []},
            "metadata": {"sovyx_tier": "starter"},
        }
        info = self._svc().extract_subscription_info(data)
        assert info.tier == SubscriptionTier.STARTER
        assert info.status == "trial"

    def test_no_tier_info(self) -> None:
        data = {
            "id": "sub_3",
            "customer": "cus_3",
            "status": "active",
            "items": {"data": []},
        }
        info = self._svc().extract_subscription_info(data)
        assert info.tier is None

    def test_invalid_tier_metadata_ignored(self) -> None:
        data = {
            "id": "sub_4",
            "customer": "cus_4",
            "status": "active",
            "items": {"data": []},
            "metadata": {"sovyx_tier": "nonexistent"},
        }
        info = self._svc().extract_subscription_info(data)
        assert info.tier is None

    def test_empty_data(self) -> None:
        info = self._svc().extract_subscription_info({})
        assert info.subscription_id == ""
        assert info.customer_id == ""
        assert info.status == ""
        assert info.tier is None
        assert info.amount_cents == 0

    def test_past_due_status_mapping(self) -> None:
        data = {"id": "sub_5", "customer": "cus_5", "status": "past_due", "items": {"data": []}}
        info = self._svc().extract_subscription_info(data)
        assert info.status == "past_due"


# ── Data model tests ─────────────────────────────────────────────────────


class TestDataModels:
    """Tests for frozen dataclass models."""

    def test_billing_config_frozen(self) -> None:
        config = _make_config()
        with pytest.raises(AttributeError):
            config.secret_key = "new"  # type: ignore[misc]

    def test_checkout_result_frozen(self) -> None:
        r = CheckoutResult(
            session_id="cs_1",
            url="https://test.com",
            tier=SubscriptionTier.CLOUD,
            amount_cents=999,
        )
        with pytest.raises(AttributeError):
            r.url = "changed"  # type: ignore[misc]

    def test_portal_result_fields(self) -> None:
        r = PortalResult(url="https://portal.com", customer_id="cus_1")
        assert r.url == "https://portal.com"
        assert r.customer_id == "cus_1"

    def test_webhook_event_fields(self) -> None:
        e = WebhookEvent(
            event_id="evt_1",
            event_type="test",
            data={"key": "val"},
            created=1234567890,
            api_version="2024-12-18.acacia",
        )
        assert e.event_id == "evt_1"
        assert e.api_version == "2024-12-18.acacia"

    def test_webhook_event_optional_api_version(self) -> None:
        e = WebhookEvent(event_id="evt_2", event_type="t", data={}, created=0)
        assert e.api_version is None

    def test_webhook_result_fields(self) -> None:
        r = WebhookResult(status="ok", event_id="evt_1", event_type="t")
        assert r.status == "ok"
        assert r.error is None

    def test_webhook_result_defaults(self) -> None:
        r = WebhookResult(status="ok")
        assert r.event_id == ""
        assert r.event_type == ""

    def test_subscription_info_defaults(self) -> None:
        info = SubscriptionInfo(
            subscription_id="sub_1",
            customer_id="cus_1",
            tier=None,
            status="active",
        )
        assert info.current_period_start is None
        assert info.amount_cents == 0
        assert info.interval == "month"

    def test_billing_config_defaults(self) -> None:
        config = BillingConfig(secret_key="sk_test", webhook_secret="whsec_test")
        assert config.currency == "usd"
        assert config.tax_product_code == "txcd_10000000"
        assert "sovyx.ai" in config.success_url


# ── Property-based tests ────────────────────────────────────────────────


class TestPropertyBased:
    """Hypothesis-based property tests."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(status=st.sampled_from(list(STRIPE_STATUS_MAP.keys())))
    def test_all_known_statuses_map(self, status: str) -> None:
        result = map_stripe_status(status)
        assert isinstance(result, str)
        assert len(result) > 0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(status=st.text(min_size=1, max_size=50))
    def test_unknown_status_returns_string(self, status: str) -> None:
        result = map_stripe_status(status)
        assert isinstance(result, str)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(tier=st.sampled_from(list(SubscriptionTier)))
    def test_all_tiers_have_price_and_name(self, tier: SubscriptionTier) -> None:
        assert tier in TIER_PRICES
        assert tier in TIER_NAMES
        assert isinstance(TIER_PRICES[tier], int)
        assert TIER_PRICES[tier] >= 0
