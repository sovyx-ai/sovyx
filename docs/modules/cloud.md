# Módulo: cloud

## Objetivo

Camada de monetização e serviços de nuvem do Sovyx: assinaturas Stripe com 6 tiers, licenciamento offline-capable via JWT Ed25519, backup criptografado para Cloudflare R2 (zero-knowledge), scheduler GFS (Grandfather-Father-Son), dunning para recuperação de falhas de pagamento, flex balance (pay-as-you-go), usage cascade e gerenciamento de API keys.

**Estado atual: ~64% completo.** Fundamentos prontos; Stripe Connect (marketplace) e experimentos de pricing não foram implementados.

## Responsabilidades

- **Billing** — Stripe Checkout + Customer Portal + Webhook com assinatura HMAC-SHA256 (6 events cobertos).
- **Tiers** — 6 planos (`free`, `starter`, `sync`, `cloud`, `business`, `enterprise`) com price map e feature flags.
- **License** — JWT Ed25519 assinado localmente, validação offline com chave pública embutida, grace period de 7 dias, background refresh 24 h.
- **Backup** — `brain.db` via `VACUUM INTO` → gzip → Argon2id + AES-256-GCM (BackupCrypto) → upload R2 (S3-compat).
- **Scheduler** — retenção GFS: últimos N daily + M weekly + K monthly.
- **Dunning** — recuperação automática de `payment_failed` com retries progressivos.
- **Flex balance** — créditos pay-as-you-go acima do allowance do tier.
- **Usage cascade** — roteamento de cobrança: subscription allowance → flex balance → block.
- **API keys** — geração, revogação, rate-limit por key.
- **LLM proxy** — encaminha chamadas cloud para provider com observabilidade centralizada.

## Arquitetura

```
cloud/
  ├── billing.py     SubscriptionTier (6 tiers), checkout, portal, webhook (HMAC)
  ├── license.py     JWT Ed25519, grace period 7d, background refresh 24h
  ├── backup.py      VACUUM INTO → gzip → Argon2id+AES-256-GCM → R2 upload
  ├── crypto.py      BackupCrypto (passphrase → key derivation)
  ├── scheduler.py   GFS retention (daily/weekly/monthly)
  ├── dunning.py     Recuperação de payment_failed
  ├── flex.py        Saldo pay-as-you-go
  ├── usage.py       Cascade: allowance → flex → block
  ├── apikeys.py     Geração/revogação/rate-limit
  └── llm_proxy.py   Cloud-side LLM router
```

## Código real (exemplos curtos)

**`src/sovyx/cloud/billing.py`** — tiers e pricing (centavos USD):

```python
class SubscriptionTier(enum.StrEnum):
    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"

TIER_PRICES: dict[SubscriptionTier, int] = {
    SubscriptionTier.FREE: 0,
    SubscriptionTier.STARTER: 399,    # $3.99
    SubscriptionTier.SYNC: 599,       # $5.99
    SubscriptionTier.CLOUD: 999,      # $9.99
    SubscriptionTier.BUSINESS: 9900,  # $99
    SubscriptionTier.ENTERPRISE: 0,   # custom
}

WEBHOOK_TOLERANCE_SECONDS = 300        # replay protection
STRIPE_SIGNATURE_PREFIX = "v1"         # HMAC-SHA256
```

**`src/sovyx/cloud/license.py`** — features por tier:

```python
TIER_FEATURES: dict[str, list[str]] = {
    "free": [],
    "starter": ["backup_daily", "relay"],
    "sync":    ["backup_daily", "relay", "byok_routing", "byok_caching", "byok_analytics"],
    "cloud":   ["backup_hourly", "relay", "llm_proxy"],
    "business":["backup_hourly", "relay", "llm_proxy", "sso", "team"],
    "enterprise": ["backup_hourly", "relay", "llm_proxy", "sso", "team",
                   "ldap", "dedicated_relay", "sla"],
}
```

**`src/sovyx/cloud/backup.py`** — wire format:

```python
# Wire format em R2:
#   [gzip([brain.db VACUUM snapshot])] → encrypt(Argon2id+AES-256-GCM) → .enc.gz
#
# Restore reverse o pipeline: download → decrypt → decompress → integrity check
GZIP_LEVEL = 6
```

## Specs-fonte

- **SPE-033-CLOUD-SERVICES** — API do BackupService, LicenseService, Dunning.
- **IMPL-011-STRIPE-CONNECT** — marketplace billing, Express onboarding, destination charges.
- **IMPL-SUP-006-PRICING-PQL** — 6 tiers, Van Westendorp, Gabor-Granger, PQL.
- **MONETIZATION-LIFECYCLE** — fluxos lifecycle.

## Status de implementação

| Item | Status |
|---|---|
| 6 SubscriptionTiers + price map | Aligned |
| Stripe Checkout + Customer Portal | Aligned |
| Webhook handler (HMAC-SHA256, 6 events, replay protection) | Aligned |
| LicenseService (JWT Ed25519, grace 7d, refresh 24h) | Aligned |
| BackupService (VACUUM + gzip + AES-256-GCM + R2) | Aligned |
| BackupCrypto (Argon2id + AES-256-GCM) | Aligned |
| Scheduler GFS retention | Aligned |
| Dunning (payment recovery) | Aligned |
| Flex balance + Usage cascade | Aligned |
| API keys | Aligned |
| LLM proxy cloud-side | Aligned |
| Stripe Connect — webhook (20+ events completos) | Partial |
| Stripe Connect — Express onboarding | Not Implemented |
| Stripe Connect — destination charges | Not Implemented |
| Stripe Connect — refund, dispute, payout | Not Implemented |
| Stripe Tax | Not Implemented |
| Van Westendorp analyzer (4 price questions) | Not Implemented |
| Gabor-Granger analyzer (WTP) | Not Implemented |
| PQLScorer + FunnelTracker | Not Implemented |

## Divergências

**Stripe Connect (IMPL-011) parcialmente implementado** — `billing.py` cobre Checkout, Portal e **6 eventos de webhook**, mas a spec IMPL-011 pede *marketplace billing* completo:

- Express account onboarding (plugin authors recebem payout).
- Destination charges (taxa Sovyx + pagamento ao desenvolvedor).
- Refund, dispute e payout management.
- Stripe Tax para cálculo automático de impostos.
- Webhook handler com 20+ events (hoje cobre 6).

**Impacto comercial: bloqueia launch do plugin marketplace** (gap-analysis Top 10 #2).

**Pricing experiments (IMPL-SUP-006) não implementados** — `VanWestendorpAnalyzer` (4 questões de preço com curvas OPP/IPP/PMC/PME), `GaborGrangerAnalyzer` (willingness-to-pay), `PQLScorer` (Product-Qualified Lead por adoção de features) e `FunnelTracker` (conversão por etapa). Bloqueia otimização de revenue.

**Features sem doc dedicada** — `dunning.py`, `flex.py`, `usage.py` estão implementados e testados, mas não há spec dedicada (apenas menções em SPE-033 e MONETIZATION-LIFECYCLE). Oportunidade para ADR retroativa.

## Dependências

- `stripe` (SDK Python) — `billing.py`, `llm_proxy.py`.
- `pyjwt>=2` + `cryptography` — `license.py` (Ed25519).
- `boto3` ou S3-compat client — `backup.py` (R2).
- `argon2-cffi` + `cryptography` — `crypto.py` (Argon2id, AES-256-GCM).
- `sovyx.engine.errors.CloudError`.
- `sovyx.observability.logging` — todos os arquivos.

## Testes

- `tests/unit/cloud/` — webhook signature verification (replay, tamper), JWT grace period, BackupCrypto roundtrip, GFS retention logic, usage cascade decisions.
- `tests/integration/cloud/` — Stripe mock server (stripe-mock) para fluxos completos.
- Nunca testar com Stripe real, mesmo em test mode — usar `stripe-mock` ou SDK em `mock=True`.

## Referências

- `src/sovyx/cloud/billing.py` — Stripe checkout, portal, webhook.
- `src/sovyx/cloud/license.py` — JWT Ed25519, grace period.
- `src/sovyx/cloud/backup.py` — VACUUM + encrypt + R2 upload.
- `src/sovyx/cloud/crypto.py` — Argon2id + AES-256-GCM.
- `src/sovyx/cloud/scheduler.py` — GFS retention.
- `src/sovyx/cloud/dunning.py` — payment recovery.
- `src/sovyx/cloud/flex.py` — pay-as-you-go.
- `src/sovyx/cloud/usage.py` — cascade allowance → flex → block.
- `src/sovyx/cloud/apikeys.py` — API keys.
- `src/sovyx/cloud/llm_proxy.py` — cloud-side LLM.
- SPE-033-CLOUD-SERVICES — contratos de serviço.
- IMPL-011-STRIPE-CONNECT — marketplace billing (parcial).
- IMPL-SUP-006-PRICING-PQL — 6 tiers + experimentos (NOT IMPL).
- MONETIZATION-LIFECYCLE — lifecycle de assinaturas.
- `docs/_meta/gap-inputs/analysis-C-integration.md` §cloud — 64% completion.
- `docs/_meta/gap-analysis.md` Top 10 #2, #5.
