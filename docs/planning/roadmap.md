# Roadmap — v0.5 / v0.6 / v1.0

> **Scope**: planejamento derivado de `docs/_meta/gap-analysis.md` §Roadmap, alinhado com `SOVYX-V05-MASTER-MISSION.md`, `SOVYX-PRODUCTION-ROADMAP.md`, e estado real do código em `src/sovyx/` / `dashboard/`.
>
> **Vision docs**: `vps-brain-dump/memory/nodes/sovyx-60-seconds.md`, `sovyx-cloud-platform.md`, `sovyx-mission-hub.md`.

---

## Estado atual (2026-04-14)

- **v0.1 "First Breath"** ✅ released (2026-04-04). PyPI + Docker + CI green. Engine + Brain + LLM router + Telegram + CLI.
- **v0.5 "First Words"** 🔄 em polish. Dashboard + Observability + base Cloud + Voice pipeline shipped; gaps residuais em polimento.
- **Núcleo cognitivo**: ~95% feature-complete (cognitive loop 5/7 fases, brain algoritmos, context assembly, multi-provider LLM, WAL persistence, observability).
- **Dashboard**: 100% type alignment backend↔frontend. Zero critical gaps.

Ver `docs/_meta/gap-analysis.md` pra sumário por módulo (Tabela §2).

---

## v0.5 — polish cycle (atual)

Fechar pendências pequenas antes de partir pra v0.6.

### Tarefas ativas

- [ ] `dashboard/src/main.tsx`: adicionar `import "@/lib/i18n"` (1 linha — `gap-analysis.md` §dashboard).
- [ ] Auditar i18n namespace consistency entre páginas.
- [ ] Clarificar 3 stubs (Voice, Emotions, Productivity pages) como "v0.6 planned" — não remover, marcar.
- [ ] Sanitização final de docs legadas (já ocorrendo no branch de reescrita).

### Critérios de release v0.5.x

- Test coverage ≥ 95% (atualmente 96.05% — `SOVYX-V05-MASTER-MISSION` §0).
- Zero `ruff check` errors.
- Zero `mypy --strict` errors.
- Zero `bandit -r` HIGH.
- Docker build OK (amd64 + arm64).
- Dashboard `npx tsc -b` zero erros.
- 4900+ pytest passing, 400+ vitest passing.

---

## v0.6 "The Mind That Connects" — próxima major

Escopo ambicioso: fechar os **bloqueadores comerciais** (relay/marketplace/importers) + refinamentos arquiteturais críticos (emotional 3D, cognitive loop completo) + features secundárias. Ver `gap-analysis.md` §Top 10 gaps críticos.

### Bloqueadores comerciais

Cada um desbloqueia uma dimensão monetária do produto.

#### Relay Client (bridge)

- **Doc-fonte**: `IMPL-007-RELAY-CLIENT`.
- **Gap**: `RelayClient` + Opus codec 24kbps + audio ring buffer 60ms + resampling 16↔48kHz + offline queue + exponential backoff — **zero arquivos**.
- **Bloqueia**: mobile app + cloud relay inteiro.
- **Estimativa**: 3-5 dias (conforme `gap-analysis.md` v0.6 section).

#### Stripe Connect completo (cloud)

- **Doc-fonte**: `IMPL-011-STRIPE-CONNECT`.
- **Gap**: webhook atual cobre só 6 eventos (planejado 20+). Faltando Express onboarding, destination charges, refund, dispute, payout, Stripe Tax integration.
- **Bloqueia**: plugin marketplace (revenue share 85/15).
- **Cross-ref**: `memory/nodes/sovyx-cloud-platform.md` §Marketplace.

#### Conversation Importers (upgrade)

- **Doc-fonte**: `IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION`.
- **Gap**: ChatGPT (`conversations.json` tree) + Claude + Gemini + Obsidian (markdown + wikilinks) — **nenhum implementado**.
- **Bloqueia**: (a) onboarding — fricção pra migrar de outro assistente, (b) GDPR Art. 20 (portability).
- **Inclui**: `SMFExporter completo` (data portability outbound).

#### Speaker Recognition (voice)

- **Doc-fonte**: `IMPL-005-SPEAKER-RECOGNITION`.
- **Gap**: ECAPA-TDNN biometrics + enrollment + verification — zero arquivos.
- **Bloqueia**: voice auth multi-user ("knows who's speaking" — pitch em `sovyx-60-seconds.md`).

### Refinamentos arquiteturais

Divergências entre spec e código que precisam resolver antes de escalar features em cima.

#### Emotional model — migration 2D → 3D PAD

- **Divergência**: `ADR-001 §2 Option D` decidiu PAD 3D (Pleasure + Arousal + Dominance). Código guarda só 2D em `Episode` (valence+arousal) e 1D em `Concept` (valence).
- **Doc-fonte**: `ADR-001-EMOTIONAL-MODEL`, `docs/research/memory-systems.md` §7.
- **Escopo**:
  1. Schema migration (adicionar `dominance` e `valence` no `Concept`).
  2. Update de `ImportanceWeights` pra considerar 3 eixos.
  3. Backfill opcional via LLM inference sobre episodes existentes.
- **Impacto**: consolidation, context assembly, personality drift.

#### Mind emotional baseline config

- **Gap**: `MindConfig` não permite configurar `emotional_baseline` (valence/arousal/dominance) + `homeostasis_rate` por mente.
- **Doc-fonte**: `ADR-001-EMOTIONAL-MODEL`.
- **Depende**: emotional 3D migration acima.

#### Cognitive loop — CONSOLIDATE in-loop

- **Gap**: `brain/consolidation.py` existe, mas não é chamada pelo CognitiveLoop. Fica como background standalone.
- **Doc-fonte**: `SPE-003-COGNITIVE-LOOP §1.1` (7 fases canônicas).
- **Escopo**: invocar `ConsolidationCycle` periodicamente do loop (p.ex. a cada N turns ou time-based).

#### Cognitive loop — DREAM phase

- **Gap**: fase nightly de pattern discovery **não existe**.
- **Doc-fonte**: `SPE-003-COGNITIVE-LOOP §1.1`.
- **Escopo**: design + implementação de job nightly que:
  - Extrai patterns de episodes recentes.
  - Gera novos concepts derivados.
  - Refina edges Hebbian com visão de janela maior.
- **Cross-ref**: `docs/research/memory-systems.md` §10.

### Features secundárias

Importantes, não blocking commercial. Podem ser reordenadas.

- **Home Assistant bridge** (`IMPL-008-HOME-ASSISTANT`) — 10 domain entity registry, ActionSafety SAFE/CONFIRM/DENY, mDNS, WS reconnect. Distribuição estratégica (VR-048).
- **CalDAV sync** (`IMPL-009-CALDAV`) — CalendarAdapter, ctag+etag incremental, RRULE expansion, timezones.
- **Pricing experiments** (`IMPL-SUP-006-PRICING-PQL`) — Van Westendorp (4-question), Gabor-Granger (WTP), PQLScorer, FunnelTracker. Ver `docs/planning/gtm-strategy.md` §pricing.
- **Voice Cloning** (`IMPL-SUP-002-VOICE-CLONING`) — speaker adaptation. Feature premium.
- **Parakeet TDT** (`IMPL-SUP-004-PARAKEET-TDT`) — text detection / monolingual fallback.
- **CLI REPL** (`SPE-015-CLI-TOOLS`) — multi-line interactive, auto-complete, history.
- **CLI admin utilities** — DB inspection, config reset, user/mind management.
- **Vector search exposure** (persistence) — validar/expor ANN queries via sqlite-vec (gap `[VERIFY]`).
- **LLM streaming → TTS** — pipelinar chunks pra latência end-to-end (`gap-analysis.md` §llm).
- **BYOK token isolation** por-user API key — `gap-analysis.md` §llm.

---

## v1.0 "The Mind That Remembers" — estabilidade + plugin marketplace

Marco de GA. Foco em:

- **Plugin sandbox v2** — deferido intencionalmente em v0.5/v0.6. Layers 5-7:
  - seccomp-BPF (Linux).
  - Linux namespaces (pid, net, mount).
  - macOS Seatbelt.
  - **Doc-fonte**: `IMPL-012-PLUGIN-SANDBOX` layers 5-7 + `18 escape vectors mapped`.
- **Subprocess IPC** — mover plugins pra subprocess isolated. v0.5 roda in-process (layers 0-4) — seguro o suficiente mas não defense-in-depth pleno.
- **Zero-downtime update / rollback** — atualmente parcial. Precisa completar:
  - Blue-green atomic switch.
  - Rollback automático em failure health check pós-upgrade.
- **Redis caching** (opcional per `ADR-004-DATABASE-STACK`) — pra deployments multi-instance ou high-throughput. Não necessário pra single-user.
- **Full GDPR compliance** suite — depende de Importers (v0.6) + audit trail hardening.
- **Enterprise identity** (`SPE-034-ENTERPRISE-IDENTITY`) — SSO, SCIM, audit log.
- **Multi-Mind** (`SPE-029-MULTI-MIND`) — múltiplas mentes rodando no mesmo daemon com isolation forte.

### Critérios de release v1.0

Além dos critérios de v0.5.x:

- Plugin sandbox penetrated por security audit externa (budget: contratar firm).
- Trademark SOVYX registrado e confirmado (INPI Brasil em andamento — `sovyx-trademark-inpi.md`).
- Foundation / dual-license structure operacional (ver `VR-016 §AP-08` defense).
- ≥50 plugins no marketplace (ou 10 plugins oficiais + SDK público funcional).
- SLA 99.9% no Cloud tier (requer observability + dunning + graceful degradation validados em produção).

---

## Milestones trimestrais (best-effort)

Baseado em `SOVYX-V05-MASTER-MISSION` §timeline + `SOVYX-PRODUCTION-ROADMAP`. Datas são orientativas com 1 dev (Guipe/Nyx); ajustar conforme realidade.

| Trimestre | Marco | Escopo principal |
|---|---|---|
| **2026 Q2** (em curso) | v0.5.x polish | i18n, stubs, doc rewrite |
| **2026 Q3** | v0.6 preview | Relay client + Stripe Connect + Importers |
| **2026 Q3-Q4** | v0.6 GA | Emotional 3D migration + CONSOLIDATE/DREAM + Speaker Recognition |
| **2026 Q4** | v0.6 polish | HA bridge + CalDAV + pricing experiments |
| **2027 Q1** | v1.0 preview | Sandbox v2 + subprocess IPC + Multi-Mind |
| **2027 Q1-Q2** | v1.0 GA | Security audit + Foundation + marketplace launch |

Ver `docs/planning/milestones.md` pra detalhamento e dependências.

---

## Princípios não-negociáveis do roadmap

Inspirados em `SOVYX-VR-016-FAILURE-SYNTHESIS.md` (ver `docs/research/competitive-analysis.md` §5).

- **No hardware Year 1** (AP-01 defense). Software-first em hardware existente (Pi, mini PC, VPS).
- **AGPL-3.0 inegociável** (AP-02 defense). Nunca pivotar pra BSL/SSPL.
- **Empresa desde dia 1** (AP-03 defense). Dual-license + foundation plan.
- **Ship iterativo — nunca rewrite perpétuo** (AP-04 defense).
- **"Mind" identity locked** — consumer-first forever (AP-05 defense). Enterprise features são add-ons.
- **LLM-native** (AP-07 defense). Multi-provider. Nunca voltar a intent-matching.
- **Zero fake metrics** (AP-09 defense). Otimizar contributors / downloads / dependents.
- **Underpromise, overdeliver** (AP-10 defense). README mostra só o que funciona hoje.
- **Multi-provider everything** (AP-12 defense). Zero vendor lock-in.

---

## Referências

- `docs/_meta/gap-analysis.md` §Roadmap + §Top 10 gaps críticos + §Top divergências
- `vps-brain-dump/memory/confidential/sovyx-bible/missions/SOVYX-V05-MASTER-MISSION.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/missions/SOVYX-PRODUCTION-ROADMAP.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/plans/SOVYX-BKD-PLN-002-ROADMAP.md`
- `vps-brain-dump/memory/nodes/sovyx-60-seconds.md` §Roadmap
- `vps-brain-dump/memory/nodes/sovyx-cloud-platform.md`
- `docs/research/competitive-analysis.md` §5 (failure patterns)
- `docs/research/memory-systems.md` §7 (divergence ADR-001)
- `docs/research/llm-landscape.md` §3-6 (cloud gaps)

---

_Última revisão: 2026-04-14._
