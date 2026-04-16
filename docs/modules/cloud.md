# Module: cloud

## What it does

`sovyx.cloud` is the monetization and managed-services layer: Stripe-backed subscriptions across six tiers, offline-capable licensing with JWT Ed25519, zero-knowledge backups to Cloudflare R2, GFS retention, dunning for failed payments, a flex balance for pay-as-you-go credits, usage cascade from allowance to flex, API key management, and a cloud-side LLM proxy.

Every user-visible operation is local-first: the daemon works without any cloud services, and cloud features are unlocked by a signed license token that validates offline.

## Key components

| Name | Responsibility |
|---|---|
| `BillingService` | Stripe Checkout, Customer Portal, webhook dispatch with signature verification. |
| `WebhookHandler` | HMAC-SHA256 signature check + replay protection + registry of event handlers. |
| `LicenseService` | Issues and validates JWT Ed25519 license tokens; 7-day grace period, 24 h background refresh. |
| `BackupService` | `brain.db` → `VACUUM INTO` → gzip → `Argon2id + AES-256-GCM` → upload to R2. |
| `BackupCrypto` | Argon2id key derivation + AES-256-GCM encryption. |
| `BackupScheduler` | Per-tier schedule plus GFS retention pruning. |
| `DunningService` | Progressive retries and downgrade flow when a payment fails. |
| `FlexBalanceService` | Prepaid balance topped up via Stripe, debited atomically by usage. |
| `UsageCascade` | Billing router: allowance → flex balance → block. |
| `APIKeyService` | Generation, revocation, scope and rate-limit per key. |
| `LLMProxyService` | Cloud-side LLM router with metering and provider fallback. |

## Pricing tiers

```python
# src/sovyx/cloud/billing.py — prices in cents (USD)
class SubscriptionTier(enum.StrEnum):
    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


TIER_PRICES: dict[SubscriptionTier, int] = {
    SubscriptionTier.FREE: 0,
    SubscriptionTier.STARTER: 399,     # $3.99
    SubscriptionTier.SYNC: 599,        # $5.99
    SubscriptionTier.CLOUD: 999,       # $9.99
    SubscriptionTier.BUSINESS: 9900,   # $99
    SubscriptionTier.ENTERPRISE: 0,    # custom pricing
}
```

| Tier | Price | Features |
|---|---|---|
| `free` | $0 | Local-only Sovyx. |
| `starter` | $3.99 / month | Daily backup, relay. |
| `sync` | $5.99 / month | Daily backup, relay, BYOK routing, BYOK caching, BYOK analytics. |
| `cloud` | $9.99 / month | Hourly backup, relay, managed LLM proxy. |
| `business` | $99 / month | Everything in `cloud` plus SSO and team. |
| `enterprise` | custom | Adds LDAP, dedicated relay, SLA. |

Mind count limits mirror the tier: `free` and `starter` → 2, `sync` → 5, `cloud` → 10, `business` → 25, `enterprise` → 999.

## Licensing

`LicenseService` signs a JWT with an Ed25519 private key on the cloud and distributes the token to the daemon. Validation is offline: the public key is embedded in the client, so the daemon does not need to reach the cloud to check the license.

```python
# src/sovyx/cloud/license.py
TOKEN_VALIDITY_DAYS = 7
REFRESH_BEFORE_DAYS = 5
GRACE_PERIOD_DAYS = 7
REFRESH_INTERVAL_SECONDS = 86400  # 24 h background refresh
JWT_ALGORITHM = "EdDSA"
```

After expiry the daemon enters a 7-day grace period and continues to run local-only features. `LicenseStatus` is one of `active`, `grace`, `expired`, `invalid`.

## Backups

Backups are zero-knowledge: the encryption key is derived from the user's passphrase on the device. The cloud only ever sees ciphertext.

```
brain.db
   │
   ▼  (VACUUM INTO — consistent snapshot)
   ▼  (gzip, level 6)
   ▼  (BackupCrypto: Argon2id → AES-256-GCM)
   ▼
R2 bucket (<user_id>/<mind_id>/<backup_id>.enc.gz)
```

`BackupMetadata` records the SHA-256 checksum, compressed and original sizes, the Sovyx version, and the brain schema version for safe restore. `RestoreResult.integrity_ok` confirms the SHA-256 match on download.

## GFS retention

`BackupScheduler` prunes old backups using a Grandfather-Father-Son policy.

```python
@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
```

Tiers get different schedules: `starter`/`sync` → daily snapshots, `cloud`/`business`/`enterprise` → hourly. The pruner returns a `PruneResult(kept=..., pruned=...)` for observability.

## Webhook dispatch

Stripe events hit a single endpoint, get their signature verified, and are routed through a registry.

```python
WEBHOOK_TOLERANCE_SECONDS = 300       # replay protection
STRIPE_SIGNATURE_PREFIX = "v1"        # HMAC-SHA256


# Handlers register themselves — no hard-coded dispatch.
@webhook_handler.register("customer.subscription.updated")
async def _on_sub_updated(event: WebhookEvent) -> None: ...
```

`WebhookSignatureError` is raised if the HMAC is wrong or the timestamp is older than 300 s. `WebhookPayloadError` is raised if the payload is malformed.

## Flex balance and usage cascade

`FlexBalanceService` gives users a prepaid balance for pay-as-you-go usage above the subscription allowance. `UsageCascade` routes every chargeable event through three stages.

```
charge(user, amount)
   │
   ├─► allowance stage: subscription quota left → consume it
   │
   ├─► flex stage: flex balance sufficient → debit
   │
   └─► blocked stage: raise InsufficientBalanceError
```

`ChargeResult` reports which stage paid (`allowance` / `flex` / `blocked`) and the remaining balance, so the dashboard can show accurate forecasts.

## Dunning

When Stripe signals `invoice.payment_failed`, `DunningService` walks the customer through progressive states.

| State | Behavior |
|---|---|
| `warning` | Email reminder, Stripe retries. |
| `suspended` | Cloud features disabled; local features continue. |
| `cancelled` | Subscription terminated; downgrade to `free`. |

Each transition emits a `DunningRecord` with the attempt count and the email type sent (`EmailType`).

## API keys

`APIKeyService` issues keys for the LLM proxy and other cloud endpoints. Keys carry a `Scope` flag, a per-tier `RateTier` rate limit, and a revocation state. `APIKeyValidation` returns whether the key is valid and whether it was rate-limited.

## LLM proxy

`LLMProxyService` is the cloud-side counterpart to the local `LLMRouter`. It accepts requests from `cloud` and `business` subscribers, forwards them to the selected provider via `LiteLLMBackend`, and returns a `ProxyResponse` with token counts, cost, and latency for metering. `MeteringSnapshot` aggregates usage for billing.

## Errors

| Exception | Raised when |
|---|---|
| `WebhookSignatureError` | HMAC mismatch or timestamp outside 300 s tolerance. |
| `WebhookPayloadError` | Stripe event payload is malformed. |
| `FlexError` | Base for flex balance errors. |
| `InvalidTopupAmountError` | Top-up outside allowed range. |
| `InsufficientBalanceError` | Debit exceeds balance. |
| `MaxBalanceExceededError` | Top-up would exceed the per-account cap. |
| `PaymentError` | Stripe rejected a charge. |
| `RateLimitExceededError` | Proxy rate limit exceeded for a key or tier. |
| `ModelNotFoundError` | Requested model is not mapped in the proxy. |
| `AllProvidersFailedError` | Every provider in the fallback chain failed. |

## Configuration

```yaml
cloud:
  billing:
    secret_key: "${STRIPE_SECRET_KEY}"
    webhook_secret: "${STRIPE_WEBHOOK_SECRET}"
    success_url: https://sovyx.ai/billing/success
    cancel_url:  https://sovyx.ai/billing/cancel
    portal_return_url: https://sovyx.ai/billing
    currency: usd
  backup:
    r2_endpoint_url: https://<account>.r2.cloudflarestorage.com
    r2_bucket: sovyx-backups
    r2_access_key_id:     "${R2_KEY_ID}"
    r2_secret_access_key: "${R2_SECRET}"
  proxy:
    providers: [anthropic, openai, google]
    rate_tier: cloud
```

## Roadmap

- **Stripe Connect for marketplace** — Express onboarding for plugin authors, destination charges that split revenue, refund / dispute / payout handling, Stripe Tax, and a complete set of webhook handlers (today the registry covers the subscription lifecycle).
- **Pricing experiments** — Van Westendorp, Gabor-Granger, PQL scoring, and a funnel tracker to feed pricing decisions.
- **Dedicated specs for flex, usage, and dunning** — currently implemented and tested but documented only through code.

## See also

- Source: `src/sovyx/cloud/billing.py`, `license.py`, `backup.py`, `crypto.py`, `scheduler.py`, `dunning.py`, `flex.py`, `usage.py`, `apikeys.py`, `llm_proxy.py`.
- Tests: `tests/unit/cloud/`, `tests/integration/cloud/` (Stripe is exercised via `stripe-mock`, never the real API).
- Related modules: [`engine`](./engine.md) for `CloudError`, [`llm`](./engine.md) for the local router that pairs with the cloud proxy.
