# Módulo: cognitive

## Objetivo

O pacote `sovyx.cognitive` implementa o loop cognitivo que transforma uma
perception (mensagem recebida) em uma response executada e refletida em
memória. A SPE-003 prevê 7 fases contínuas (Perceive → Attend → Think → Act
→ Reflect → Consolidate → Dream); o código roda as **5 primeiras fases de
forma síncrona por request**, e mantém Consolidate como job de background
desacoplado em `brain/consolidation.py`. Além do loop, este pacote
concentra a stack completa de segurança (serialization gate, injection
tracker, PII guard, financial gate, output guard, shadow mode,
classifier e escalation).

## Responsabilidades

- Enforcar a sequência OODA (PERCEIVE → ATTEND → THINK → ACT → REFLECT)
  via `CognitiveStateMachine` com transições validadas.
- Serializar requests concorrentes de múltiplos canais via `CogLoopGate`
  (PriorityQueue + single worker + Future-per-request).
- Executar cada fase como classe independente com responsabilidade única
  (`PerceivePhase`, `AttendPhase`, `ThinkPhase`, `ActPhase`, `ReflectPhase`).
- Chamar o LLM via router com roteamento por complexidade (fast_model
  vs default_model) e assemble de contexto.
- Atualizar memória via `BrainService` na Reflect phase.
- Aplicar guardrails de segurança em input (PII, injection) e em output
  (output guard, financial gate, classifier multi-tier).
- Emitir métricas, spans OTel e eventos `ThinkCompleted` / `ResponseSent`
  no `EventBus`.

## Arquitetura

`CognitiveLoop.process_request()` **nunca lança exceção** — o contrato é
retornar sempre um `ActionResult`, com state machine resetado para IDLE
no `finally`. Erros conhecidos (`CostLimitExceededError`,
`ProviderUnavailableError`) viram mensagens user-facing via
`_categorize_error()`; erros desconhecidos caem em "unexpected error"
sem vazar internals.

`CogLoopGate` é o único ponto de entrada vindo do bridge: cria
`CognitiveRequest` com tudo que o loop precisa (perception, mind_id,
conversation_id, history, person_name), publica em PriorityQueue
(maxsize=10), worker único drena sequencialmente. Backpressure: quando
fila cheia, `submit()` levanta `CognitiveError`.

A stack de safety é composta e independente do loop: cada guard é
chamado nas fases relevantes. `CogLoopGate` também vincula contexto
estruturado de logging (`mind_id`, `conversation_id`) antes de cada
processamento, para que todos os logs emitidos durante o loop carreguem
correlação.

## Código real

```python
# src/sovyx/cognitive/loop.py:92-109 — contrato do processamento
async def process_request(self, request: CognitiveRequest) -> ActionResult:
    """Process a CognitiveRequest through the full loop.
    NEVER raises an exception — always returns ActionResult.
    State machine always resets to IDLE via finally block.
    """
    with (
        tracer.start_span("cognitive.loop", ...),
        metrics.measure_latency(metrics.cognitive_loop_latency),
    ):
        return await self._execute_loop(request, tracer, metrics)
```

```python
# src/sovyx/cognitive/state.py:15-22 — transições válidas (OODA)
VALID_TRANSITIONS: dict[CognitivePhase, set[CognitivePhase]] = {
    CognitivePhase.IDLE: {CognitivePhase.PERCEIVING},
    CognitivePhase.PERCEIVING: {CognitivePhase.ATTENDING, CognitivePhase.IDLE},
    CognitivePhase.ATTENDING: {CognitivePhase.THINKING, CognitivePhase.IDLE},
    CognitivePhase.THINKING: {CognitivePhase.ACTING},
    CognitivePhase.ACTING: {CognitivePhase.REFLECTING},
    CognitivePhase.REFLECTING: {CognitivePhase.IDLE},
}
```

```python
# src/sovyx/cognitive/perceive.py:112-126 — complexidade sem LLM
@staticmethod
def classify_complexity(content: str) -> float:
    """Result determines model routing:
    - complexity < 0.3 → fast_model (haiku)
    - complexity >= 0.3 → default_model (sonnet)
    """
```

```python
# src/sovyx/cognitive/gate.py:59-94 — submit com timeout+backpressure
async def submit(self, request: CognitiveRequest, timeout: float = 30.0) -> ActionResult:
    future = asyncio.get_running_loop().create_future()
    item = (request.perception.priority, next(self._counter), request, future)
    try:
        self._queue.put_nowait(item)
    except asyncio.QueueFull:
        raise CognitiveError("Cognitive loop queue full (backpressure)") from None
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError:
        raise CognitiveError(f"Cognitive loop timed out after {timeout}s") from None
```

## Specs-fonte

- `SOVYX-BKD-SPE-003-COGNITIVE-LOOP.md` (1408 linhas) — 7 fases planejadas,
  OODA Loop (Boyd 1987), ReAct (Yao et al. 2023), Schneier OODA critique.
- `SOVYX-BKD-IMPL-006-COGNITIVE-LOOP.md` — detalhes de implementação,
  error handling, tracing.
- ADRs de safety (dispersas em `vps-brain-dump/.../adrs/`) — filtros
  multi-tier, shadow mode, escalation.

## Status de implementação

### ✅ Implementado

- Fase 1 **PERCEIVE** (`perceive.py`, 155 LOC): `Perception` dataclass;
  validação, normalização, `MAX_INPUT_CHARS = 10_000`; classify_complexity
  por heurística (length, complex markers, multi-question, simple triggers).
- Fase 2 **ATTEND** (`attend.py`, 310 LOC): filtro por prioridade,
  normalização de texto (`text_normalizer.py`), decisão `should_process`.
- Fase 3 **THINK** (`think.py`, 126 LOC): seleção de modelo por
  complexidade → context_window do modelo escolhido → `ContextAssembler` →
  `LLMRouter.generate()` com tools opcionais do PluginManager. Em erro,
  retorna `LLMResponse` degradado.
- Fase 4 **ACT** (`act.py`, 459 LOC): `ActionResult`, execução de tool
  calls via `ToolExecutor`, re-invocação de LLM com resultados de tool.
- Fase 5 **REFLECT** (`reflect.py`, 1021 LOC): extração de conceitos via
  LLM, update em `BrainService`, tagging emocional, decay de working
  memory após reflect.
- `CognitiveStateMachine` (`state.py`, 77 LOC) com transições validadas
  e `reset()` incondicional para IDLE.
- `CogLoopGate` (`gate.py`, 148 LOC) — **extra, não documentado em spec**
  — PriorityQueue(maxsize=10), single worker, Future-per-request,
  binding de `bind_request_context` para logging estruturado.

### ⚠️ Parcial

- **Fase 6 CONSOLIDATE**: `brain/consolidation.py` (`ConsolidationCycle`,
  `ConsolidationScheduler`) existe e implementa Ebbinghaus decay + merge +
  prune. **Não é chamada pelo CognitiveLoop**. A spec (§1.1) pede
  "periodic: prune, strengthen" — semanticamente o agendador o faz fora
  do loop, mas o mapping spec→código é confuso.

### ❌ [NOT IMPLEMENTED]

- **Fase 7 DREAM** — spec: "nightly: discover patterns". Nenhum arquivo
  `cognitive/dream.py`. Nenhum scheduler batch descobrindo padrões em
  episódios. Implementação futura (v1.0+).

### Features de safety (extras da spec cognitive)

14 arquivos implementando defense-in-depth não totalmente mapeados em
SPE-003:

- `gate.py` — serialization
- `injection_tracker.py` (453 LOC) — detecção de prompt injection
- `pii_guard.py` (466 LOC) — PII scrubbing
- `financial_gate.py` (453 LOC) — confirmação em ações financeiras
- `output_guard.py` (303 LOC) — filtro de output
- `safety_patterns.py` (1165 LOC) — patterns library
- `safety_classifier.py` (704 LOC) — classificação multi-tier
- `safety_escalation.py` (201 LOC) — escalation policy
- `shadow_mode.py` (277 LOC) — dry-run de novas rules
- `audit_store.py`, `custom_rules.py`, `safety_audit.py`, `safety_i18n.py`,
  `safety_notifications.py`, `safety_container.py`, `text_normalizer.py`.

## Divergências [DIVERGENCE]

- [DIVERGENCE] Spec SPE-003 lista 7 fases como "contínuas". Código
  executa 5 fases síncronas por turn; fases 6 e 7 seriam ciclos de
  background. Consolidation existe mas não é invocada do loop — ela
  depende de agendador externo (SPE-004 prevê `consolidation_interval_hours`
  na `BrainConfig`). Dream inexistente.
- [DIVERGENCE] Spec não documenta `CogLoopGate`; priorização e
  backpressure foram decisões de implementação (INT-001) não refletidas
  em SPE-003.

## Dependências

- **Externas**: `asyncio`, `structlog`.
- **Internas**: `sovyx.brain.service.BrainService`,
  `sovyx.context.assembler.ContextAssembler`, `sovyx.llm.router.LLMRouter`,
  `sovyx.llm.models.LLMResponse`, `sovyx.mind.config.MindConfig`,
  `sovyx.plugins.manager.PluginManager`, `sovyx.engine.events.EventBus`,
  `sovyx.engine.types`, `sovyx.observability.{logging,metrics,tracing}`.

## Testes

- `tests/unit/cognitive/` — uma suite por fase (`test_perceive`,
  `test_attend`, `test_think`, `test_act`, `test_reflect`).
- `tests/unit/cognitive/test_state.py` — transições válidas/inválidas.
- `tests/unit/cognitive/test_gate.py` — backpressure, timeout, shutdown
  draining.
- `tests/integration/test_cognitive_loop.py` — cadeia completa com mocks
  de LLM/Brain.
- `tests/unit/cognitive/safety_*` — cada guard tem sua suite.
- Anti-pattern #8 (xdist class-identity): loop usa
  `type(exc).__name__` em `_categorize_error` — igualmente aplicado em
  testes.

## Public API reference

### Public API

| Classe | Descrição |
|---|---|
| `CognitiveLoop` | Loop completo Perceive → Attend → Think → Act → Reflect; nunca lança, sempre retorna ActionResult. |
| `CognitiveStateMachine` | State machine das fases OODA com transições validadas e `reset()` incondicional. |
| `CogLoopGate` | Serializa requests concorrentes via PriorityQueue + single worker + Future-per-request. |
| `CognitiveRequest` | Bundle (perception, mind_id, conversation_id, history, person_name) submetido ao loop. |
| `PerceivePhase` | Valida, enriquece e classifica complexidade de uma Perception. |
| `Perception` | Input bruto do cognitive loop. |
| `AttendPhase` | Filtra perceptions por prioridade, safety e normalização. |
| `ThinkPhase` | Assembla contexto e chama LLM com roteamento por complexidade. |
| `ActPhase` | Formata a resposta, executa tool calls via ToolExecutor e prepara entrega. |
| `ActionResult` | Resultado da fase Act, pronto para entrega por canal. |
| `ToolExecutor` | Framework de execução de tools (padrão ReAct, SPE-003 §4). |
| `ReflectPhase` | Pós-resposta: encode episode + extract concepts + Hebbian + working memory decay. |
| `ExtractedConcept` | Concept extraído de input via LLM ou regex. |
| `OutputGuard` | Filtro de segurança pós-LLM sobre a resposta gerada. |
| `PIIGuard` | Detecta e redacta PII em output. |
| `PIIPattern` | Pattern de detecção de PII (regex + metadata). |
| `FinancialGate` | Intercepta tool calls financeiras exigindo confirmação do usuário. |
| `PendingConfirmation` | Ação financeira aguardando confirmação. |
| `InjectionContextTracker` | Rastreia tentativas de prompt injection multi-turn por conversa. |
| `SafetyContainer` | Container DI para componentes do subsistema de safety. |
| `SafetyEscalationTracker` | Rastreia blocks de safety por source e gerencia escalation. |
| `ClassificationCache` | Cache LRU thread-safe para classificações de safety. |
| `SafetyAuditTrail` | Registra e consulta eventos de filtros de safety (sem conteúdo original, privacy). |
| `SafetyNotifier` | Gerencia notificações de alertas de safety com debounce. |
| `LogNotificationSink` | Sink default que registra alertas no structured logger. |
| `AuditStore` | Event store de audit em SQLite. |
| `SafetyPattern` | Pattern compilado de safety com metadata. |

### Configuration

| Config | Campo/Finalidade |
|---|---|
| `InjectionVerdict` | Enum do resultado de análise multi-turn de injection. |
| `SafetyCategory` | Categorias de violação alinhadas com PatternCategory. |
| `PatternCategory` | Categorias de patterns de safety para audit trail. |
| `FilterTier` | Tiers de content filter (cada tier inclui os inferiores). |
| `EscalationLevel` | Estado de escalation por source (session/IP). |
| `FilterDirection` | Direção do conteúdo filtrado (input/output). |
| `FilterAction` | Ação tomada sobre conteúdo filtrado. |
| `ClassificationBudget` | Tracker de gasto com classificação LLM com cap horário. |

## Referências

- Code: `src/sovyx/cognitive/loop.py`, `src/sovyx/cognitive/perceive.py`,
  `src/sovyx/cognitive/attend.py`, `src/sovyx/cognitive/think.py`,
  `src/sovyx/cognitive/act.py`, `src/sovyx/cognitive/reflect.py`,
  `src/sovyx/cognitive/state.py`, `src/sovyx/cognitive/gate.py`,
  `src/sovyx/cognitive/safety_*.py`, `src/sovyx/cognitive/injection_tracker.py`,
  `src/sovyx/cognitive/pii_guard.py`, `src/sovyx/cognitive/financial_gate.py`,
  `src/sovyx/cognitive/output_guard.py`, `src/sovyx/cognitive/shadow_mode.py`,
  `src/sovyx/cognitive/audit_store.py`, `src/sovyx/cognitive/custom_rules.py`,
  `src/sovyx/cognitive/text_normalizer.py`.
- Specs: `SOVYX-BKD-SPE-003-COGNITIVE-LOOP.md`,
  `SOVYX-BKD-IMPL-006-COGNITIVE-LOOP.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-A-core.md` §cognitive.
