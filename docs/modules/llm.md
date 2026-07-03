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

All ten providers are implemented on top of `httpx` — no vendor SDKs are required at runtime. The seven OpenAI-compatible providers (OpenAI, xAI, DeepSeek, Mistral, Together AI, Groq, Fireworks) share a base class (`OpenAICompatibleProvider`) that handles `generate()` + `stream()` + retry + error handling; each provider file is ~30 LOC of configuration.

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

Tier-to-model mapping is driven by `select_model_for_complexity` in `src/sovyx/llm/router.py` and the **single source of truth** for supported models and their prices is `src/sovyx/llm/pricing.py`. Tiers span every active provider (fast Haiku/Flash/mini on SIMPLE; flagship Sonnet/Opus/Pro/GPT-4o/Grok/Reasoner on COMPLEX). See `pricing.py` for the authoritative list — the doc intentionally does not duplicate model IDs, which move every release.

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

`_get_equivalent_models` in `src/sovyx/llm/router.py` maintains a symmetric equivalence map across three tiers — **flagship** (Sonnet / GPT-4o / Gemini Pro / Grok / Mistral Large), **fast** (Haiku / mini / Gemini Flash / DeepSeek Chat / Mistral Small), and **reasoning** (Opus / o1 / DeepSeek Reasoner). When a requested model fails (circuit open or provider error), the router rotates through the same-tier peers in order. The map is updated each release as new models land in `pricing.py`.

## Cost tracking

`CostGuard` enforces three budgets — the estimated cost of each call is checked against every applicable one before the call runs:

- **Daily** — `budget_daily_usd`, resets each day.
- **Per-conversation** — `budget_per_conversation_usd`, keyed by `conversation_id`.
- **Monthly** — `budget_monthly_usd`, optional (default `null` = disabled); checked in addition to the daily cap.

Prices (USD per 1M input/output tokens) for every supported model live in `src/sovyx/llm/pricing.py` — it is the single source of truth and is updated as providers publish new tiers. The **router** reads that table to compute each response's `cost_usd` and passes the result to `CostGuard.record()`; the doc does not duplicate the entries.

After each successful call the router also updates the counters in `sovyx.dashboard.status` so the dashboard sees live cost and token usage.

## Circuit breaker

`CircuitBreaker` maintains per-provider state. After three consecutive failures the circuit opens; after 60 seconds it transitions to half-open and the next success closes it.

- `can_call(provider) -> bool`
- `record_success(provider)`
- `record_failure(provider)`

The router calls `can_call()` before each provider attempt and records the outcome.

## BYOK — bring your own keys

Provider credentials are **environment variables with their native names**, never `mind.yaml` fields. At bootstrap the daemon first loads `<data_dir>/channel.env` and `<data_dir>/secrets.env` into the process environment (keys saved via the dashboard settings/onboarding flow land in `secrets.env`), then constructs one provider instance for each cloud key present:

| Env var | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `GOOGLE_API_KEY` | Google |
| `XGROK_API_KEY` | xAI |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `MISTRAL_API_KEY` | Mistral |
| `GROQ_API_KEY` | Groq |
| `TOGETHER_API_KEY` | Together AI |
| `FIREWORKS_API_KEY` | Fireworks AI |

Ollama needs no key — it is always registered and pinged at boot; when no cloud key is present and Ollama has pulled models, it is auto-selected as the default provider. The authoritative env-var map is `LLMProviderKey` in `src/sovyx/llm/_provider_registry.py`; construction happens in `engine/bootstrap.py`. Budgets and model choices remain per-mind via `MindConfig.llm`.

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

- **Metrics** — `llm_calls` (`provider`, `model`), `tokens_used` (`direction` in/out, `provider`), `llm_cost` (`provider`), `llm_response_latency` (`provider`).
- **Tracing** — `tracer.start_llm_span(provider, model)` with attributes `sovyx.llm.tokens_in`, `sovyx.llm.tokens_out`, `sovyx.llm.cost_usd`.
- **Events** — `ThinkCompleted` on every successful call (`streamed` + `ttft_ms` fields for streaming). `ThinkStreamStarted` on first streaming token.

## Configuration

Per-mind router settings live under `llm:` in `mind.yaml` (`MindConfig.llm`):

```yaml
llm:
  default_provider: anthropic              # "" for auto-detect from env keys
  default_model: claude-sonnet-4-20250514  # "" for auto-detect
  fast_model: claude-3-5-haiku-20241022    # "" for auto-detect
  local_model: llama3.2:1b                 # Ollama fallback
  temperature: 0.7
  streaming: true
  budget_daily_usd: 2.0
  budget_per_conversation_usd: 0.5
  budget_monthly_usd: null                 # optional monthly cap
```

Credentials are NOT configured here — see "BYOK" above. The circuit-breaker
thresholds are process-global tuning knobs, not YAML config:

```bash
export SOVYX_TUNING__LLM__CIRCUIT_BREAKER_FAILURES=3        # default
export SOVYX_TUNING__LLM__CIRCUIT_BREAKER_RESET_SECONDS=60  # default
```

## Roadmap

- Per-user token isolation in addition to per-mind BYOK.
- Per-chunk output guard (regex pass on each streaming delta, full LLM cascade on final text).

## See also

- `cognitive.md` — `ThinkPhase` is the router's only caller in the loop
- `engine.md` — `ThinkCompleted` on the `EventBus` and DI wiring
- `../architecture.md` — end-to-end flow
