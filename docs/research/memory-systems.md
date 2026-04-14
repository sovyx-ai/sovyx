# Memory Systems — Research Notes

> **Scope**: fundamentos neurocientíficos e psicológicos que inspiram o desenho do Brain do Sovyx, e como cada um mapeia (ou não mapeia) pro código atual.
>
> **Research base**: `SPE-004-BRAIN-MEMORY`, `IMPL-002-BRAIN-ALGORITHMS`, `ADR-001-EMOTIONAL-MODEL`, `sovyx-60-seconds.md` ("5 regiões neuroscience-based").

---

## 1. Inspiração biológica — as regiões cerebrais

O pitch de alto nível (`sovyx-60-seconds.md`) descreve Sovyx como tendo "5 regiões de memória neuroscience-based: episodic / semantic / procedural + emotional + contextual". Cada região tem analogia biológica direta:

### 1.1 Hippocampus — memória episódica

Papel biológico: formar memórias episódicas novas (quando/onde/com quem aconteceu algo), consolidá-las durante sono lento, e indexá-las com contexto espaço-temporal.

No Sovyx:

- `Episode` (em `src/sovyx/brain/models.py`) armazena turns/conversas com timestamp, person_id, conversation_id, e embedding.
- **Consolidation** (`src/sovyx/brain/consolidation.py`) espelha a ideia de replay hipocampal — merging de episodes similares, strengthening de conceitos co-ativados, pruning de episódios obsoletos.
- Reference: Squire & Kandel (1999), *Memory: From Mind to Molecules*.

### 1.2 Neocortex — memória semântica

Papel biológico: conhecimento estável, abstraído do contexto episódico. "Capitals of countries", "what is a bicycle", "properties of water".

No Sovyx:

- `Concept` (em `brain/models.py`) é o equivalente. Abstração livre de timestamp (tem `created_at` mas o significado é atemporal).
- Concepts ganham `importance` cumulativa via acessos e nascem de consolidation de múltiplos episodes.

### 1.3 Prefrontal cortex — working memory

Papel biológico: mantém 3-7 itens em foco ativo por alguns segundos (Baddeley & Hitch 1974, Baddeley 2000 multi-component model).

No Sovyx:

- `WorkingMemory` em `src/sovyx/brain/working_memory.py` — cache em memória dos últimos N concepts/episodes ativados na conversa corrente.
- Capacidade limitada (default 7 slots, configurable) com displacement LRU — fiel ao modelo biológico.
- Tem `asyncio.Lock` pra evitar race entre CogLoop e Consolidation background (`MISSION-AUDIT-V9` §issue #6).

### 1.4 Amygdala — memória emocional

Papel biológico: marca estímulos com valência emocional; modula a força de consolidação — memórias emocionalmente intensas são mais duráveis.

No Sovyx:

- Cada `Episode` tem fields `valence` e `arousal` (2D atualmente; `ADR-001` decidiu 3D PAD — ver §divergence abaixo).
- `ImportanceWeights` dá peso emocional 0.10 no scoring — intencionalmente baixo, mas presente.

### 1.5 Cerebellum — memória procedural

Papel biológico: habilidades motoras e cognitivas aprendidas (andar de bicicleta, tocar piano, digitar).

No Sovyx:

- Representado como padrões de tool-use aprendidos + personality traits (OCEAN) que afetam como o assistente responde.
- **Não é uma região distinta no grafo** — é codificada implicitamente via `PersonalityEngine` e frequencia de tool calls registrada no BrainService.

---

## 2. Fundamentos temporais — decay

### 2.1 Ebbinghaus 1885 — curva de esquecimento

Hermann Ebbinghaus publicou *Über das Gedächtnis* em 1885 — o estudo empírico fundador da psicologia da memória. Curva de esquecimento segue aproximadamente:

```
R(t) = exp(-t / S)
```

onde R = probabilidade de recall, t = tempo desde o aprendizado, S = strength/stability do traço.

**Insight central**: reativar um traço antes do decay resetar aumenta S — a base de **spaced repetition** (Wozniak → Anki).

### 2.2 Implementação Sovyx

`src/sovyx/brain/learning.py` (`EbbinghausDecay` classe):

- `importance_decay_rate` aplicado em background por um job periódico.
- Cada `retrieve()` (no Brain) age como "reactivation" e reseta parcialmente o decay.
- **Combinado com Hebbian** (ver §3): edges entre concepts decaem se nunca co-ativados, reforçam se frequentes.

Implicação operacional: Sovyx "esquece" naturalmente informação irrelevante sem precisar prune manual agressivo. Memórias importantes flutuam pro topo.

---

## 3. Spreading activation — Collins & Loftus 1975

Allan Collins e Elizabeth Loftus publicaram em *Psychological Review* (1975) "A spreading-activation theory of semantic processing" — modelo que:

- Conceitos são nodes num grafo semântico.
- Ativação se propaga de um node seed via edges com peso, decaindo por hop.
- Explicava priming effects (falar "doctor" facilita reconhecer "nurse").

### 3.1 Conexão com Sovyx

`src/sovyx/brain/activation.py` (`SpreadingActivation`) — implementação direta:

- Seed(s) iniciam com ativação 1.0.
- `max_hops` = 3, `decay` = 0.5.
- Output = scores (não probabilidades — ver `MISSION-AUDIT-V9` §issue #9).

Usado em `ContextAssembler` pra expandir o set de concepts relevantes ao turn atual, complementando hybrid retrieval.

---

## 4. ACT-R — Anderson

John Anderson publicou *The Atomic Components of Thought* (1998) e sucessores descrevendo ACT-R (Adaptive Control of Thought—Rational), uma arquitetura cognitiva com:

- **Declarative memory** (chunks) + **procedural memory** (production rules).
- Equação de activation: `A_i = B_i + Σ_j W_j * S_ji + ε` combinando base-level activation (history), spreading activation de contextos associados, e noise.
- Base-level: `B_i = ln(Σ_k t_k^(-d))` — soma de accesses com decay (conexão direta com Ebbinghaus).

### 4.1 Influência no Sovyx

Sovyx não implementa ACT-R literal, mas herda o **princípio de composição**:

- `ImportanceWeights` combina múltiplos sinais aditivos (cat + llm + emo + novelty + explicit), espelhando a equação ACT-R de soma ponderada.
- `access_count` + `last_accessed_at` em `Concept`/`Episode` são a base pra calcular base-level activation via decay.
- **Não implementado explicitamente**: a equação `B_i = ln(...)` — Sovyx usa importance acumulativa com decay aplicado por job, não cálculo por-query. `[NOT IMPLEMENTED]` em v0.6 exploratory backlog.

---

## 5. Hebbian learning — "neurons that fire together wire together"

Donald Hebb (*The Organization of Behavior*, 1949):

> "When an axon of cell A is near enough to excite cell B and repeatedly or persistently takes part in firing it, some growth process or metabolic change takes place in one or both cells such that A's efficiency, as one of the cells firing B, is increased."

Reduzido ao aforismo clássico. Base biofísica: LTP (long-term potentiation) hipocampal (Bliss & Lømo 1973).

### 5.1 Implementação Sovyx

`src/sovyx/brain/learning.py` (`HebbianLearning`):

- Quando dois concepts são co-ativados numa janela temporal (p.ex. mesma conversa), o edge entre eles é criado/reforçado.
- Threshold mínimo pra reforçar (evita thrashing).
- Complementa o decay: edges não reforçados decaem gradualmente.

Resultado: o grafo semântico do Mind **se auto-organiza** — relações frequentes ficam fortes, raras desaparecem.

---

## 6. Memória episódica vs semântica — Tulving 1972

Endel Tulving introduziu em "Episodic and semantic memory" (*Organization of Memory*, 1972) a distinção fundamental:

- **Episodic** — memórias específicas com contexto (quando/onde). "Meu aniversário de 20 anos."
- **Semantic** — conhecimento factual sem contexto pessoal. "Capital do Brasil é Brasília."

Tulving depois (1985) adicionou **procedural** e um modelo SPI (serial-parallel-independent) de interação entre os três.

### 6.1 Mapeamento direto no Sovyx

```
Episode (brain/models.py)    ↔  Episodic memory (Tulving)
Concept (brain/models.py)    ↔  Semantic memory
Tool-use patterns + OCEAN    ↔  Procedural memory (implícito)
```

Consolidation (em `brain/consolidation.py`) espelha o processo biológico **system consolidation** (Squire): episodes repetidos/similares viram concepts — semantic memory emerge de episodic memory.

---

## 7. PAD emotional model — Russell 1980 / Mehrabian 1996

James Russell (1980) propôs o **circumplex model of affect** — emoções num plano 2D de valence × arousal. Albert Mehrabian (1974, refinado em 1996) expandiu pra 3D adicionando dominance: **P**leasure, **A**rousal, **D**ominance.

- **Pleasure/Valence** (-1 to +1): agradável ↔ desagradável.
- **Arousal** (0 to 1): calmo ↔ agitado.
- **Dominance** (-1 to +1): submisso ↔ em controle.

PAD é amplamente usado em affective computing, HRI, e game AI justamente pela compactness + expressividade.

### 7.1 [DIVERGENCE] Código 2D vs ADR 3D

**`ADR-001-EMOTIONAL-MODEL` §2 Option D CHOSEN**: PAD 3D.

**Código atual** (`gap-analysis.md` §tabela brain + §top divergências #1): `Episode` guarda `valence + arousal` (2D); `Concept` guarda apenas `valence` (1D). Dominance **ausente**. `MindConfig` não tem emotional baseline config.

Impacto:

- Scoring emocional é parcial — perde-se a dimensão de agency/dominance que diferenciaria p.ex. "medo" (low valence, high arousal, low dominance) de "raiva" (low valence, high arousal, high dominance).
- Consolidation e personality drift não podem usar o eixo de dominance.

**Status**: schema migration planejada pra v0.6 (ver `docs/planning/roadmap.md`). Divergência explícita registrada em `docs/_meta/gap-analysis.md` §top divergências.

---

## 8. Working memory — Baddeley multi-component

Alan Baddeley (1974 com Hitch, refinado 2000) — **working memory** não é um slot único, mas composto de:

- **Central executive** — atenção e controle.
- **Phonological loop** — informação verbal/acústica (~2s de rehearsal).
- **Visuospatial sketchpad** — informação visual/espacial.
- **Episodic buffer** (adicionado 2000) — integra com long-term memory.

Capacidade: famoso "magical number seven ± two" (Miller 1956), revisado pra ~4 chunks (Cowan 2001).

### 8.1 Sovyx prefrontal cache

`brain/working_memory.py` implementa uma versão reduzida:

- Slots únicos (não diferencia phonological vs visuospatial — irrelevante pra assistente de texto/voz unimodal de momento).
- Capacidade default 7 (configurável), LRU displacement.
- Episodic buffer analog: quando um item sai do working memory, é persistido como `Episode` se marked-interesting.

Fiel ao espírito (cache de fast-access bounded) mas simplificado pra implementação prática.

---

## 9. Sovyx design — Concept / Episode / Relation

A abstração de três entidades surge como intersecção prática dos modelos acima:

```
Concept    ←→    Semantic memory (Tulving) + Neocortex (biológico)
Episode    ←→    Episodic memory (Tulving) + Hippocampus
Relation   ←→    Associative edges (Collins & Loftus) + Hebbian synapses
```

Relations carregam:

- `source_id`, `target_id`, `relation_type`.
- `weight` (reforçada por Hebbian, decai por Ebbinghaus).
- `metadata` (como foi criada — inference LLM, consolidation, explicit).

Esta triada é suficiente pra reconstruir todos os mecanismos cognitivos acima sem over-engineering. Documentado em `SPE-004-BRAIN-MEMORY` §modelo de dados.

---

## 10. Gaps cognitivos reconhecidos

Do `gap-analysis.md` §top 10 gaps críticos, dois são cognitivos:

- **Gap #8 — CONSOLIDATE orphaned**: `brain/consolidation.py` existe mas **não é chamada pelo CognitiveLoop**. Consolidation background existe standalone; falta integração. `[NOT IMPLEMENTED — v0.6]`.
- **Gap #9 — DREAM phase ausente**: o modelo canonical de 7 fases (`SPE-003` §1.1) inclui `DREAM` — fase nightly onde o sistema faz pattern discovery sobre episodes acumulados. Inexistente no código. `[NOT IMPLEMENTED — v0.6]`.

Inspiração biológica pro DREAM: sono REM em mamíferos está associado a replay hipocampal de episodes do dia + pattern extraction (Buzsáki 2006, *Rhythms of the Brain*). Sovyx DREAM replicaria isso — rodando durante janelas de idle do sistema — mas ainda é design, não código.

---

## 11. Referências

Acadêmicas primárias (inspiração):

- Baddeley, A. D., & Hitch, G. J. (1974). Working memory. *Psychology of Learning and Motivation*, 8.
- Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6).
- Ebbinghaus, H. (1885). *Über das Gedächtnis*.
- Hebb, D. O. (1949). *The Organization of Behavior*.
- Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6).
- Tulving, E. (1972). Episodic and semantic memory. *Organization of Memory*.
- Mehrabian, A. (1996). Pleasure-Arousal-Dominance: A general framework. *Current Psychology*, 14(4).

Docs-fonte internos:

- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-004-BRAIN-MEMORY.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md`
- `vps-brain-dump/memory/nodes/sovyx-60-seconds.md` ("5 regiões neuroscience-based")
- `vps-brain-dump/SOVYX-MISSION-AUDIT-V8.md` §issue #12 (consolidation merge strategy)
- `vps-brain-dump/SOVYX-MISSION-AUDIT-V8.md` §issue #6, #9 (v9)
- `docs/_meta/gap-analysis.md` §tabela brain + §top divergências

Código:

- `src/sovyx/brain/{models,embedding,learning,activation,working_memory,consolidation,scoring,retrieval}.py`

---

_Última revisão: 2026-04-14._
