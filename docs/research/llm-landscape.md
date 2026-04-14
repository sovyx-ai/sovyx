# LLM Landscape — Research Notes

> **Scope**: panorama atual de providers LLM suportados pelo Sovyx, estratégia de routing por complexidade, implicações de custo, e features novas (prompt caching, tool use, streaming, local-first via Ollama).
>
> **Research base**: `SOVYX-VR-002-OLLAMA-CASE-STUDY.md`, `SOVYX-VR-085-CLOUD-LLM-PROXY` (referenciado em `sovyx-cloud-platform.md`), `SOVYX-BKD-SPE-007-LLM-ROUTER.md`, implementação em `src/sovyx/llm/`.

---

## 1. Panorama atual (2026-Q1/Q2)

Sovyx suporta 4 providers LLM como cidadãos de primeira classe — escolhidos por cobrir o espectro de latência, custo e soberania.

### 1.1 Anthropic (Claude 4.5 família)

- **Modelos**: Claude 4.5 Sonnet (generalista), Claude 4.5 Haiku (rápido/barato), Opus 4.6 (raciocínio profundo — contexto 1M).
- **Força**: qualidade de raciocínio consistentemente no top-1/top-2 em benchmarks; excelente seguimento de instruções longas.
- **Features diferenciais**:
  - **Prompt caching** (beta estável): blocos marcados com `cache_control` reduzem custo em até 90% nas reads subsequentes dentro da TTL de 5 min (ou 1h extended).
  - **Tool use** nativo via `content_block_delta` com `input_json_delta`.
  - **Extended thinking** (thinking blocks) em modelos Opus.
- **Uso recomendado no Sovyx**: COMPLEX tier por padrão (context assembly pesado + tool calls).

### 1.2 OpenAI (GPT-4o / GPT-4.1 / o-series)

- **Modelos**: GPT-4o (multimodal baseline), GPT-4.1 (long-context), o1/o3 (reasoning models com chain-of-thought implícita).
- **Força**: tool calling maduro, suporte wide pra function calling, latência baixa em gpt-4o-mini.
- **Features diferenciais**:
  - **Tool calls** via campo `tool_calls` (array), formato diferente do Anthropic.
  - **Structured outputs** (JSON schema strict).
  - Embedding models (text-embedding-3-small / 3-large) — não usados no Sovyx (roda ONNX local).
- **Uso recomendado no Sovyx**: SIMPLE/MODERATE tier (gpt-4o-mini) quando custo importa e latência é crítica.

### 1.3 Google (Gemini 2.5 Pro / Flash)

- **Modelos**: Gemini 2.5 Pro (janela de contexto de 2M tokens), Gemini 2.5 Flash (barato, rápido).
- **Força**: contexto gigante (útil pra context assembly sem pruning agressivo), multimodal nativo.
- **Features diferenciais**:
  - **Context caching** (Google): similar ao Anthropic mas com pricing diferente (cost-per-hour de cache storage).
  - **Grounding** com Google Search (não usado no Sovyx — local-first).
- **Uso recomendado no Sovyx**: fallback MODERATE tier quando BYOK do user aponta Google.

### 1.4 Ollama (local)

- **Modelos servidos**: Llama 3.x/4.x, Qwen 2.5/3, Gemma 3, Mistral Nemo, DeepSeek, Phi-3.5 — qualquer GGUF do Ollama Hub.
- **Força**: zero custo marginal, zero telemetria, roda on-prem (Pi 5 com modelos 3B, workstation com 30B-70B).
- **API shape**: compatível com OpenAI (`/v1/chat/completions` endpoint) + API nativa (`/api/chat`).
- **Uso recomendado no Sovyx**: provider default em modo `local-first`; usado em Free tier sem BYOK.
- **Case study**: VR-002 documenta como Ollama cresceu 520× em downloads (100K/mês → 52M/mês) em 3 anos via simplicidade `ollama run llama3`. Sovyx trata Ollama como **infraestrutura complementar** — "Ollama = motor, Sovyx = cérebro".

---

## 2. Routing strategy — tiers de complexidade

Implementado em `src/sovyx/llm/router.py` (ver também `SPE-007-LLM-ROUTER`).

### 2.1 Os três tiers

```
SIMPLE    → saudações curtas, intent classification, confirmações
          → modelos: gpt-4o-mini, Haiku, Gemini Flash, Llama-3 8B local
          → TTL budget: 500-1500 tokens
          → latência alvo: <1s

MODERATE  → respostas conversacionais, retrieval-augmented
          → modelos: Sonnet, GPT-4o, Gemini 2.5 Flash, Qwen 2.5 32B
          → TTL budget: 2000-6000 tokens
          → latência alvo: <3s

COMPLEX   → raciocínio multi-passo, tool use, planning
          → modelos: Opus, o1/o3, Gemini 2.5 Pro
          → TTL budget: 8000-32000 tokens
          → latência alvo: <10s (ou streaming)
```

### 2.2 Sinais de classificação

`classify_complexity()` em `llm/router.py` combina:

- Tamanho do prompt (`len(messages)` + token count).
- Presença de tool schemas (sempre upgrade MODERATE → COMPLEX quando há tools).
- Categoria semântica do turn (detectada via heurística barata — keywords tipo "plan", "code", "analyze").
- Profile override via `MindConfig.llm.default_complexity`.

O resultado é um `ComplexityLevel` (StrEnum — ver CLAUDE.md anti-pattern #9) que o router mapeia pro provider/modelo disponível.

### 2.3 Cascade fallback

Quando o provider primário falha (circuit breaker aberto, rate limit, timeout):

```
Anthropic COMPLEX  → OpenAI COMPLEX  → Google COMPLEX  → Ollama local
                                                        (graceful degrade)
```

Implementado via `DegradationManager` (`engine/degradation.py`) — ver `INT-005: Cascading Graceful Degradation`.

---

## 3. Cost trade-offs

### 3.1 Pricing por tier (ordem de magnitude, USD/1M tokens — Q1 2026)

| Modelo | Input | Output | Cache read | Nota |
|---|---:|---:|---:|---|
| Claude Opus 4.6 | $15 | $75 | $1.50 | Reasoning premium |
| Claude Sonnet 4.5 | $3 | $15 | $0.30 | Workhorse |
| Claude Haiku 4.5 | $0.80 | $4 | $0.08 | Rápido |
| GPT-4o | $2.50 | $10 | — | |
| GPT-4o-mini | $0.15 | $0.60 | — | Cost winner cloud |
| o1 | $15 | $60 | — | Reasoning |
| Gemini 2.5 Pro | $1.25 | $5 | — | Best context |
| Gemini 2.5 Flash | $0.10 | $0.40 | — | Cheapest cloud |
| Ollama local | $0 | $0 | — | Hardware only |

### 3.2 Prompt caching — quanto importa

Anthropic cobra **10% do preço de input** pra leitura de cache. Sovyx cacheia:

- System prompt (personality + mind config) — alto reuso por mente.
- History (últimos N turns) — reuso em cada turn subsequente.
- Brain context slots que raramente mudam (ex: `self_knowledge`, `long_term_facts`).

**Economia estimada**: em uma conversa de 20 turns com ~4K token system prompt fixo, caching reduz o custo de input em ~70-85% cumulativo (as reads seguintes pagam $0.30/M em vez de $3/M pra Sonnet).

**Cross-ref**: `SPE-007-LLM-ROUTER` §cost-tracker + `src/sovyx/llm/cost.py`.

### 3.3 BYOK-first economics

`SOVYX-BKD-PRD-002-PRICING-MONETIZATION.md` §1.2 — insight central:

> Sovyx inverte o modelo SaaS tradicional. O usuário traz a própria chave (BYOK), então o custo marginal de LLM por Free-tier user é $0. Sovyx monetiza a **camada de valor** (routing, caching, analytics, Cloud proxy), não o acesso ao LLM.

---

## 4. Prompt caching — features avançadas

Anthropic (principal feature diferencial):

- **cache_control** blocks (4 blocos max por prompt): cacheáveis por TTL de 5 min default, ou 1h com flag `beta=extended-cache-ttl-2025-04-11`.
- **Ordering**: cache blocks devem ser prefix-stable — qualquer byte diferente antes invalida o cache.
- **Implicações pro Sovyx**:
  - `ContextAssembler` deve emitir system prompt em formato determinístico (ordering fixo de slots), senão o cache nunca hit.
  - Slots com conteúdo volátil (ex: `temporal_context` com timestamp now()) devem ir **depois** dos estáticos.
- **Status no código**: parcialmente implementado — cache_control é adicionado em `llm/providers/anthropic.py` mas o ordering determinístico ainda depende de `ContextAssembler` emitir sempre a mesma estrutura. Ver issue aberta em v0.6 roadmap.

Google (Gemini):

- Context caching API separada (`POST /cachedContents`) — storage charged per hour de cache active.
- Não implementado no Sovyx ainda — marcado `[NOT IMPLEMENTED]` em v0.6 backlog.

OpenAI:

- Sem prompt caching explícito até Q1 2026 (implicit cache interno mas não exposto na API).

---

## 5. Tool use — formatos por provider

Heterogeneidade que o `LLMProvider` abstrai:

### 5.1 Anthropic

```python
# Tool definition
{"name": "search_brain", "description": "...", "input_schema": {...}}

# Tool call no response (content_block)
{"type": "tool_use", "id": "tool_xyz", "name": "search_brain", "input": {...}}

# Tool result de volta
{"type": "tool_result", "tool_use_id": "tool_xyz", "content": "..."}
```

Streaming via `content_block_delta` com `input_json_delta` (incremental JSON).

### 5.2 OpenAI

```python
# Tool definition (function)
{"type": "function", "function": {"name": "search_brain", "parameters": {...}}}

# Tool call no response
{"tool_calls": [{"id": "call_xyz", "type": "function", "function": {"name": "...", "arguments": "..."}}]}

# Tool result
{"role": "tool", "tool_call_id": "call_xyz", "content": "..."}
```

### 5.3 Ollama

Compatível com OpenAI tool calling desde v0.3+ (formato `tool_calls` igual OpenAI). Nem todos os modelos locais suportam — depende do fine-tune do modelo (Llama 3.1+, Qwen 2.5+, Mistral Nemo OK).

### 5.4 Normalização no Sovyx

`LLMProvider` abstract emite `ToolCall` dataclass com shape comum. Conversões vivem em `llm/providers/{anthropic,openai,ollama,google}.py`. Teste em `tests/unit/llm/test_tool_normalization.py`.

---

## 6. Streaming

### 6.1 Unified chunk format (parcial)

Sovyx expõe streaming via `LLMProvider.astream()` retornando `AsyncIterator[LLMChunk]` onde `LLMChunk` tem:

- `delta_text: str | None`
- `tool_call_delta: ToolCallDelta | None`
- `usage: UsageInfo | None` (último chunk)
- `stop_reason: str | None`

Providers implementados:

- ✅ Anthropic — `content_block_delta` / `message_delta` / `message_stop` mapped.
- ✅ OpenAI — `chat.completion.chunk` mapped.
- ✅ Ollama — nativo `/api/chat` stream.
- ⚠️ Google (Gemini) — streaming implementado mas usage info chega parcial; pendente [NOT IMPLEMENTED — v0.6] harmonização final.

### 6.2 Streaming pra TTS — gap reconhecido

`gap-analysis.md` §tabela `llm` menciona: "Streaming pra TTS especulativo não exposto". A intenção é pipelinar chunks do LLM direto pro TTS pra latência end-to-end baixa (alvo <500ms first-audio). Marcado `[NOT IMPLEMENTED — v0.6]`.

**Doc-fonte**: `SPE-007-LLM-ROUTER` §streaming.

---

## 7. Local-first via Ollama — quando e como

### 7.1 Quando usar Ollama

- **Free tier sem BYOK**: o usuário não deu API key; Sovyx detecta Ollama em localhost:11434 (env `SOVYX_LLM__OLLAMA__BASE_URL`) e roteia tudo pra lá.
- **Mode `sovereign`**: `MindConfig.llm.mode = "local_only"` força 100% Ollama independentemente de keys configuradas.
- **Fallback de último recurso** quando todos os cloud providers falharam.

### 7.2 Hardware requirements (validados empiricamente)

| Hardware | Modelo viável | Latência típica |
|---|---|---|
| Raspberry Pi 5 8GB | Llama 3.2 3B Q4 | 2-8 tok/s |
| Mini PC N100 | Qwen 2.5 7B Q4 | 5-15 tok/s |
| Mac M2 Pro 16GB | Qwen 2.5 32B Q4 | 20-30 tok/s |
| RTX 3090 24GB | Llama 3.1 70B Q4 | 30-50 tok/s |

Sovyx roda confortável em Mini PC N100 com modelo 7-8B pra conversação conversacional. Pi 5 é viável pra assistente de casa usando modelo 3B.

### 7.3 Modelfile pattern

VR-002 §distribution recomenda publicar um `Sovyx Modelfile` no Ollama Hub como discovery passivo (cada download do modelo vira um hit pra Sovyx). Marcado `[PLANNED — v0.6 launch week]`.

---

## 8. Referências

- `vps-brain-dump/memory/confidential/sovyx-bible/research/SOVYX-VR-002-OLLAMA-CASE-STUDY.md`
- `vps-brain-dump/memory/nodes/sovyx-cloud-platform.md` (ref VR-085 LLM Proxy)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/product/SOVYX-BKD-PRD-002-PRICING-MONETIZATION.md` §1.2 (BYOK economics)
- `docs/_meta/gap-analysis.md` §llm (gaps: streaming pra TTS, BYOK isolation)
- Código: `src/sovyx/llm/router.py`, `src/sovyx/llm/providers/*.py`, `src/sovyx/llm/cost.py`

---

_Última revisão: 2026-04-14._
