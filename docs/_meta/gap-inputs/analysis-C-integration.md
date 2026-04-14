# Gap Analysis — Integration & Lifecycle (bridge, cloud, upgrade, cli)

## Módulo: bridge

### Docs-fonte principais
- SOVYX-BKD-IMPL-007-RELAY-CLIENT.md (Opus codec, audio ring buffer, WebSocket)
- SOVYX-BKD-IMPL-008-HOME-ASSISTANT.md (Entity registry, safety guards, mDNS)
- SOVYX-BKD-IMPL-009-CALDAV.md (CalDAV sync protocol, RRULE expansion)
- SOVYX-BKD-SPE-014-COMMUNICATION-BRIDGE.md (Multi-channel normalization)

### Código real
- src/sovyx/bridge/manager.py (BridgeManager, conversation routing)
- src/sovyx/bridge/channels/telegram.py (TelegramChannel, aiogram)
- src/sovyx/bridge/channels/signal.py (SignalChannel, signal-cli-rest-api)
- src/sovyx/bridge/protocol.py (InboundMessage, OutboundMessage)

### Planejado vs Implementado

#### ✅ IMPLEMENTADO
- Telegram channel (text + inline buttons)
- Signal channel (plain text via REST)
- BridgeManager message routing + financial confirmation
- Person resolver + conversation tracker

#### ❌ NÃO IMPLEMENTADO

**Relay Client (IMPL-007) — WebSocket audio streaming**
- RelayClient class
- Opus codec (24kbps, 20ms frames, VOIP mode, DTX/FEC)
- Audio ring buffer (jitter compensation, 60ms latency)
- Sample rate resampling (16KHz↔48KHz)
- Offline message queue
- Exponential backoff + jitter for reconnection
Citação: IMPL-007 §1.1-1.3, SPE-011

**Home Assistant (IMPL-008) — Smart home integration**
- HomeAssistantBridge class
- Entity domain registry (10 domains)
- ActionSafety framework (SAFE/CONFIRM/DENY)
- mDNS discovery
- WebSocket reconnection + retry
Citação: IMPL-008 §1.1-1.6

**CalDAV (IMPL-009) — Calendar synchronization**
- CalendarAdapter / CalDAVClient class
- Incremental sync (ctag + etag)
- RRULE expansion via dateutil
- Timezone handling (DATE vs DATE-TIME, DST)
- Conflict resolution
Citação: IMPL-009 §1.1-1.3

---

## Módulo: cloud

### Docs-fonte principais
- SOVYX-BKD-IMPL-011-STRIPE-CONNECT.md (marketplace billing, Express, destination charges)
- SOVYX-BKD-IMPL-SUP-006-PRICING-PQL.md (Van Westendorp, Gabor-Granger, PQL)
- SOVYX-BKD-SPE-033-CLOUD-SERVICES.md, MONETIZATION-LIFECYCLE.md

### Código real
- src/sovyx/cloud/billing.py (SubscriptionTier, checkout, webhook handler)
- src/sovyx/cloud/license.py (JWT Ed25519 tokens)
- src/sovyx/cloud/backup.py (R2 encryption, VACUUM)
- src/sovyx/cloud/scheduler.py (GFS retention)
- src/sovyx/cloud/dunning.py (failed payment recovery)
- src/sovyx/cloud/flex.py (pay-as-you-go)
- src/sovyx/cloud/usage.py (cascade charges)

### Planejado vs Implementado

#### ✅ IMPLEMENTADO
- Billing fundamentals (6 tiers, checkout/portal, webhook handler)
- Webhook signature verification (HMAC-SHA256)
- License service (JWT, grace period)
- Backup (encryption, R2)
- Scheduler (GFS retention)
- Dunning (failed payment recovery)
- Flex balance + usage cascade
- API key management

#### ⚠️ PARCIALMENTE

**Stripe Connect (IMPL-011) — Marketplace Billing**
- Webhook handler YES, but only 6 events
- Express account onboarding NO
- Destination charge creation NO
- Refund flow NO
- Dispute handling NO
- Payout management NO
- Stripe Tax integration NO
- Complete webhook (20+ events) NO
Citação: IMPL-011 §0-2.1

#### ❌ NÃO IMPLEMENTADO

**Pricing Experiments (IMPL-SUP-006)**
- VanWestendorpAnalyzer (4 price questions, curves)
- GaborGrangerAnalyzer (willingness-to-pay)
- PricingExperiments (A/B testing)
- PQLScorer (feature adoption → revenue qualified)
- FunnelTracker (conversion pipeline)
Citação: IMPL-SUP-006 §SPEC 1-2

---

## Módulo: upgrade

### Docs-fonte principais
- SOVYX-BKD-SPE-028-UPGRADE-MIGRATION.md
- SOVYX-BKD-IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION.md

### Código real
- src/sovyx/upgrade/doctor.py (10+ health checks)
- src/sovyx/upgrade/importer.py (MindImporter, SMF/ZIP)
- src/sovyx/upgrade/exporter.py (SMF export)
- src/sovyx/upgrade/schema.py (SemVer, migrations)
- src/sovyx/upgrade/backup_manager.py
- src/sovyx/upgrade/blue_green.py

### Planejado vs Implementado

#### ✅ IMPLEMENTADO
- Doctor (10+ checks)
- Schema migrations
- Mind import (SMF + ZIP)
- Mind export (SMF)
- Backup manager + rollback
- Blue-green upgrade

#### ❌ NÃO IMPLEMENTADO

**Conversation Importers (IMPL-SUP-015 — UPG-007/008/009)**
- ChatGPTImporter
- ClaudeImporter
- GeminiImporter
Citação: IMPL-SUP-015 §SPEC 1, UPG-007/008/009

**Obsidian + InterMind (IMPL-SUP-015)**
- ObsidianImporter (markdown + wikilinks)
- InterMindBridge (multi-instance sync)
- CursorPagination (REST API)
- SMFExporter (GDPR Art. 20)
Citação: IMPL-SUP-015 §SPEC 4, UPG-010, MMD-009, API-014, PER-013

---

## Módulo: cli

### Docs-fonte principais
- SOVYX-BKD-SPE-015-CLI-TOOLS.md (Typer + Rich, JSON-RPC)

### Código real
- src/sovyx/cli/main.py (Typer app, init/start/stop/status/token/doctor)
- src/sovyx/cli/rpc_client.py (DaemonClient, Unix socket)
- src/sovyx/cli/commands/*.py (brain, mind, plugin, dashboard, logs)

### Planejado vs Implementado

#### ✅ IMPLEMENTADO
- Typer CLI framework
- Unix socket JSON-RPC 2.0
- Core commands (init, start, stop, status, token, doctor)
- Brain commands (search, stats, analyze)
- Mind commands (list, status)
- Plugin commands (list/install/enable/disable/remove/validate/create)
- Rich output + --json option

#### ⚠️ PARCIALMENTE

**CLI Daemon Communication (SPE-015 §2)**
- DaemonClient YES
- DaemonRPCServer sketch only
- Methods stub (status, shutdown) but not full registry
Citação: SPE-015 §2.1-2.2

#### ❌ NÃO IMPLEMENTADO

**REPL (SPE-015)**
- Interactive REPL mode
- Multi-line input, auto-complete, history
Citação: SPE-015 §3.1

**Admin Utilities (SPE-015)**
- Admin subcommand group
- Database inspection tools
- Config reset/migrate
- User/mind management
Citação: SPE-015 §3.2

---

## Contagem de Gaps por Módulo

Module      Planned  Impl   Partial  Not Impl  Completion
bridge        13      5        0        8        38%
cloud         22     13        1        8        64%
upgrade       15      8        0        7        53%
cli           18     10        1        7        61%
TOTAL         68     36        2       30        56%

---

## Top 3 Insights

1. **Audio Streaming Missing (bridge 38%)**
   Relay Client fully spec'd but zero code. Blocks mobile app + cloud relay (major revenue).
   Effort: 3-5 days. Impact: Can't ship mobile.

2. **Marketplace Billing Incomplete (cloud 64%)**
   Stripe Connect + pricing experiments not coded. Webhook 6/20+ events. Blocks plugin marketplace.
   Impact: Can't launch revenue model.

3. **Conversation Importers Missing (upgrade 53%)**
   ChatGPT/Claude/Gemini/Obsidian importers unimplemented. Blocks user onboarding + GDPR Art. 20.
   Impact: New user acquisition at risk.

---

Archive path: /e/sovyx/docs/_meta/gap-inputs/analysis-C-integration.md
