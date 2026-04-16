# Module: llm

## What it does

The `sovyx.llm` package is the only place Sovyx talks to language models. It routes requests across ten providers, classifies complexity to pick the right model tier, enforces cost budgets, wraps every provider in a circuit breaker with cross-provider fallback, and supports token-level streaming for real-time voice TTS.

## Key classes

| Name | Responsibility |
|---|---|
| `LLMRouter` | Cross-provider routing with failover, circuit breaker, and cost tracking. |
| `CircuitBreaker` | Per-provider state machine (threshold 3, recovery 60 s). |
| `CostGuard` | Daily budget + per-conversation budget. |
| `AnthropicProvider` / `GoogleProvider` / `OllamaProvider` | httpx-based providers with unique API formats. |
| `OpenAICompatibleProvider` | Base class for OpenAI-wire-format providers (OpenAI, xAI, DeepSeek, Mistral, Together, Groq, Fireworks). |
| `LLMResponse` | Unified response (`content`, `model`, `tokens_in`, `tokens_out`, `latency_ms`, `cost_usd`, `finish_reason`, `provider`). |
| `LLMStreamChunk` / `ToolCallDelta` | Incremental streaming models yielded by `stream()`. |
| `ComplexityLevel` | `StrEnum` (`SIMPLE`, `MODERATE`, `COMPLEX`). |
| `ComplexitySignals` | Inputs to `classify_complexity`. |

All ten providers are implemented on top of `httpx` — no vendor SDKs are required at runtime. The six OpenAI-compatible providers (OpenAI, xAI, DeepSeek, Mistral, Together AI, Groq, Fireworks) share a base class (`OpenAICompatibleProvider`) that handles `generate()` + `stream()` + retry + error handling; each provider file is ~30 LOC of configuration.

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

- **Simple** — `gemini-2.0-flash`, `claude-3-5-haiku-20241022`, `gpt-4o-mini`, `deepseek-chat`, `mistral-small-latest`, `mixtral-8x7b-32768`, `llama-3.1-8b-instant`.
- **Complex** — `claude-sonnet-4-20250514`, `gemini-2.5-pro-preview-03-25`, `gpt-4o`, `grok-3`, `deepseek-reasoner`, `mistral-large-latest`, `llama-3.1-70b-versatile`.

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
    "claude-sonnet-4-20250514": ["gpt-4o", "gemini-2.5-pro-preview-03-25", "grok-3", "mistral-large-latest"],
    "gpt-4o": ["claude-sonnet-4-20250514", "gemini-2.5-pro-preview-03-25", "grok-3", "mistral-large-latest"],
    # ... (symmetric entries for all flagship models)
    # Fast tier
    "claude-3-5-haiku-20241022": ["gpt-4o-mini", "gemini-2.0-flash", "deepseek-chat", "mistral-small-latest"],
    "gpt-4o-mini": ["claude-3-5-haiku-20241022", "gemini-2.0-flash", "deepseek-chat", "mistral-small-latest"],
    # ... (symmetric entries for all fast models)
    # Reasoning tier
    "claude-opus-4-20250514": ["o1", "deepseek-reasoner"],
    "o1": ["claude-opus-4-20250514", "deepseek-reasoner"],
    "deepseek-reasoner": ["claude-opus-4-20250514", "o1"],
}
```

## Cost tracking

`CostGuard` enforces three budgets:

- **Per-request** — estimated before the call; rejected if it would exceed the daily budget.
- **Per-conversation** — `conversation_id` rolling budget.
- **Daily** — engine-wide daily cap.

Pricing table (USD per 1M tokens, input/output):

```python
# src/sovyx/llm/pricing.py (single source of truth)
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-20250514":     (3.0,  15.0),
    "claude-3-5-haiku-20241022":    (1.0,   5.0),
    "claude-opus-4-20250514":       (15.0, 75.0),
    # OpenAI
    "gpt-4o":                       (5.0,  15.0),
    "gpt-4o-mini":                  (0.15,  0.6),
    "o1":                           (15.0, 60.0),
    "o3-mini":                      (1.1,   4.4),
    # Google
    "gemini-2.0-flash":             (0.10,  0.40),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
    # xAI
    "grok-2":                       (2.0,  10.0),
    "grok-3":                       (3.0,  15.0),
    # DeepSeek
    "deepseek-chat":                (0.14,  0.28),
    "deepseek-reasoner":            (0.55,  2.19),
    # Mistral
    "mistral-large-latest":         (2.0,   6.0),
    "mistral-small-latest":         (0.10,  0.30),
    # Together AI / Groq / Fireworks — see pricing.py for full table
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

## Streaming

`LLMRouter.stream()` yields `LLMStreamChunk` objects as the model produces tokens. All ten providers implement `stream()`:

- **Anthropic** — Messages API SSE (`content_block_delta` events).
- **OpenAI / xAI / DeepSeek / Mistral / Together / Groq / Fireworks** — Chat Completions SSE via the shared `OpenAICompatibleProvider` base.
- **Google** — `streamGenerateContent?alt=sse`.
- **Ollama** — NDJSON line-by-line (`stream: true`).

Failover works only before the first chunk. Cost accounting waits for the final `is_final` chunk (cloud providers emit usage at SSE end). `ThinkStreamStarted` event carries `ttft_ms` (time-to-first-token).

The voice pipeline's `VoiceCognitiveBridge` calls `CognitiveLoop.process_request_streaming()` with `pipeline.stream_text` as the callback, so TTS begins synthesizing as soon as the first sentence boundary arrives (~300 ms perceived latency vs 3-7 s without streaming).

## Tool calls

`LLMRouter.generate(tools=...)` accepts a list of `ToolDefinition` objects from the plugin SDK. `tool_definitions_to_dicts()` converts them to the generic JSON shape accepted by all providers. Tool-call results come back as `ToolCall` entries on `LLMResponse`; `ActPhase` executes them and re-invokes the router with the tool outputs appended.

## Observability

- **Metrics** — `llm_calls`, `tokens_used` (direction in/out), `llm_cost`, `llm_response_latency` — each labelled with `provider` and `model`.
- **Tracing** — `tracer.start_llm_span(provider, model)` with attributes `sovyx.llm.tokens_in`, `sovyx.llm.tokens_out`, `sovyx.llm.cost_usd`.
- **Events** — `ThinkCompleted` on every successful call (`streamed` + `ttft_ms` fields for streaming). `ThinkStreamStarted` on first streaming token.

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

- Per-user token isolation in addition to per-mind BYOK.
- Per-chunk output guard (regex pass on each streaming delta, full LLM cascade on final text).

## See also

- `cognitive.md` — `ThinkPhase` is the router's only caller in the loop
- `engine.md` — `ThinkCompleted` on the `EventBus` and DI wiring
- `../architecture.md` — end-to-end flow
