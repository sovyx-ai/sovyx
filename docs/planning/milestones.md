# Milestones

> **Scope**: timeline de marcos do Sovyx — passado recente (pivot Erebus→Sovyx, v0.1 First Breath, v0.5 polish) e futuros (v0.6, v1.0). Dependencies por milestone + critérios de release.
>
> **Base**: `SOVYX-MISSION-AUDIT-V8.md`, `SOVYX-V05-MASTER-MISSION.md`, `SOVYX-PRODUCTION-ROADMAP.md`, `docs/_meta/gap-analysis.md`, `memory/daily/2026-04-13.md`.

---

## 1. Timeline — passado (até 2026-04-14)

### 1.1 Erebus → Sovyx pivot

**2026-03-21**: decisão formal de pivot do projeto Erebus (prediction markets / edge hunter) pra Sovyx (Sovereign Minds Engine — AI companion). Motivação: foco em consumer-facing product com moat defensível (cognitive architecture).

**2026-03-24**: logo v4 finalizado. Identidade visual locked.

### 1.2 Mission audits & specs

**2026-03-24 a 2026-03-30**: 8 auditorias progressivas do blueprint (`SOVYX-MISSION-AUDIT-V1..V8`). 68 issues corrigidos em 8 passadas — cada audit operou em camada progressiva de profundidade:

- V1-V3: cobertura de spec.
- V4-V6: Python compila?
- V7: runtime executa?
- V8: interfaces encaixam?
- V9 (adicional): data flow, concorrência, deadlocks, segurança semântica — +9 issues.

**Status**: blueprint audit 100% (77 issues acumulados, 0 CRITICAL residual).

### 1.3 v0.1 "First Breath" — released

**2026-04-04**: `v0.1.0` released.

- PyPI: `pip install sovyx` funcional.
- Docker: `ghcr.io/sovyx-ai/sovyx:0.1.0` (amd64 + arm64).
- CI: GitHub Actions (lint, typecheck, security, tests 3.11 + 3.12, Docker build, PyPI publish).
- Testes: 1233 passed, 96.05% coverage.
- Smoke test: pipeline Telegram completo ao vivo com 4 mensagens.

### 1.4 Dashboard + Observability

Cronograma consolidado em `SOVYX-PRODUCTION-ROADMAP.md`:

- **Fase 1 Observability**: structlog + OTel + health checks + metrics + tracing. 12 PRs, 1445 tests.
- **Fase 2 Dashboard Backend**: FastAPI + auth + 7 API routes + WebSocket + events. 22 PRs, 169 tests, 105 pentest attacks.
- **Fase 2 Dashboard Frontend**: React + Vite + Tailwind + shadcn + 11 pages + 3 stubs. Zero critical type drifts entre backend/frontend.

### 1.5 CI deadlock fix

**2026-04-13**: deadlock em CI resolvido (aiosqlite + pytest-asyncio interaction). Ver `memory/daily/2026-04-13.md` + `memory/nodes/aiosqlite-deadlock-protocol.md`.

Context: testes async com SQLite compartilhado em xdist pool deadlocavam intermitentemente. Protocolo anti-deadlock documentado; CI verde desde então.

### 1.6 Doc rewrite (em curso)

**2026-04-14**: fase de reescrita da documentação. 853 arquivos do VPS brain dump triados (`docs/_meta/triage-index.md`). Gap analysis completo (`docs/_meta/gap-analysis.md`). Novos docs consolidados em `docs/{architecture,development,modules,planning,research,security}/`.

---

## 2. Milestones próximos

### 2.1 v0.5.x polish cycle — **agora**

- **Target**: 2026-04-30.
- **Escopo**:
  - Fix `dashboard/src/main.tsx` i18n import.
  - Auditar i18n namespace consistency.
  - Marcar 3 stubs como "v0.6 planned" (Voice, Emotions, Productivity pages).
  - Finalizar doc rewrite (este trabalho).
  - Deprecar `vps-brain-dump/` após confirmação de migração.
- **Critérios release** (ver §3 abaixo).
- **Dependencies**: nenhuma externa.

### 2.2 v0.6 "The Mind That Connects"

- **Target preview**: 2026-Q3 (Jul-Set).
- **Target GA**: 2026-Q4 (Out-Dez).

**Escopo macro** (ver `docs/planning/roadmap.md` §v0.6 para detalhe):

1. **Bloqueadores comerciais**:
   - Relay Client (bridge) — IMPL-007.
   - Stripe Connect completo — IMPL-011.
   - Conversation Importers — IMPL-SUP-015.
   - Speaker Recognition — IMPL-005.
2. **Refinamentos arquiteturais**:
   - Emotional 2D → 3D PAD migration — ADR-001.
   - Mind emotional baseline config.
   - CognitiveLoop CONSOLIDATE in-loop.
   - CognitiveLoop DREAM phase.
3. **Features secundárias**:
   - HA bridge + CalDAV.
   - Pricing experiments (Van Westendorp, Gabor-Granger, PQL).
   - Voice Cloning + Parakeet TDT.
   - CLI REPL + admin.
   - Vector search exposure.

**Dependencies**:

- Preview release depende dos 4 bloqueadores comerciais.
- GA depende de: preview + emotional migration + consolidate/dream + security review.

### 2.3 v1.0 "The Mind That Remembers" — marketplace launch

- **Target preview**: 2027-Q1.
- **Target GA**: 2027-Q1/Q2.

**Escopo macro**:

- Plugin sandbox v2 (seccomp-BPF, namespaces, macOS Seatbelt).
- Subprocess IPC pra plugins.
- Zero-downtime update / rollback completo.
- Redis caching opcional (ADR-004).
- Multi-Mind (SPE-029).
- Enterprise identity (SPE-034).
- Plugin marketplace launch (requer Stripe Connect de v0.6).
- Full GDPR compliance suite.

**Dependencies**:

- v0.6 GA completo.
- Security audit externa (plugin sandbox).
- Trademark SOVYX registrado (INPI Brasil — `sovyx-trademark-inpi.md`).
- Foundation / dual-license structure operacional.

---

## 3. Critérios de release (checklist genérico)

Aplicado a **qualquer** release (v0.5.x, v0.6.x, v1.0.x). Derivado de `CLAUDE.md` §Quality Gates + `SOVYX-V05-MASTER-MISSION` §0 (v0.1 evidence).

### 3.1 Quality gates mandatórios

- [ ] `uv run ruff check src/ tests/` — zero erros.
- [ ] `uv run ruff format --check src/ tests/` — zero erros.
- [ ] `uv run mypy src/` — zero erros em modo `strict`.
- [ ] `uv run bandit -r src/sovyx/ --configfile pyproject.toml` — zero HIGH.
- [ ] `uv run pytest tests/ --timeout=20` — 100% passando.
- [ ] Test coverage ≥ 95% (baseline: 96.05% em v0.1.0).
- [ ] `npx tsc -b tsconfig.app.json` (dashboard) — zero erros.
- [ ] `npx vitest run` (dashboard) — 100% passando.

### 3.2 Build & deploy gates

- [ ] Docker build amd64 OK.
- [ ] Docker build arm64 OK.
- [ ] PyPI package build OK + wheel válido.
- [ ] CHANGELOG atualizado com conventional commits aggregated.
- [ ] Version bump em `pyproject.toml` + `src/sovyx/__init__.py`.
- [ ] Git tag `vX.Y.Z` criado.

### 3.3 Smoke tests

- [ ] `sovyx init` em tmpdir limpo completa sem erro.
- [ ] `sovyx start` sobe daemon + dashboard em <15s.
- [ ] Pipeline Telegram: mensagem entra → brain retrieval → LLM → resposta sai. End-to-end <10s.
- [ ] Dashboard loga token, renderiza 11 páginas, WebSocket conecta.
- [ ] `sovyx doctor` passa 10 checks.

### 3.4 v0.6-specific gates

Além do baseline:

- [ ] Relay Client: handshake WebSocket + Opus 24kbps validado em device real.
- [ ] Stripe Connect: 20+ webhook events processados em staging.
- [ ] Importers: pelo menos 3 de 4 (ChatGPT/Claude/Gemini/Obsidian) shipped.
- [ ] Speaker Recognition: enrollment + verification funcional em demo.
- [ ] Emotional migration: backfill executa sem data loss em DB de teste com 10K episodes.

### 3.5 v1.0-specific gates

Além dos de v0.6:

- [ ] Plugin sandbox v2 penetrado por security audit externa (relatório anexado).
- [ ] Zero-downtime update: rollback automático em failure de health check pós-upgrade.
- [ ] SLA 99.9% no Cloud tier sustained por 30 dias em staging.
- [ ] Trademark SOVYX registrado.
- [ ] Foundation structure operacional (if applicable).
- [ ] ≥50 plugins no marketplace (ou 10 oficiais + SDK público validado).

---

## 4. Dependency map

Cada milestone desbloqueia ou depende de gaps específicos. Rastreado em `docs/_meta/gap-analysis.md` §Top 10 gaps críticos.

```
v0.1 (done)
  ├─ engine + brain + cognitive + persistence + llm + context + mind
  └─ telegram + cli + observability + packaging

v0.5 (done + polish atual)
  ├─ dashboard (BE + FE)
  ├─ signal bridge
  ├─ voice base (STT/TTS/VAD/wake/Wyoming)
  ├─ cloud base (billing, license, backup, dunning, flex, usage)
  ├─ plugins v1 (sandbox layers 0-4, 5 official plugins)
  └─ upgrade (doctor, SMF import, migrations, backup)

v0.6 preview
  ├─ REQUIRES: v0.5 GA
  ├─ relay client        ← unblocks mobile app
  ├─ stripe connect      ← unblocks marketplace revenue
  ├─ importers           ← unblocks onboarding + GDPR Art. 20
  └─ speaker recognition ← unblocks voice multi-user

v0.6 GA
  ├─ REQUIRES: v0.6 preview
  ├─ emotional 2D→3D migration (ADR-001 alignment)
  ├─ consolidate in-loop
  ├─ dream phase
  ├─ HA bridge + CalDAV
  ├─ pricing experiments
  ├─ voice cloning + parakeet
  └─ CLI REPL + admin

v1.0 preview
  ├─ REQUIRES: v0.6 GA
  ├─ sandbox v2 (seccomp/namespaces/Seatbelt)
  ├─ subprocess IPC
  └─ multi-mind

v1.0 GA
  ├─ REQUIRES: v1.0 preview
  ├─ zero-downtime update
  ├─ redis caching optional
  ├─ enterprise identity
  ├─ security audit externa
  ├─ foundation structure
  ├─ trademark registrado
  └─ marketplace launch
```

---

## 5. Risk register & mitigations

Herdado de `SOVYX-DEVELOPMENT-STRATEGY.md` §2.1. Atualizado pra 2026-04-14.

| # | Risco | Impacto | Prob. | Mitigação |
|---|---|---|---|---|
| R1 | Brain é complexo demais numa iteração | 🟢 Mitigado | — | v0.1 shippou; 3 sub-fases no plano original |
| R2 | Cognitive Loop integra com tudo — bugs de interface | 🟢 Mitigado | — | Contract-first via ABCs; CONSOLIDATE/DREAM são adições, não refactor |
| R3 | sqlite-vec no ARM64 | 🟢 Mitigado | — | Validado; fallback FTS5 documentado |
| R4 | Context window limitation (Nyx) | 🟡 Contínuo | Alta | Task decomposition com spec-slices |
| R5 | Token budget management | 🟢 Mitigado | — | ContextAssembler shipped com adaptive budget |
| R6 | Spreading activation performance em escala | 🟡 Ativo | Baixa | Benchmark pendente em 10K+ concepts |
| R7 | E5-small-v2 ONNX ARM64 | 🟢 Mitigado | — | Working em prod |
| R8 | LLM Router failover chain | 🟢 Mitigado | — | Multi-provider shipped |
| R9 | Burnout de 1 developer | 🔴 Ativo | Média | Milestones a cada 2 sem; vitórias incrementais |
| R10 (novo) | **Patent troll** (lesson from Mycroft VR-011) | 🔴 Alto | Baixa | Defensive patent provisional pre-v1.0; war chest mínimo legal |
| R11 (novo) | **Security incident marketplace** (OpenClaw parallel VR-001) | 🔴 Crítico | Média | Plugin sandbox layers 0-4 já duro; layers 5-7 pra v1.0; review CI obrigatório |
| R12 (novo) | **Community não cresce** | 🟡 Médio | Média | Ver `gtm-strategy.md` §8 (D-90 playbook) |

---

## 6. Métricas de execução por milestone

Rastreadas pelo próprio time:

- **v0.1**: 34 PRs, 1233 tests, 88+ commits em main.
- **v0.5** (atual): 4900+ tests (~4× de v0.1), 40+ módulos, dashboard 14 páginas.
- **v0.6** (alvo): +20 tests por bloqueador comercial, +10 por refinamento. Estimado +500 tests total.
- **v1.0** (alvo): +300 tests em security sandboxing, +200 em multi-mind isolation.

---

## 7. Referências

Missões e roadmaps internos:

- `vps-brain-dump/SOVYX-MISSION-AUDIT-V8.md` (+ v9 trecho)
- `vps-brain-dump/SOVYX-DEVELOPMENT-STRATEGY.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/missions/SOVYX-V05-MASTER-MISSION.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/missions/SOVYX-PRODUCTION-ROADMAP.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/plans/SOVYX-BKD-PLN-001-LAUNCH-PLAN.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/plans/SOVYX-BKD-PLN-002-ROADMAP.md`

Recent daily notes:

- `vps-brain-dump/memory/daily/2026-04-13.md`
- `vps-brain-dump/memory/nodes/aiosqlite-deadlock-protocol.md`

Análise e gap:

- `docs/_meta/gap-analysis.md` (sumário por módulo, top 10 gaps, top 5 divergências)

Cross-refs internas:

- `docs/planning/roadmap.md` §v0.5 / v0.6 / v1.0 (detalhes técnicos)
- `docs/planning/gtm-strategy.md` §4 (Launch Week "Genesis")
- `docs/research/competitive-analysis.md` §5 (failure patterns a evitar)

---

_Última revisão: 2026-04-14._
