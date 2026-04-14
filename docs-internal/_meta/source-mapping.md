# Source Mapping — 853 docs originais → 34 docs consolidados

**Gerado em**: 2026-04-14
**Propósito**: rastreabilidade entre docs originais em `vps-brain-dump/` (dump do VPS 216.238.111.224:/root/.openclaw/workspace/) e os 34 docs consolidados em `docs/`.

## Estatísticas

| Métrica | Valor |
|---|---|
| Docs originais processados | 853 |
| Docs originais **Sovyx-relevantes** | 430 + 39 MIXED = 469 |
| Docs originais IRRELEVANT (Erebus/Openclaw/pentests/política) | 384 |
| Docs consolidados gerados | 34 (+ 3 em `_meta/`) |
| Linhas totais geradas | 9266 (docs/) + 1930 (_meta/) |
| Redução | ~90% (doc bruto → doc consolidado) |

## Mapeamento por doc consolidado

### `docs/architecture/` (5 arquivos, 1314 linhas)

| Doc consolidado | Docs originais principais |
|---|---|
| `overview.md` | `SOVYX-BKD-SPE-001-ENGINE-CORE.md`; `ADR-004-DATABASE-STACK.md`; `ADR-007-EVENT-ARCHITECTURE.md`; `ADR-008-LOCAL-FIRST.md`; `sovyx-60-seconds.md`; `sovyx-engine-core.md`; `sovyx-bible-index.md` |
| `cognitive-loop.md` | `SOVYX-BKD-SPE-003-COGNITIVE-LOOP.md` (1408 linhas); `SOVYX-BKD-IMPL-006-COGNITIVE-LOOP.md`; `int-001-cogloop-gate.md`; `int-005-cascading-degradation.md`; `sovyx-emotional-proactive.md` |
| `brain-graph.md` | `SOVYX-BKD-SPE-004-BRAIN-MEMORY.md`; `SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md`; `ADR-001-EMOTIONAL-MODEL.md`; `int-003-brain-concurrency.md`; `sovyx-dynamic-importance-mission.md` |
| `llm-router.md` | `SOVYX-BKD-SPE-007-LLM-ROUTER.md` (1062 linhas); `SOVYX-VR-085-CLOUD-LLM-PROXY.md`; `sovyx-cloud-platform.md` |
| `data-flow.md` | `SOVYX-BKD-SPE-001`; `ADR-007-EVENT-ARCHITECTURE`; `SOVYX-BKD-IMPL-004-VOICE-ONNX`; `SOVYX-BKD-IMPL-SUP-003-WYOMING-PROTOCOL`; 8 `sovyx-imm-f0X` |

### `docs/modules/` (15 arquivos, 2899 linhas)

| Doc | Docs originais principais | Gaps surfacados |
|---|---|---|
| `engine.md` | `SPE-001`, `ADR-007`, `ADR-008` | 0 gaps; extras: HealthChecker, DegradationManager, DaemonRPCServer |
| `cognitive.md` | `SPE-003` (1408l), `IMPL-006` | [NOT IMPLEMENTED] DREAM; CONSOLIDATE orphaned |
| `brain.md` | `SPE-004`, `IMPL-002`, `ADR-001` | [DIVERGENCE] 2D emotional vs ADR-001 PAD 3D |
| `context.md` | `SPE-006-CONTEXT-ASSEMBLY`, `IMPL-003` | 0 gaps |
| `mind.md` | `SPE-002-MIND-DEFINITION`, `ADR-001` | [NOT IMPLEMENTED] emotional baseline config |
| `llm.md` | `SPE-007` (1062l), `VR-085` | [NOT IMPLEMENTED] streaming speculative TTS, BYOK isolation |
| `voice.md` | `IMPL-004`, `IMPL-005`, `IMPL-SUP-002/003/004`, `SPE-010` | [NOT IMPLEMENTED] Speaker Rec, Voice Cloning, Parakeet TDT |
| `persistence.md` | `ADR-004-DATABASE-STACK`, `SPE-005` | ⚠️ vector queries invisíveis; Redis caching não impl |
| `observability.md` | `IMPL-015-OBSERVABILITY`, `SPE-026` | ⚠️ gen_ai semantic conventions parcial |
| `plugins.md` | `IMPL-012-PLUGIN-SANDBOX`, `SPE-008` (12 variantes) | [NOT IMPLEMENTED] v2 sandbox (seccomp/namespaces/Seatbelt) |
| `bridge.md` | `IMPL-007-RELAY`, `IMPL-008-HA`, `IMPL-009-CALDAV`, `SPE-014` | [NOT IMPLEMENTED] RelayClient, HomeAssistant, CalDAV |
| `cloud.md` | `IMPL-011-STRIPE`, `IMPL-SUP-006-PRICING`, `SPE-033` | [PARTIAL] Stripe Connect; [NOT IMPLEMENTED] pricing experiments |
| `upgrade.md` | `SPE-028`, `IMPL-SUP-015-IMPORTS` | [NOT IMPLEMENTED] ChatGPT/Claude/Gemini/Obsidian importers |
| `cli.md` | `SPE-015-CLI-TOOLS` | [NOT IMPLEMENTED] REPL, admin utils |
| `dashboard.md` | 8 `sovyx-imm-f0X`, missions `sovyx-dashboard-*` | 0 critical (i18n fix confirmado presente no código atual) |

### `docs/security/` (3 arquivos, 1550 linhas)

| Doc | Docs originais principais |
|---|---|
| `obsidian-protocol.md` (739l) | `obsidian-stack-decisions.md`; `sovyx-coding-protocol.md`; `IMPL-001-CRYPTO`; `IMPL-012-PLUGIN-SANDBOX`; `IMPL-013-SSO-SECURITY`; `IMPL-SUP-009-COMPLIANCE`; `IMPL-SUP-010-SECURITY-TOOLCHAIN`; `IMPL-SUP-011-DIFFERENTIAL-PRIVACY`; `IMPL-SUP-007-ANTIABUSE-CRASH`; 14 `cognitive/safety_*` |
| `threat-model.md` | `IMPL-012-PLUGIN-SANDBOX`; `sovyx-security-identity.md`; `sovyx-privacy-compliance.md` |
| `best-practices.md` | `CLAUDE.md` anti-patterns; `obsidian-stack-decisions.md`; `sovyx-coding-protocol.md`; `IMPL-013-SSO`; `sovyx-privacy-compliance.md` |

### `docs/research/` (4 arquivos, 1015 linhas)

| Doc | Docs originais principais |
|---|---|
| `llm-landscape.md` | `SOVYX-VR-002-OLLAMA-CASE-STUDY`; `SPE-007-LLM-ROUTER`; `PRD-002-PRICING-MONETIZATION`; `sovyx-cloud-platform.md` |
| `embedding-strategies.md` | `SPE-004`, `IMPL-002`, `ADR-004`, `MISSION-AUDIT-V8/V9`, `INT-003`, `INT-005` |
| `memory-systems.md` | `ADR-001-EMOTIONAL-MODEL`; papers (Ebbinghaus 1885, Collins & Loftus 1975, Hebb 1949, Tulving 1972, Russell 1980, Mehrabian 1996, Baddeley 1974/2000, Anderson ACT-R) |
| `competitive-analysis.md` | `VR-001` OpenClaw, `VR-002` Ollama, `VR-004` Supabase, `VR-010` Cross-synthesis, `VR-011` Mycroft post-mortem, `VR-012..016` Failure synthesis, `VR-054..057` Big Tech, `competitive-landscape.md` |

### `docs/planning/` (3 arquivos, 840 linhas)

| Doc | Docs originais principais |
|---|---|
| `roadmap.md` | `gap-analysis.md` roadmap section; `SOVYX-V05-MASTER-MISSION`; `SOVYX-PRODUCTION-ROADMAP`; `PLN-002-ROADMAP`; `SOVYX-MISSION-AUDIT-V8` |
| `gtm-strategy.md` | `VR-065-GTM-STRATEGY`; `VR-082-COMMUNITY-BUILDING`; `VR-017..031` canais; `VR-048..053` distribution; `VR-054..057` counter-positioning; `PRD-002-PRICING`; `sovyx-60-seconds.md` |
| `milestones.md` | `MISSION-AUDIT-V8`; `SOVYX-V05-MASTER-MISSION`; `SOVYX-PRODUCTION-ROADMAP`; `memory/daily/2026-04-13.md`; `aiosqlite-deadlock-protocol.md` |

### `docs/development/` (4 arquivos, 1648 linhas)

| Doc | Docs originais principais |
|---|---|
| `contributing.md` | `CLAUDE.md`; existing `CONTRIBUTING.md` (stub); `pyproject.toml` |
| `testing.md` | `CLAUDE.md` testing patterns; `tests/` layout real; `aiosqlite-deadlock-protocol.md`; `obsidian-stack-decisions.md` |
| `ci-pipeline.md` | `.github/workflows/*.yml`; `CLAUDE.md` quality gates; `memory/daily/2026-04-13.md`; `aiosqlite-deadlock-protocol.md` |
| `anti-patterns.md` | `CLAUDE.md` (12 anti-patterns); `int-001..005`; `obsidian-stack-decisions.md` |

---

## Descarte (docs originais NÃO mapeados)

**384 docs IRRELEVANT** não foram mapeados pra nenhuma doc consolidada, mas estão registrados em `triage-index.md` com contexto de descarte:

| Origem | Contagem | Motivo de descarte |
|---|---:|---|
| Erebus (CODEX-*, edge-hunter-*, dark-intel-*, divergence-matrix-*, arb-engine-*) | ~300 | Projeto de trading/weather/arbitragem, não é Sovyx |
| Openclaw (AGENTS/SOUL/IDENTITY/USER/TOOLS/MEMORY/HEARTBEAT/BRAIN/PRE-FLIGHT/README + cortex/ops) | ~25 | Sistema do agente operando o VPS; meta-infra, não é Sovyx |
| Pentests (AVOZDOSPRACAS, FALADF, REDAT, VULN-CRITICA, EXPLOIT-POC) | 4 | Pentests de sites externos |
| Política (grafo-neural-politicos, DIMITRI-PROTOCOL) | 2 | Projetos paralelos |
| Archive Erebus (memory/archive/erebus-immersion-v1 + nodes antigos) | 41 | Memória antiga Erebus |
| MIXED com predominância Erebus | ~12 | Diários com seções Erebus+Sovyx (ex: 2026-03-21/22/27) — Sovyx extraído, Erebus descartado |

## Descarte (docs Sovyx não incorporados)

Dos 469 Sovyx+MIXED, ~15-20% entraram só implicitamente (citados em algum doc mas sem seção dedicada). Exemplos:

- **Missions detalhadas de specific polish**: muitas missões operacionais (ex: "sovyx-imm-d1-solutions.md", "sovyx-v01-honest-gaps.md") foram consolidadas em `development/ci-pipeline.md` via timeline histórica, não como docs individuais
- **Diários operacionais** (`memory/daily/*`): 40 diários; apenas os mais relevantes citados (2026-04-13, 2026-03-21, 2026-03-24). O resto é ruído operacional
- **Auditorias V1-V7**: só MISSION-AUDIT-V8 (última) foi usada como âncora em `planning/milestones.md`
- **sovyx-imm-f01..f08** (immersions de libs frontend): consolidadas em `modules/dashboard.md` como tabela F01-F08

## Garantias

- **100% dos docs Sovyx de alta relevância** (287 `alta` na triage) estão mapeados diretamente
- **100% dos ADRs e SPE-* principais** estão como doc-fonte em pelo menos 1 doc consolidado
- **100% dos gaps identificados em Fase 3** estão marcados `[NOT IMPLEMENTED]` ou `[DIVERGENCE]` nos docs consolidados
- Rastreabilidade reversa: cada doc consolidado tem seção `## Referências` com lista de docs originais
- Código real citado: cada doc módulo tem pelo menos 3 exemplos de código do `src/` com caminho real

## Próximos passos (pós-revisão)

1. Deletar `vps-brain-dump/` (cópia temporária — agora aguardando autorização do usuário)
2. Revisar/ajustar docs conforme feedback
3. (Opcional) Criar `docs/index.md` como entry-point de navegação
4. Commit quando autorizado

## Arquivos de suporte

- `docs/_meta/triage-index.md` (973 linhas) — índice completo classificado
- `docs/_meta/gap-analysis.md` (258 linhas) — consolidado de gaps
- `docs/_meta/gap-inputs/analysis-{A,B,C,D}-*.md` (699 linhas) — análises por grupo
- `docs/_meta/batches/` — outputs brutos da triagem + scripts Python reprodutíveis
