# Módulo: llm

## Objetivo

`sovyx.llm` abstrai a chamada a provedores de LLM (Anthropic, OpenAI,
Google, Ollama local) atrás de um router único com failover
cross-provider, circuit breaker, cost tracking e roteamento por
complexidade. O router é consumido exclusivamente pela `ThinkPhase` e
por tools do plugin system — é o único ponto onde o Sovyx faz saída
LLM, o que permite orçamento, observability e fallbacks em um lugar só.

## Responsabilidades

- Classificar complexidade da request (`SIMPLE` / `MODERATE` / `COMPLEX`)
  a partir de sinais (tamanho, turns, has_code, has_tool_use, explicit_model).
- Selecionar modelo apropriado para a complexidade entre modelos
  disponíveis (tier fast vs flagship).
- Distribuir chamadas entre providers com failover ordenado (Anthropic
  → OpenAI → Google → Ollama) e cross-provider fallback para modelos
  equivalentes.
- Controlar budget diário + per-conversation via `CostGuard`
  (`can_afford` + `record`).
- Proteger provedores via `CircuitBreaker` per-provider
  (threshold=3 falhas, reset=60s).
- Contar custos e tokens, atualizar counters do dashboard, emitir evento
  `ThinkCompleted` no `EventBus`.
- Expor `get_context_window(model)` para o `ContextAssembler` respeitar
  a janela real do modelo escolhido.
- Converter `ToolDefinition`s do plugin SDK em dicts genéricos
  aceitos por todos os providers.

## Arquitetura

`LLMRouter.generate()` flow:

1. Se `model is None`: `extract_signals(messages)` → `classify_complexity`
   → escolhe modelo disponível via `select_model_for_complexity`.
2. Cost estimation: `input_chars//4` ≈ tokens; multiplica por pricing
   tabela. `CostGuard.can_afford` gate.
3. Build fallback chain: modelo requisitado + equivalentes
   (`_get_equivalent_models`).
4. Loop: para cada modelo tentativo, para cada provider:
   - Skip se provider não suporta modelo.
   - Skip se circuit open.
   - Call `provider.generate()` dentro de span OTel + metric de latência.
   - Sucesso: `circuit.record_success()`, `cost_guard.record()`,
     dashboard counters, emit `ThinkCompleted`, return.
   - Falha: `circuit.record_failure()`, append erro, continua.
5. Se nenhum provider responde: `ProviderUnavailableError` com erros
   concatenados.

Complexity thresholds: `SIMPLE_MAX_LENGTH=500`, `SIMPLE_MAX_TURNS=3`,
`COMPLEX_MIN_LENGTH=2000`, `COMPLEX_MIN_TURNS=8`.

Tiers:
- `_SIMPLE_MODELS = {gemini-2.0-flash, claude-3-5-haiku, gpt-4o-mini}`
- `_COMPLEX_MODELS = {claude-sonnet-4, gemini-2.5-pro, gpt-4o}`

Equivalência cross-provider (flagship ↔ fast ↔ reasoning) em
`_get_equivalent_models`.

## Código real

```python
# src/sovyx/llm/router.py:37-42 — enum StrEnum (xdist-safe)
class ComplexityLevel(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
```

```python
# src/sovyx/llm/router.py:45-62 — sinais de complexidade
@dataclasses.dataclass(frozen=True, slots=True)
class ComplexitySignals:
    message_length: int = 0
    turn_count: int = 0
    has_tool_use: bool = False
    has_code: bool = False
    explicit_model: bool = False
```

```python
# src/sovyx/llm/router.py:84-125 — heurística de classificação
def classify_complexity(signals: ComplexitySignals) -> ComplexityLevel:
    if signals.explicit_model:
        return ComplexityLevel.MODERATE
    if signals.has_tool_use or signals.has_code:
        return ComplexityLevel.COMPLEX
    score = 0.0
    if signals.message_length <= _SIMPLE_MAX_LENGTH: score -= 1.0
    elif signals.message_length >= _COMPLEX_MIN_LENGTH: score += 1.0
    if signals.turn_count <= _SIMPLE_MAX_TURNS: score -= 0.5
    elif signals.turn_count >= _COMPLEX_MIN_TURNS: score += 1.0
    if score <= -1.0: return ComplexityLevel.SIMPLE
    if score >= 1.0: return ComplexityLevel.COMPLEX
    return ComplexityLevel.MODERATE
```

```python
# src/sovyx/llm/router.py:442-465 — equivalências cross-provider
_equivalence: dict[str, list[str]] = {
    # Flagship tier
    "claude-sonnet-4-20250514": ["gpt-4o", "gemini-2.5-pro-preview-03-25"],
    "gpt-4o": ["claude-sonnet-4-20250514", "gemini-2.5-pro-preview-03-25"],
    "gemini-2.5-pro-preview-03-25": ["claude-sonnet-4-20250514", "gpt-4o"],
    # Fast tier
    "claude-3-5-haiku-20241022": ["gpt-4o-mini", "gemini-2.0-flash"],
    "gpt-4o-mini": ["claude-3-5-haiku-20241022", "gemini-2.0-flash"],
    "gemini-2.0-flash": ["gpt-4o-mini", "claude-3-5-haiku-20241022"],
    # Reasoning tier
    "claude-opus-4-20250514": ["o1"],
    "o1": ["claude-opus-4-20250514"],
}
```

```python
# src/sovyx/llm/router.py:493-510 — pricing por 1M tokens (USD)
pricing: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (1.0, 5.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o1": (15.0, 60.0),
    "o3-mini": (1.1, 4.4),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
}
```

## Specs-fonte

- `SOVYX-BKD-SPE-007-LLM-ROUTER.md` (1062 linhas) — complexity-based
  routing, failover chain, circuit breaker, cost tracking, BYOK.
- `SOVYX-BKD-VR-085-CLOUD-LLM-PROXY.md` — cloud proxy, multi-model
  tiering, cost optimization.

## Status de implementação

### ✅ Implementado

- **ComplexityLevel** (StrEnum, xdist-safe por Anti-pattern #9),
  **ComplexitySignals**, **classify_complexity**, **extract_signals**,
  **select_model_for_complexity** — SPE-007 §5.
- **LLMRouter** (`router.py`, ~520 LOC): `generate()`,
  `get_context_window()`, `tool_definitions_to_dicts()`,
  `_get_equivalent_models()`, `_get_provider_models()`, `_get_pricing()`,
  `stop()`.
- **Providers** (`providers/`): `AnthropicProvider`, `OpenAIProvider`,
  `GoogleProvider`, `OllamaProvider`, `_shared.py` com base/util comum.
  Seguem o `Protocol` `LLMProvider` em `engine/protocols.py`.
- **CircuitBreaker** (`circuit.py`): per-provider state machine com
  `failure_threshold`, `recovery_timeout_s`, `can_call()`,
  `record_success()`, `record_failure()`.
- **CostGuard** (`cost.py`): daily budget + per-conversation budget,
  `can_afford()`, `record()`, `get_remaining_budget()`.
- **LLMResponse** (`models.py`): content, model, tokens_in/out,
  latency_ms, cost_usd, finish_reason, provider.
- **Eventos**: `ThinkCompleted` emitido em todo sucesso com
  model/tokens/cost/latency.
- **Metrics**: `llm_calls`, `tokens_used` (direção in/out),
  `llm_cost`, `llm_response_latency` — todos com labels
  `provider`/`model`.
- **Tracing**: `tracer.start_llm_span(provider, model)` com atributos
  `sovyx.llm.tokens_in/out`, `sovyx.llm.cost_usd`.
- **Tool calling**: `tools` argument aceito por `generate()`, convertido
  via `tool_definitions_to_dicts()`.

### ❌ [NOT IMPLEMENTED]

- **Streaming response para speculative TTS**: SPE-007 menciona stream
  integration para permitir a `VoicePipeline` começar TTS antes do LLM
  terminar. `LLMRouter.generate()` é todo-ou-nada; providers têm
  `generate()` async mas não `stream()` no shape esperado. Ponto-chave
  para reduzir latência percebida em voice.
- **BYOK token isolation per user API key**: spec prevê multi-tenancy
  onde cada usuário pode trazer sua própria API key e o cost/rate-limit
  é isolado. Hoje o router usa um `CostGuard` compartilhado e tokens
  vêm de env vars globais (`ANTHROPIC_API_KEY`, etc.). Sem isolamento
  por usuário.

### ⚠️ Parcial

- Integração `stream()` vs `complete()` com `CogLoop` não está clara —
  pipeline hoje espera resposta completa antes de `ActPhase`.

## Divergências [DIVERGENCE]

- Nenhuma divergência contratual contra SPE-007 além dos gaps acima.

## Dependências

- **Externas**: `httpx` — todos os 4 providers (Anthropic, OpenAI, Google,
  Ollama) implementados sobre HTTP direto, sem SDKs nativos. `tiktoken`
  (tokenização). `pydantic` (schemas de tools). Dependências de SDK
  (`anthropic`, `openai`, `google-generativeai`) **não** são usadas.
- **Internas**: `sovyx.engine.errors.{CostLimitExceededError,
  ProviderUnavailableError}`, `sovyx.engine.events.{EventBus, ThinkCompleted}`,
  `sovyx.engine.protocols.LLMProvider`, `sovyx.observability.{logging,
  metrics, tracing}`, `sovyx.dashboard.status.get_counters`.

## Testes

- `tests/unit/llm/test_router.py` — failover, circuit breaker,
  cross-provider fallback, pricing estimation, cost enforcement.
- `tests/unit/llm/test_complexity.py` — todos os paths de
  `classify_complexity` (short/long, tool_use, has_code, explicit_model).
- `tests/unit/llm/providers/` — uma suite por provider com httpx mocks.
- `tests/integration/test_llm_costguard.py` — budget enforcement
  cross-conversation.
- Anti-pattern #9: `ComplexityLevel` é `StrEnum` para sobreviver a
  pytest-xdist namespace duplication.

## Public API reference

### Public API

| Classe | Descrição |
|---|---|
| `LLMRouter` | Roteador de chamadas LLM cross-provider com failover, circuit breaker e cost tracking. |
| `CircuitBreaker` | State machine per-provider (threshold=3, reset=60s) — `can_call/record_success/record_failure`. |
| `CostGuard` | Controle de spending LLM com budget diário + per-conversation; `can_afford`/`record`. |
| `AnthropicProvider` | Provider Anthropic Claude via httpx (sem SDK). |
| `OpenAIProvider` | Provider OpenAI GPT via httpx (sem SDK). |
| `GoogleProvider` | Provider Google Gemini via httpx (sem SDK). |
| `OllamaProvider` | Provider Ollama local via httpx. |
| `LLMResponse` | Response unificado entre providers (content, model, tokens, latency, cost, finish_reason). |
| `ToolCall` | Tool call emitido pelo LLM (padrão ReAct, SPE-003 §4) — definido em `llm/models.py`. |
| `ToolResult` | Resultado de execução de tool — definido em `llm/models.py`. |
| `CostBreakdown` | Breakdown de custo para um período dado. |

### Configuration

| Config | Campo/Finalidade |
|---|---|
| `ComplexityLevel` | Enum (StrEnum xdist-safe) — SIMPLE / MODERATE / COMPLEX para roteamento de modelo. |
| `ComplexitySignals` | Sinais usados para estimar complexidade (length, turns, has_code, has_tool_use, explicit_model). |

## Referências

- Code: `src/sovyx/llm/router.py`, `src/sovyx/llm/circuit.py`,
  `src/sovyx/llm/cost.py`, `src/sovyx/llm/models.py`,
  `src/sovyx/llm/providers/anthropic.py`, `src/sovyx/llm/providers/openai.py`,
  `src/sovyx/llm/providers/google.py`, `src/sovyx/llm/providers/ollama.py`,
  `src/sovyx/llm/providers/_shared.py`.
- Specs: `SOVYX-BKD-SPE-007-LLM-ROUTER.md`, `SOVYX-BKD-VR-085-CLOUD-LLM-PROXY.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-B-services.md` §llm.
