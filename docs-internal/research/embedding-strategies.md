# Embedding Strategies — Research Notes

> **Scope**: decisões de embedding/retrieval no Sovyx — modelos ONNX locais, dimensões, vector storage via sqlite-vec, hybrid retrieval (semantic + keyword) e spreading activation como camada cognitiva.
>
> **Research base**: `SPE-004-BRAIN-MEMORY`, `IMPL-002-BRAIN-ALGORITHMS`, `ADR-004-DATABASE-STACK`, `sovyx-60-seconds.md` (stack summary).

---

## 1. Embedding models

### 1.1 ONNX Runtime como runtime padrão

Decisão: Sovyx roda embeddings **localmente via ONNX Runtime**. Nunca chama APIs remotas (OpenAI text-embedding-3, Cohere) — viola o princípio local-first (`ADR-008-LOCAL-FIRST`).

Motivações:

- Zero telemetria (dados do Brain nunca saem do host).
- Custo marginal zero (embeddings são gerados em **cada** episode + concept + chunk — seriam a dor de $/token mais alta se cloud).
- Latência baixa em CPU ARM64 (Pi 5) e x86 (mini PC).

### 1.2 Modelo padrão: gte-small / all-MiniLM

O código em `src/sovyx/brain/embedding.py` carrega (via HF → ONNX export) um dos seguintes:

- **all-MiniLM-L6-v2** (384 dim, ~22M params, ~80MB) — default histórico, inglês forte, multilíngue razoável.
- **gte-small** (384 dim, ~33M params) — alternativa mais recente, melhor em retrieval benchmarks (MTEB).
- **E5-small-v2** (384 dim, ~33M params) — citado em `sovyx-60-seconds.md` como stack oficial.

A escolha é configurável via `BrainConfig.embedding_model_id`. O `MASTER-BLUEPRINT` referencia **E5-small-v2** como baseline; `IMPL-002` lista MiniLM como fallback quando E5 indisponível.

### 1.3 Lazy loading + caching

`EmbeddingEngine.ensure_loaded()` (documentado após audit v9 §7 do MISSION-AUDIT-V8) carrega o modelo na primeira chamada de `encode()`. Evita custo de startup quando o Mind ainda não processou nada.

---

## 2. Dimensões — trade-offs

| Dim | Modelos típicos | Prós | Contras |
|---:|---|---|---|
| 384 | MiniLM, gte-small, E5-small | 3-4× storage barato; rápido; ONNX leve | Menos expressivo em multi-língua longo |
| 768 | BGE-base, gte-base | Melhor recall em domínio específico | 2× storage; 2-3× latência |
| 1024 | BGE-large, E5-large | Top retrieval benchmarks | 4× storage, 5-10× latência; RAM-hungry |

**Decisão Sovyx**: **384 dimensões** como default por 3 razões:

1. **Target hardware** inclui Pi 5 (8GB RAM) — modelos 768/1024 dim consomem memória demais.
2. **Volume de embeddings**: cada Mind tem 10K-100K concepts + milhões de episode chunks. A 384 × float32, 100K embeddings = 150MB. A 1024 × float32 = 400MB. Diferença relevante.
3. **Ganho marginal**: MTEB benchmarks mostram ~2-5% de melhora de recall@10 ao subir de 384 pra 768, insuficiente pra justificar o custo operacional.

Upgrade pra 768 dim é viável em hardware top-tier e configurável via `BrainConfig.embedding_dim` (gera migration).

---

## 3. Vector storage — sqlite-vec

### 3.1 Por que sqlite-vec, não pgvector/Qdrant

`ADR-004-DATABASE-STACK` decidiu SQLite como backend único. Razões:

- **DB-per-Mind isolation**: cada mente tem `brain.db` próprio. Postgres exigiria schema multi-tenant + backup orquestrado.
- **Zero operational overhead**: Qdrant/Milvus exigem servidor dedicado, incompatível com Pi 5 deployment.
- **Writer unico + multiple readers** (WAL mode) atende o padrão "CogLoop escreve / Context lê" (`INT-003-brain-concurrency.md`).

`sqlite-vec` é extensão C que roda em-process no SQLite. Suporta:

- `vec0` virtual table: colunas `float[N]` com ANN approximate search.
- Distância: cosine, L2, inner product.
- Filtros com WHERE combinados com top-K.

Schema real da virtual table (extraído de `src/sovyx/persistence/schemas/brain.py` ao habilitar a extensão):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS concept_embeddings USING vec0(
    concept_id TEXT PRIMARY KEY,
    embedding float[384] distance_metric=cosine
);

-- Query típica do HybridRetrieval (semantic side):
SELECT concept_id, distance
FROM concept_embeddings
WHERE embedding MATCH ?  -- bytes do query embedding
ORDER BY distance
LIMIT 20;
```

### 3.2 Fallback graceful — sem sqlite-vec

`MISSION-AUDIT-V8` §issue #1 (CRITICAL): migrations iniciais quebravam bootstrap quando sqlite-vec não estava disponível. Fix: `get_brain_migrations(has_sqlite_vec)` gera schema condicional. Em hosts sem a extensão:

- Queries vetoriais degradam pra `Episode.get_recent()` chronological (audit v8 §issue #10).
- Sistema continua operacional; retrieval perde precisão semântica mas ganha previsibilidade.

Esta é uma instância concreta de cascading degradation (`INT-005`).

### 3.3 Status no código

`gap-analysis.md` tabela persistence: "extensão sqlite-vec carrega mas queries não visíveis no scan". Vector search end-to-end está marcado `[VERIFY — v0.6]` na auditoria — a extensão é carregada corretamente (`brain/embedding.py`), mas a evidência de queries ANN no caminho retrieval precisa ser auditada (`persistence/manager.py`).

---

## 4. Hybrid retrieval — semantic + keyword (FTS5)

### 4.1 Motivação

Embeddings semânticos perdem em queries lexicais específicas:

- Queries com código, nomes próprios, números, datas, tokens raros.
- Queries curtas com intenção de **match exato** ("what did I say about Ebbinghaus yesterday?").

SQLite FTS5 é o complemento natural — full-text search com ranking BM25, tokenizer configurável (porter, unicode61).

### 4.2 Arquitetura híbrida

Implementada em `src/sovyx/brain/retrieval.py` (`HybridRetrieval`):

```
Query → parallel:
         ├─ semantic: embed(query) → sqlite-vec top-K
         └─ keyword:  FTS5 MATCH query → BM25 ranked top-K

  → Reciprocal Rank Fusion (RRF):
         score(doc) = Σ 1 / (k + rank_in_list(doc))
         k default = 60
  → final top-N
```

Implementação do RRF é clássica (Cormack et al. 2009). Default k=60 é empírico do domínio IR.

### 4.3 FTS5 considerations

`MISSION-AUDIT-V8` §issue #5 (MEDIUM): sanitização de FTS5 query — tokens tipo `AND`, `OR`, `NOT`, `*` têm significado especial. Sovyx wrap queries em aspas duplas pra busca literal, com fallback pra query livre quando user explicitamente pede operators. Documentado em `brain/retrieval.py` docstrings.

`MISSION-AUDIT-V9` §issue #7 (MEDIUM): **FTS5 tokenizer sem stemming pra português**. Aceito como limitação v0.1 — embeddings multilíngues compensam o gap.

---

## 5. Spreading activation — camada cognitiva

### 5.1 Origem

Collins & Loftus 1975 (ver também `docs/research/memory-systems.md`). Modelo de grafo: conceitos são nodes, relações semânticas são edges com peso, ativação propaga do(s) node(s) seed diminuindo por um fator de decay a cada hop.

### 5.2 Implementação no Sovyx

`src/sovyx/brain/activation.py` (`SpreadingActivation` classe):

```
Input: concept_seeds (ids) com activation inicial.

Para cada hop t = 1..max_hops:
  Para cada edge (u → v) com peso w:
     activation(v) += activation(u) * w * decay^t

Resultado: scores por concept, ordenados.
```

- `max_hops` default 3 (além de 3 hops o signal-to-noise fica ruim).
- `decay` default 0.5 por hop.
- `max_activation_per_node` cap pra evitar blow-up (documentado em `MISSION-AUDIT-V9` §issue #9: scores podem ir >1.0 por design — não são probabilidades, são rankings).

### 5.3 Quando usar spreading activation vs embeddings

| Caso de uso | Mecanismo ideal |
|---|---|
| "find memories similar in meaning" | Embedding semantic search |
| "find related concepts starting from X" | Spreading activation |
| "retrieve exact phrase" | FTS5 keyword |
| "context assembly full-range" | Hybrid (RRF) + activation-boost |

Na prática, `ContextAssembler` usa **ambos**: retrieval híbrido pra trazer episodes candidatos, spreading activation pra expandir o set de concepts relacionados no grafo.

---

## 6. Complementos — Hebbian learning + Ebbinghaus decay

Não são embedding strategies stricto sensu, mas fecham o ciclo cognitivo:

- **Hebbian learning** (`brain/learning.py`): concepts co-ativados têm seu edge weight reforçado. "Neurons that fire together wire together" (Hebb 1949).
- **Ebbinghaus decay** (`brain/learning.py`): importance e recall probability decaem exponencialmente com o tempo desde last-access, mas são reset em cada retrieval (spaced repetition pattern).

Conseqüência prática: **embeddings não são a única fonte de "relevância"**. A conjunção embedding-similarity + edge-weight-Hebbian + recency-Ebbinghaus + emotional-valence forma o scoring final.

Ver `ImportanceWeights` em `src/sovyx/brain/scoring.py` (do gap-analysis: `cat=0.15, llm=0.35, emo=0.10, novelty=0.15, explicit=0.25`).

---

## 7. Referências

- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-004-BRAIN-MEMORY.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-004-DATABASE-STACK.md`
- `vps-brain-dump/memory/nodes/int-003-brain-concurrency.md`
- `vps-brain-dump/memory/nodes/int-005-cascading-degradation.md`
- `vps-brain-dump/SOVYX-MISSION-AUDIT-V8.md` §issues #1, #5, #7, #9, #10
- Código: `src/sovyx/brain/{embedding,retrieval,activation,learning,scoring}.py`, `src/sovyx/persistence/manager.py`

---

_Última revisão: 2026-04-14._
