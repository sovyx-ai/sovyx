# Module: llm

## What it does

The `sovyx.llm` package is the only place Sovyx talks to language models. It routes requests across four providers, classifies complexity to pick the right model tier, enforces cost budgets, and wraps every provider in a circuit breaker with cross-provider fallback.

## Key classes

| Name | Responsibility |
|---|---|
| `LLMRouter` | Cross-provider routing with failover, circuit breaker, and cost tracking. |
| `CircuitBreaker` | Per-provider state machine (threshold 3, recovery 60 s). |
| `CostGuard` | Daily budget + per-conversation budget. |
| `AnthropicProvider` / `OpenAIProvider` / `GoogleProvider` / `OllamaProvider` | httpx-based providers. |
| `LLMResponse` | Unified response (`content`, `model`, `tokens_in`, `tokens_out`, `latency_ms`, `cost_usd`, `finish_reason`, `provider`). |
| `ComplexityLevel` | `StrEnum` (`SIMPLE`, `MODERATE`, `COMPLEX`). |
| `ComplexitySignals` | Inputs to `classify_complexity`. |

All four providers are implemented on top of `httpx` — no vendor SDKs are required at runtime.

## Complexity tiers

`ThinkPhase` passes the message list to the router. The router extracts signals, classifies complexity, and selects a model from the registered providers.

```python
# src/sovyx/llm/router.py
class ComplexityLevel(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclasses.dataclass(frozen=True, slots=True)
class ComplexitySignals:
    message_length: int = 0
    turn_count: int = 0
    has_tool_use: bool = False
    has_code: bool = False
    explicit_model: bool = False


def classify_complexity(signals: ComplexitySignals) -> ComplexityLevel:
    if signals.explicit_model:
        return ComplexityLevel.MODERATE
    if signals.has_tool_use or signals.has_code:
        return ComplexityLevel.COMPLEX
    score = 0.0
    if signals.message_length <= 500:     score -= 1.0
    elif signals.message_length >= 2000:  score += 1.0
    if signals.turn_count <= 3:           score -= 0.5
    elif signals.turn_count >= 8:         score += 1.0
    if score <= -1.0: return ComplexityLevel.SIMPLE
    if score >= 1.0:  return ComplexityLevel.COMPLEX
    return ComplexityLevel.MODERATE
```

Thresholds: `SIMPLE_MAX_LENGTH=500`, `SIMPLE_MAX_TURNS=3`, `COMPLEX_MIN_LENGTH=2000`, `COMPLEX_MIN_TURNS=8`.

Tiers (used by `select_model_for_complexity`):

- **Simple** — `gemini-2.0-flash`, `claude-3-5-haiku-20241022`, `gpt-4o-mini`.
- **Complex** — `claude-sonnet-4-20250514`, `gemini-2.5-pro-preview-03-25`, `gpt-4o`.

## Routing flow

`LLMRouter.generate()` proceeds as follows:

1. If `model is None`: `extract_signals(messages)` then `classify_complexity` then `select_model_for_complexity`.
2. Estimate cost (`input_chars // 4` tokens against the pricing table); gate with `CostGuard.can_afford`.
3. Build a fallback chain: requested model plus equivalents from `_get_equivalent_models`.
4. For each candidate model, for each provider that supports it:
   - Skip if the provider's circuit is open.
   - Call `provider.generate()` inside an OTel span.
   - On success: `circuit.record_success()`, `cost_guard.record()`, emit `ThinkCompleted`, return.
   - On failure: `circuit.record_failure()`, append the error, try the next candidate.
5. If nothing responds: raise `ProviderUnavailableError` with the concatenated errors.

## Cross-provider equivalence

```python
# src/sovyx/llm/router.py — _get_equivalent_models
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

## Cost tracking

`CostGuard` enforces three budgets:

- **Per-request** — estimated before the call; rejected if it would exceed the daily budget.
- **Per-conversation** — `conversation_id` rolling budget.
- **Daily** — engine-wide daily cap.

Pricing table (USD per 1M tokens, input/output):

```python
# src/sovyx/llm/router.py
pricing: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514":     (3.0,  15.0),
    "claude-3-5-haiku-20241022":    (1.0,   5.0),
    "claude-opus-4-20250514":       (15.0, 75.0),
    "gpt-4o":                       (5.0,  15.0),
    "gpt-4o-mini":                  (0.15,  0.6),
    "o1":                           (15.0, 60.0),
    "o3-mini":                      (1.1,   4.4),
    "gemini-2.0-flash":             (0.10,  0.40),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
}
```

`CostGuard.record()` updates counters in `sovyx.dashboard.status` so the dashboard sees live cost and token usage.

## Circuit breaker

`CircuitBreaker` maintains per-provider state. After three consecutive failures the circuit opens; after 60 seconds it transitions to half-open and the next success closes it.

- `can_call(provider) -> bool`
- `record_success(provider)`
- `record_failure(provider)`

The router calls `can_call()` before each provider attempt and records the outcome.

## BYOK per mind

Each mind configures its own provider credentials in `mind.yaml` (for example `llm.providers.anthropic.api_key`). Providers receive these at construction time, so one mind's key never leaks into another mind's requests. The router and the cost guard are built per mind, so rate and budget limits are scoped the same way.

## Tool calls

`LLMRouter.generate(tools=...)` accepts a list of `ToolDefinition` objects from the plugin SDK. `tool_definitions_to_dicts()` converts them to the generic JSON shape accepted by all four providers. Tool-call results come back as `ToolCall` entries on `LLMResponse`; `ActPhase` executes them and re-invokes the router with the tool outputs appended.

## Observability

- **Metrics** — `llm_calls`, `tokens_used` (direction in/out), `llm_cost`, `llm_response_latency` — each labelled with `provider` and `model`.
- **Tracing** — `tracer.start_llm_span(provider, model)` with attributes `sovyx.llm.tokens_in`, `sovyx.llm.tokens_out`, `sovyx.llm.cost_usd`.
- **Events** — `ThinkCompleted` on every successful call.

## Configuration

```yaml
llm:
  defaults:
    daily_budget_usd: 5.0
    per_conversation_budget_usd: 0.5
    circuit:
      failure_threshold: 3
      recovery_timeout_s: 60
  providers:
    anthropic:
      api_key: ${ANTHROPIC_API_KEY}
      models: [claude-sonnet-4-20250514, claude-3-5-haiku-20241022]
    openai:
      api_key: ${OPENAI_API_KEY}
      models: [gpt-4o, gpt-4o-mini, o1]
    google:
      api_key: ${GEMINI_API_KEY}
      models: [gemini-2.5-pro-preview-03-25, gemini-2.0-flash]
    ollama:
      base_url: http://localhost:11434
      models: [llama3.1:8b]
```

`MindConfig.llm` overrides `LLMDefaultsConfig` fields on a per-mind basis.

## Roadmap

- Streaming `generate()` path to let the voice pipeline start TTS before the full response arrives.
- Per-user token isolation in addition to per-mind BYOK.
- Richer equivalence graph (reasoning tier, long-context tier).

## See also

- `cognitive.md` — `ThinkPhase` is the router's only caller in the loop
- `engine.md` — `ThinkCompleted` on the `EventBus` and DI wiring
- `../architecture.md` — end-to-end flow
