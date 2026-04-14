# Módulo: mind

## Objetivo

`sovyx.mind` materializa o princípio "Mind is configuration, not code".
Carrega e valida `mind.yaml` via pydantic, expõe a configuração completa
da mente (personalidade, traços OCEAN, LLM, brain scoring, canais,
safety, plugins), e traduz esses traços em um system prompt injectável
no LLM via `PersonalityEngine`. Cada mente tem seu próprio diretório
`~/.sovyx/minds/{name}/` com `brain.db`, `conversations.db` e o próprio
`mind.yaml`.

## Responsabilidades

- Definir o schema pydantic completo do `mind.yaml`: `MindConfig` +
  sub-modelos `PersonalityConfig`, `OceanConfig`, `LLMConfig`,
  `ScoringConfig`, `BrainConfig`, `ChannelsConfig`, `SafetyConfig`,
  `PluginsConfig`.
- Carregar `mind.yaml` com validação (`load_mind_config`), erros claros
  (`MindConfigError`), e criar defaults (`create_default_mind_config`).
- Auto-detectar provider/modelos LLM em runtime baseado em API keys
  presentes no ambiente (resolve `default_provider`, `default_model`,
  `fast_model` quando strings vazias).
- Validar somas de weights de scoring (importance=1.0, confidence=1.0).
- Gerar system prompt a partir de OCEAN + traços comportamentais +
  guardrails + safety rules + instruction integrity.
- Carregar guardrails default (honesty, privacy, safety) e permitir
  custom rules + shadow patterns.

## Arquitetura

`MindConfig` é o modelo raiz; sub-modelos descrevem cada seção de
`mind.yaml`. Pydantic v2 faz a validação via `model_validator(mode="after")`
em três pontos:

- `LLMConfig.resolve_provider_at_runtime`: preenche campos vazios
  baseado em `ANTHROPIC_API_KEY` > `OPENAI_API_KEY` > `GOOGLE_API_KEY`.
- `ScoringConfig.validate_weight_sums`: garante que importance_* e
  confidence_* somam 1.0 com tolerância 0.01.
- `MindConfig.set_default_id`: gera `id` a partir de `name.lower().replace(" ", "-")`.

`PersonalityEngine` consome o `MindConfig` e produz um system prompt
multilinear com seções: identity, communication style, OCEAN traits,
verbosity, language, safety (content filter, financial confirmation,
child safe mode), guardrails customizados, instruction integrity
(não-negociável, anti-injection).

Tom é mapeado por `_TONE_MAP` (warm/neutral/direct/playful). OCEAN é
quebrado em 3 níveis via `_level()` (thresholds 0.33 e 0.66) com
descritores distintos para low/mid/high. Valores numéricos (formality,
humor, etc.) viram textos descritivos por `_formality_desc`,
`_humor_desc`, etc.

## Código real

```python
# src/sovyx/mind/config.py:24-43 — personalidade + OCEAN
class PersonalityConfig(BaseModel):
    tone: Literal["warm", "neutral", "direct", "playful"] = "warm"
    formality: float = Field(default=0.5, ge=0.0, le=1.0)
    humor: float = Field(default=0.4, ge=0.0, le=1.0)
    assertiveness: float = Field(default=0.6, ge=0.0, le=1.0)
    curiosity: float = Field(default=0.7, ge=0.0, le=1.0)
    empathy: float = Field(default=0.8, ge=0.0, le=1.0)
    verbosity: float = Field(default=0.5, ge=0.0, le=1.0)


class OceanConfig(BaseModel):
    openness: float = Field(default=0.7, ge=0.0, le=1.0)
    conscientiousness: float = Field(default=0.6, ge=0.0, le=1.0)
    extraversion: float = Field(default=0.5, ge=0.0, le=1.0)
    agreeableness: float = Field(default=0.7, ge=0.0, le=1.0)
    neuroticism: float = Field(default=0.3, ge=0.0, le=1.0)
```

```python
# src/sovyx/mind/config.py:70-109 — auto-detection de LLM por env
@model_validator(mode="after")
def resolve_provider_at_runtime(self) -> LLMConfig:
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_google = bool(os.environ.get("GOOGLE_API_KEY"))
    if not self.default_model:
        if has_anthropic: self.default_model = "claude-sonnet-4-20250514"
        elif has_openai:  self.default_model = "gpt-4o"
        elif has_google:  self.default_model = "gemini-2.5-pro-preview-03-25"
    ...
    return self
```

```python
# src/sovyx/mind/personality.py:86-102 — system prompt generation
def generate_system_prompt(self, emotional_state: dict[str, float] | None = None) -> str:
    """Generate system prompt with personality.

    Args:
        emotional_state: v0.1 IGNORED. v0.5+: valence/arousal/dominance
            modifies prompt tone based on emotional state.
    """
    cfg = self._config
    sections: list[str] = []
    sections.append(f"You are {cfg.name}, a personal AI Mind.")
```

```python
# src/sovyx/mind/config.py:213-235 — default guardrails
DEFAULT_GUARDRAILS: tuple[Guardrail, ...] = (
    Guardrail(id="honesty", rule="Always be truthful. Never fabricate facts, ...",
              severity="critical", builtin=True),
    Guardrail(id="privacy", rule="Never reveal, store, or transmit personal data...",
              severity="critical", builtin=True),
    Guardrail(id="safety", rule="Never provide instructions for harm, violence, ...",
              severity="critical", builtin=True),
)
```

## Specs-fonte

- `SOVYX-BKD-SPE-002-MIND-DEFINITION.md` — identity, personality, config
  schema, behavioral traits.
- `SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md` — PAD 3D, emotional baseline,
  homeostasis_rate.

## Status de implementação

### ✅ Implementado

- **Mind directory structure**: `~/.sovyx/minds/{name}/` com brain.db,
  conversations.db, mind.yaml (resolvido pelo `EngineConfig.data_dir`).
- **Schema completo de `mind.yaml`** (`config.py`, 553 LOC):
  - `PersonalityConfig`: tone, formality, humor, assertiveness,
    curiosity, empathy, verbosity.
  - `OceanConfig`: 5 traços Big Five com defaults razoáveis.
  - `LLMConfig`: provider auto-detect, temperature, streaming, budgets.
  - `ScoringConfig`: 5 importance weights + 4 confidence weights com
    validação de soma.
  - `BrainConfig`: consolidation_interval_hours, dream_time, max_concepts,
    forgetting_enabled, decay_rate, min_strength, nested scoring.
  - `ChannelsConfig`: Telegram + Discord com `token_env` (nome da env
    var, não o valor).
  - `SafetyConfig`: child_safe_mode, financial_confirmation,
    content_filter, pii_protection, guardrails, custom_rules,
    banned_topics, shadow_mode, shadow_patterns.
  - `PluginsConfig`: enabled/disabled whitelists, plugins_config
    per-plugin (config + permissions), tool_timeout_s.
- `load_mind_config(path)` com erros limpos (`MindConfigError`).
- `create_default_mind_config(name, data_dir)` serializa omitindo os
  campos LLM resolvidos para que auto-detect rode a cada startup.
- `validate_plugin_config(config, schema)` — JSON Schema-like validator
  básico para plugin configs.
- **`PersonalityEngine`** (`personality.py`, 249 LOC) — **extra, não
  documentado em SPE-002**: gera system prompt com tom + OCEAN +
  verbosity + safety + guardrails + anti-injection. `get_personality_summary()`
  para debug/dashboard.
- **Child-safe mode**: system prompt hardcoded (não configurável por
  usuário, safety-critical) quando `safety.child_safe_mode=True`.
- **Anti-injection hardening**: sempre presente no system prompt,
  não-configurável.

### ❌ [NOT IMPLEMENTED]

- **Emotional baseline config em `MindConfig`**: ADR-001 pede
  `emotional.baseline.{valence, arousal, dominance}` + `homeostasis_rate`.
  Atualmente não há seção `emotional:` em `MindConfig`. Estado emocional
  é armazenado por `Episode` (valence+arousal 2D) mas não é configurável
  por mente.
- `PersonalityEngine.generate_system_prompt(emotional_state=...)`: o
  parâmetro existe na assinatura, mas é **ignored em v0.1**. O docstring
  anota "v0.5+: valence/arousal/dominance modifies prompt tone".

## Divergências [DIVERGENCE]

- [DIVERGENCE] ADR-001 §2 decide PAD 3D (valence + arousal + dominance)
  como modelo emocional normativo. `MindConfig` é 0D (sem baseline
  config) e `Episode` é 2D (sem dominance). Acoplado ao gap equivalente
  em `brain`: migration schema + config section necessárias para v1.0.

## Dependências

- **Externas**: `pydantic`, `pyyaml`.
- **Internas**: `sovyx.engine.errors.MindConfigError`,
  `sovyx.engine.types.MindId`, `sovyx.observability.logging`.
- **Consumido por**: `ContextAssembler` (timezone, personality),
  `ThinkPhase` (llm config), `BrainService` (scoring weights),
  `PluginManager` (permissions, configs), `BridgeManager` (channels),
  todas as safety guards em `cognitive/*`.

## Testes

- `tests/unit/mind/test_config.py` — validação de weights, auto-detect
  de LLM, defaults, erros em YAML inválido.
- `tests/unit/mind/test_personality.py` — todos os paths de system
  prompt (tone variants, OCEAN levels, safety modes, guardrails).
- `tests/unit/mind/test_config_defaults.py` — `create_default_mind_config`
  omite campos LLM resolvidos.
- `tests/integration/test_mind_load.py` — carga real de YAML com
  diversos perfis.
- Property tests: Hypothesis em `OceanConfig` para verificar todos os
  paths de `_level()` (low/mid/high).

## Referências

- Code: `src/sovyx/mind/config.py`, `src/sovyx/mind/personality.py`.
- Specs: `SOVYX-BKD-SPE-002-MIND-DEFINITION.md`,
  `SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-A-core.md` §mind.
