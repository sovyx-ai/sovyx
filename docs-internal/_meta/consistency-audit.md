# Consistency Audit — Docs de Módulo ↔ Código Real

**Gerado em**: 2026-04-14 (ETAPA 2 da auditoria final)
**Escopo**: 16 docs em `docs/modules/` vs `src/sovyx/` (+ `dashboard/src/` pra dashboard.md)
**Método**: 3 agents paralelos, cada um verificando 5-6 docs via Grep/Read/Glob direto no código

**Detalhe completo**: `consistency-audit-A.md`, `consistency-audit-B.md`, `consistency-audit-C.md`

---

## Sumário geral

| Part | Docs | Checks OK | Material | Minor | Status |
|---|---|---:|---:|---:|---|
| A (engine/cognitive/brain/context/mind) | 5 | 159/165 | 4 | 8 | ⚠️ |
| B (llm/voice/persistence/observability/plugins) | 5 | — | 0 | 6 | ⚠️ |
| C (bridge/cloud/upgrade/cli/dashboard/benchmarks) | 6 | 69/90 | 9 | 12 | ❌ |
| **TOTAL** | **16** | **~290/~330** | **13** | **26** | — |

**Taxa de consistência**: ~88% (290/330 checks OK). **13 issues materiais** precisam fix.

---

## ❌ Issues materiais (13)

### `engine.md`
1. **Linha 123**: texto diz "EventBus com 11 event classes" — código tem **13** em `engine/events.py` (linhas 62, 75, 87, 104, 120, 138, 156, 172, 186, 207, 226, 244, 257). A lista enumerada já tem 13 nomes; só o número está errado.

### `brain.md`
2. **Linhas 165-166**: `EvolutionScorer` listado como classe implementada em `scoring.py`. **Não existe**. Só há `EvolutionWeights` (dataclass de pesos, scoring.py:90). Tabela Public API está correta; texto narrativo errado.
3. **Linhas 176-177**: `ContradictionDetector` descrito como classe de 233 LOC em `contradiction.py`. Arquivo tem 233 LOC mas **não tem essa classe** — só `class ContentRelation(StrEnum)` + funções de módulo (`detect_contradiction`, `_detect_via_llm`).
4. **Linha 177**: `ConceptContradicted` atribuído a `contradiction.py`. **Evento emitido em** `brain/service.py`.

### `observability.md`
5. **Hidden gap não registrado**: código usa `SimpleSpanProcessor` em `tracing.py:39`, mas IMPL-015 spec manda `BatchSpanProcessor`. Divergência **não** marcada na doc. Deveria virar `[DIVERGENCE]`.
6. **Classe errada na prosa**: "**HealthChecker** descobre automaticamente 10 implementações" — classe real é **`HealthRegistry`** (observability/health.py:85). `HealthChecker` é outra coisa em `engine/health.py`.

### `cloud.md`
7. **Claim falso sobre webhook**: doc diz "webhook processes 6 events". `WebhookHandler` (billing.py:341) é **dispatch registry genérico**; zero events hardcoded. A afirmação é factualmente falsa.

### `upgrade.md`
8. **`BackupTrigger` valores errados**: doc lista `manual/pre_upgrade/scheduled`. Código tem `migration/daily/manual` (backup_manager.py:50).

### `cli.md`
9. **Subcomandos inexistentes**: `sovyx dashboard start/stop` e `sovyx logs tail/search` **não existem**. `dashboard_app` e `logs_app` são single callbacks com flags, não subapps.
10. **`sovyx brain analyze` não é runnable**: requer subcomando `scores` (doc não menciona).
11. **`sovyx doctor` mal-atribuído**: doc diz que usa `upgrade/doctor.py`; código real usa `observability.health`.

### `dashboard.md`
12. **Todas as contagens erradas**:
    - REST endpoints: doc diz **25**, real = **32 decorators** (~28-29 paths únicos)
    - WS events: doc diz **15**, real = **12** (11 subscribed em `DashboardEventBridge._subscribed` + `PluginStateChanged`)
    - Pages: doc diz **14**, real = **12** (`ComingSoon` é component, não page)
    - Zustand slices: doc diz **11**, real = **12**
13. **Nota sobre i18n import**: doc está correto (import presente em main.tsx:3). **gap-analysis.md é que está stale** sobre este ponto — precisa correção em `gap-analysis.md` também.

---

## ⚠️ Issues minor (26)

### `llm.md`
- ISSUE-LLM-1: seção Dependências lista `anthropic`/`openai`/`google-generativeai` como deps, mas **nenhum provider importa SDKs nativos** — todos usam `httpx` puro (contradiz o próprio Public API da doc)
- ISSUE-LLM-2 (editorial): clarificar que `ToolCall`/`ToolResult` vivem em `llm/models.py`

### `voice.md`
- ISSUE-VOICE-1: inconsistência interna — "Moonshine v1.0" na Arquitetura vs "Moonshine v2 via moonshine-voice" no Public API. Confirmar versão real.

### `plugins.md`
- ISSUE-PLUG-EDIT: frase "13 permissões ... o doc cita 18 tipos" mistura permissions (13) com escape vectors do IMPL-012 (18). Refrasear.

### `bridge.md`
- Public API lista classes que **não estão em** `__init__.py` `__all__` (só `InlineButton`/`InboundMessage`/`OutboundMessage` são re-exportados). Ajustar tabela ou `__all__`.

### `cli.md`
- `sovyx plugin info` omitido da tabela de comandos
- REPL/admin confirmed absent (doc já marca `[NOT IMPLEMENTED]`, ok)
- outros 2 minor editoriais

### `upgrade.md`
- `upgrade/migrations/` diretório é empty scaffold (só `__init__.py`) — doc não menciona
- outro minor

### `benchmarks.md`
- `TierLimits`/`BudgetCheck`/`MetricComparison` não estão em `__init__.py` `__all__` apesar de documentadas como Public API
- Referências lista só 2 dos 5 `bench_*.py` scripts em `benchmarks/` root

### Triviais (Part A — 8 issues)
- Off-by-1 em labels de linhas de blocos de código em engine.md/brain.md/context.md/mind.md (puramente cosmético, doc cita "linha N" mas é N±1)

---

## ✅ Alinhamentos corretos (destaque)

- `[NOT IMPLEMENTED]` DREAM (cognitive.md) — `grep dream*` em cognitive/ retorna zero
- `[PARTIAL]` CONSOLIDATE orphaned — `grep consolidation` em cognitive/ retorna zero (código existe em brain/)
- `[DIVERGENCE]` Emotional 2D vs 3D ADR-001 — brain.md e mind.md marcados; grep confirma estrutura 2D
- 15 campos do Concept conferem
- LOCs: brain=4829, context=759, mind=802, benchmarks=483 — todos batem com `wc -l`
- 10 health checks, 8 safety enums, 14 safety files, 13 permissions, 19 plugin files, 4 LLM providers, 9 pragmas — TODOS verificados
- Speaker Recognition / Voice Cloning / Parakeet TDT — zero arquivos, `[NOT IMPLEMENTED]` confirmado
- Relay Client / Home Assistant / CalDAV — zero arquivos em `bridge/`, `[NOT IMPLEMENTED]` confirmado
- Stripe Express/destination/refund/dispute/payout/Tax — zero, `[NOT IMPLEMENTED]` confirmado
- Van Westendorp / Gabor-Granger / PQLScorer / FunnelTracker — zero, `[NOT IMPLEMENTED]` confirmado
- ChatGPT/Claude/Gemini/Obsidian importers — zero, `[NOT IMPLEMENTED]` confirmado
- REPL/admin CLI — zero, `[NOT IMPLEMENTED]` confirmado
- Sandbox v2 (seccomp/namespaces/Seatbelt) — zero código, `[NOT IMPLEMENTED]` confirmado
- i18n import em `dashboard/src/main.tsx:3` — **presente** (doc correto; `gap-analysis.md` stale)

---

## Ação recomendada

Listar 13 issues materiais em ETAPA 3 pra fix, junto com os 26 minor.

Notável: há **1 novo DIVERGENCE não-registrado** descoberto nesta auditoria:
- `observability/tracing.py` usa `SimpleSpanProcessor`; IMPL-015 manda `BatchSpanProcessor`. Adicionar a `gap-analysis.md` top divergências (vira 5 total em vez de 4).

Também: `gap-analysis.md` tem 1 item stale:
- "i18n import faltando em main.tsx" — na verdade presente. Remover do gap-analysis.
