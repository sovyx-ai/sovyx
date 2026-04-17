# Module: licensing

## What it does

`sovyx.tiers` and `sovyx.license` describe the Sovyx service tiers and
validate license JWTs **offline** with an Ed25519 public key embedded in
the daemon. The open-source daemon never has access to the private key —
token issuance (signing) lives in the closed-source `sovyx-cloud`
service. A 7-day grace period after expiry keeps the daemon running in
local-only mode so a network outage or billing hiccup can't lock a user
out of their own mind.

## Key components

| Name | Responsibility |
|---|---|
| `ServiceTier` | `StrEnum` of the six tiers: `free`, `sync`, `byok_plus`, `cloud`, `business`, `enterprise`. |
| `TIER_FEATURES` | Map of tier → feature flags (`backup_daily`, `relay`, `llm_proxy`, `sso`, `team`, `ldap`, `dedicated_relay`, `sla`). |
| `TIER_MIND_LIMITS` | Map of tier → mind cap (free: 2, enterprise: 999). |
| `LicenseValidator` | JWT verifier. Uses `EdDSA` with an Ed25519 public key; returns `LicenseInfo`. |
| `LicenseClaims` | Decoded token body (`sub`, `tier`, `features`, `minds_max`, `iat`, `exp`, `refresh_before`). |
| `LicenseInfo` | Validation result (`status`, `claims`, resolved tier/features/minds_max, `grace_days_remaining`). |
| `LicenseStatus` | `StrEnum`: `valid`, `grace`, `expired`, `invalid`. |

## Tier matrix

| Tier | Minds | Features |
|---|---|---|
| `free` | 2 | — |
| `sync` | 2 | `backup_daily`, `relay` |
| `byok_plus` | 5 | `backup_daily`, `relay`, `byok_routing`, `byok_caching`, `byok_analytics` |
| `cloud` | 10 | `backup_hourly`, `relay`, `llm_proxy` |
| `business` | 25 | + `sso`, `team` |
| `enterprise` | 999 | + `ldap`, `dedicated_relay`, `sla` |

`GRACE_FEATURES = []` — during grace, features collapse to the free tier.
`minds_max` drops to `TIER_MIND_LIMITS["free"]` (2) so the user can still
run their default mind but can't provision new ones paid tiers allowed.

## Validation flow

```python
from sovyx.license import LicenseValidator

validator = LicenseValidator(public_key=ed25519_public_key)
info = validator.validate(token)

if info.is_valid:
    enabled_features = info.features
    minds_cap = info.minds_max
else:
    # LicenseStatus.EXPIRED or INVALID — free tier only.
    ...
```

`LicenseInfo.is_valid` is `True` for both `VALID` and `GRACE` — that's
the contract callers should use when gating features. `status` alone lets
the dashboard show "grace period, X days remaining" messaging.

### Grace period

When the JWT's `exp` has passed but the current time is still inside
`exp + 7 days`, the validator decodes the token with `verify_exp=False`
(the signature is re-verified; only the expiry is ignored), emits a
`WARNING` log entry, and returns `LicenseStatus.GRACE` with
`features=[]` and `minds_max=2`. Past that window the token is rejected
as `EXPIRED`.

## Security properties

- **Offline-only.** The daemon never phones home to validate a token;
  the public key is embedded at build time. A compromised or unreachable
  cloud service cannot block a paying customer from their local data.
- **No private key in the repo.** Only the public half ships with the
  daemon. Tokens are issued by `sovyx-cloud`, which is not open source.
- **Tamper-evident.** Any modification to the token — tier upgrade,
  expiry extension, mind-limit bump — invalidates the signature and
  returns `INVALID`.
- **Required claims.** `jwt.decode(..., options={"require": [...]})`
  enforces that `sub`, `tier`, `features`, `minds_max`, `iat`, and `exp`
  are present; a missing field is an `InvalidTokenError`.

## Configuration

License tokens aren't part of `system.yaml`. They are typically injected
by `sovyx-cloud` during first-run activation and stored outside the
config surface. The daemon reads them via `LicenseValidator` at startup
and re-validates on `refresh_before` crossings.

## See also

- `src/sovyx/tiers.py`, `src/sovyx/license.py` — source of truth.
- [`configuration.md`](../configuration.md) — `system.yaml` schema
  (tokens are not part of it).
- `docs-internal/IMPL-SUP-006` — tier governance spec (internal).
