# Module: mind

## What it does

`sovyx.mind` materializes the principle "Mind is configuration, not code". Every behavioral axis — personality, LLM selection, scoring weights, safety guardrails, channel bindings, plugin permissions — is a field on `MindConfig`. The `PersonalityEngine` translates that config into the system prompt injected at the start of every LLM call.

## Key classes

| Name | Responsibility |
|---|---|
| `MindConfig` | Root Pydantic model — owns all sub-configs for a single Mind. |
| `PersonalityConfig` | Tone, formality, humor, assertiveness, curiosity, empathy, verbosity. |
| `OceanConfig` | Big Five personality traits (openness, conscientiousness, extraversion, agreeableness, neuroticism). |
| `LLMConfig` | Provider auto-detection, model selection, temperature, streaming flag, budget. |
| `ScoringConfig` | Importance + confidence weight vectors (sum=1.0 validated). |
| `BrainConfig` | Consolidation interval, dream schedule, decay rate, max concepts. |
| `EmotionalBaselineConfig` | PAD 3D baseline (valence, arousal, dominance) + homeostasis rate. |
| `SafetyConfig` | Content filter, child-safe mode, financial confirmation, custom rules. |
| `PersonalityEngine` | Translates config into the LLM system prompt. |

## Auto-detection

When `default_model`, `default_provider`, or `fast_model` are empty strings, a `model_validator` resolves them from environment API keys at startup:

| Priority | Key | `default_model` | `fast_model` |
|---|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` | `claude-3-5-haiku-20241022` |
| 2 | `OPENAI_API_KEY` | `gpt-4o` | `gpt-4o-mini` |
| 3 | `GOOGLE_API_KEY` | `gemini-2.5-pro-preview-03-25` | `gemini-2.0-flash` |
| 4 | `XGROK_API_KEY` | `grok-2` | — |
| 5 | `DEEPSEEK_API_KEY` | `deepseek-chat` | `deepseek-chat` |
| 6 | `MISTRAL_API_KEY` | `mistral-large-latest` | `mistral-small-latest` |
| 7 | `GROQ_API_KEY` | `llama-3.1-70b-versatile` | `mixtral-8x7b-32768` |

## Configuration

```yaml
# ~/.sovyx/my-mind/mind.yaml
name: my-mind
language: en
timezone: America/Sao_Paulo

personality:
  tone: warm           # warm | neutral | direct | playful
  formality: 0.5       # 0.0 (casual) – 1.0 (formal)
  humor: 0.4
  empathy: 0.8
  verbosity: 0.5

ocean:
  openness: 0.7
  conscientiousness: 0.6
  extraversion: 0.5
  agreeableness: 0.7
  neuroticism: 0.3

llm:
  default_model: ""     # empty = auto-detect from API keys
  temperature: 0.7
  streaming: true
  budget_daily_usd: 2.0

scoring:
  importance:
    category_base: 0.15
    llm_assessment: 0.35
    emotional: 0.10
    novelty: 0.15
    explicit_signal: 0.25

safety:
  child_safe_mode: false
  content_filter: standard
  financial_confirmation: true
```

## Roadmap

- Personality prompt modulation driven by PAD emotional state.
- Per-mind voice persona (TTS voice selection from personality traits).

## See also

- Source: `src/sovyx/mind/config.py`, `src/sovyx/mind/personality.py`.
- Tests: `tests/unit/mind/`.
- Related: [`cognitive`](./cognitive.md) (consumes personality via system prompt), [`llm`](./llm.md) (LLMConfig drives model selection), [`brain`](./brain.md) (ScoringConfig drives importance weights).
