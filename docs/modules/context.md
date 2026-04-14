# Módulo: context

## Objetivo

`sovyx.context` assembla o contexto final enviado ao LLM em toda chamada
Think. A premissa da SPE-006 — "the quality of the context directly
determines the quality of the response" — é implementada via 6 slots
priorizados, alocação adaptativa de tokens, e ordenação Lost-in-Middle
(Liu et al. 2023) para mitigar o viés de esquecimento dos LLMs no meio
da janela. O módulo é pequeno (5 arquivos, 759 LOC) e essencialmente
sem divergências com a spec.

## Responsabilidades

- Receber uma mensagem corrente + histórico + mind_id e produzir
  `AssembledContext` (messages prontas para OpenAI/Anthropic/Google
  chat APIs).
- Chamar `BrainService.recall()` para buscar concepts e episodes
  relevantes.
- Alocar tokens entre 6 slots adaptativamente baseado em
  conversation_length, brain_result_count, complexity, context_window e
  mean_confidence dos concepts.
- Formatar cada slot com `ContextFormatter` respeitando Lost-in-Middle.
- Contar tokens via `TokenCounter` (tiktoken quando disponível, fallback
  heurístico chars/4).
- Trimar histórico (oldest first) quando orçamento estoura, preservando
  system prompt, temporal e mensagem atual (NEVER cut).
- Retornar breakdown por slot para observability e debug.

## Arquitetura

Os 6 slots (em ordem de renderização):

1. **SYSTEM PROMPT** — personality + rules. NEVER cut.
2. **TEMPORAL** — data/hora no timezone do mind. NEVER cut (~50 tokens).
3. **MEMORY (concepts)** — relevantes do Brain. Cuttable.
4. **MEMORY (episodes)** — recentes relevantes. Cuttable.
5. **CONVERSATION** — histórico de mensagens. Cuttable oldest-first.
6. **CURRENT MESSAGE** — user input. NEVER cut.

Proporções base v0.1 (somam 100%): system=15%, concepts=20%,
episodes=13%, temporal=2%, conversation=37%, response_reserve=13%.

Regras adaptativas em `TokenBudgetManager.allocate`:

- conversation > 15 turns → +8% conversation, −5% concepts, −3% episodes.
- conversation < 3 turns → −10% conversation, +6% concepts, +4% episodes.
- complexity > 0.7 → +3% response_reserve, −3% conversation.
- brain_result_count > 20 → +5% concepts, −5% conversation.
- mean_confidence > 0.7 → +5% concepts, −5% conversation.
- mean_confidence < 0.3 → −5% concepts, +5% conversation.

Ordem crítica (aplicada em `ThinkPhase`): primeiro escolhe o modelo
(precisa só de complexity da perception), depois obtém `context_window`
daquele modelo, só então chama `ContextAssembler.assemble` com o
`context_window` real.

## Código real

```python
# src/sovyx/context/assembler.py:28-49 — estrutura e 6 slots
@dataclasses.dataclass
class AssembledContext:
    messages: list[dict[str, str]]
    tokens_used: int
    token_budget: int
    sources: list[str]
    budget_breakdown: dict[str, int]


class ContextAssembler:
    """Assemble complete context with 6 slots (SPE-006).
    1. SYSTEM PROMPT  2. TEMPORAL  3. MEMORY (concepts)
    4. MEMORY (episodes)  5. CONVERSATION  6. CURRENT MESSAGE
    """
```

```python
# src/sovyx/context/budget.py:86-92 — proporções base
p_system = 0.15
p_concepts = 0.20
p_episodes = 0.13
p_temporal = 0.02
p_conversation = 0.37
p_response = 0.13
```

```python
# src/sovyx/context/assembler.py:115-130 — recall + mean_confidence
brain_results = await self._brain.recall(current_message, mind_id)
concepts, episodes = brain_results
mean_conf = 0.5
if concepts:
    mean_conf = sum(c.confidence for c, _ in concepts) / len(concepts)
budget = self._budget.allocate(
    conversation_length=len(conversation_history),
    brain_result_count=len(concepts),
    complexity=complexity,
    context_window=context_window,
    mean_confidence=mean_conf,
)
```

```python
# src/sovyx/context/assembler.py:164-172 — overflow guard
max_usable = budget.total - budget.response_reserve
if tokens_used > max_usable and len(trimmed) > 0:
    while tokens_used > max_usable and trimmed:
        trimmed = trimmed[1:]  # drop oldest
        messages = [{"role": "system", "content": system_content}]
        messages.extend(trimmed)
        messages.append({"role": "user", "content": current_message})
        tokens_used = self._counter.count_messages(messages)
```

## Specs-fonte

- `SOVYX-BKD-SPE-006-CONTEXT-ASSEMBLY.md` — 6 slots, token budget,
  adaptação, Lost-in-Middle.
- `SOVYX-BKD-IMPL-003-CONTEXT-ASSEMBLY.md` — implementação detalhada.
- Research: Liu et al. 2023 "Lost in the Middle: How Language Models Use
  Long Contexts".

## Status de implementação

### ✅ Implementado

- `ContextAssembler.assemble()` (`assembler.py`, 237 LOC) com 6 slots e
  as regras acima, tracing (`context.assemble` span) e métrica de latência
  (`context_assembly_latency`).
- `AssembledContext` dataclass com `messages`, `tokens_used`,
  `token_budget`, `sources` (string list para observability),
  `budget_breakdown` (dict por slot).
- `TokenBudgetManager.allocate()` (`budget.py`, 181 LOC) com todas as
  regras adaptativas; normalização de overflow reduz flex slots
  (concepts → episodes → conversation) proporcionalmente quando os
  mínimos absolutos somam mais que `context_window`.
- Mínimos não-negociáveis: `MIN_SYSTEM_PROMPT=200`, `MIN_CONVERSATION=500`,
  `MIN_RESPONSE=256`, `MIN_TEMPORAL=50`, `MIN_CONTEXT_WINDOW=2048`.
- `ContextFormatter` (`formatter.py`, 222 LOC): `format_temporal()` com
  timezone do mind, `format_concepts_block()` e `format_episodes_block()`
  com budget por slot (trunca quando necessário).
- `TokenCounter` (`tokenizer.py`, 118 LOC): tiktoken preferido, fallback
  heurístico chars/4. `count_messages()` soma overheads (role tokens).
- Imutabilidade do `conversation_history`: `_trim_history()` retorna
  nova lista, nunca muta a original (v12 fix).

### Sem gaps

- 0 gaps significativos. Módulo é bem alinhado com spec.

## Divergências [DIVERGENCE]

- Nenhuma divergência documentada.

## Dependências

- **Externas**: `tiktoken` (opcional, fallback heurístico).
- **Internas**: `sovyx.brain.service.BrainService` (para `recall`),
  `sovyx.mind.personality.PersonalityEngine` (gera system prompt),
  `sovyx.mind.config.MindConfig` (timezone), `sovyx.engine.errors`,
  `sovyx.observability.{logging,metrics,tracing}`.

## Testes

- `tests/unit/context/test_assembler.py` — assembly end-to-end com
  mocks de brain/personality; overflow trimming.
- `tests/unit/context/test_budget.py` — todas as 6 regras adaptativas,
  caso de overflow quando context_window pequeno, validação de floor.
- `tests/unit/context/test_formatter.py` — Lost-in-Middle ordering,
  temporal format, truncation.
- `tests/unit/context/test_tokenizer.py` — tiktoken path + fallback.
- `tests/integration/test_think_context.py` — integração com ThinkPhase.

## Referências

- Code: `src/sovyx/context/assembler.py`, `src/sovyx/context/budget.py`,
  `src/sovyx/context/formatter.py`, `src/sovyx/context/tokenizer.py`.
- Specs: `SOVYX-BKD-SPE-006-CONTEXT-ASSEMBLY.md`,
  `SOVYX-BKD-IMPL-003-CONTEXT-ASSEMBLY.md`.
- Research: Liu et al. 2023 "Lost in the Middle".
- Gap analysis: `docs/_meta/gap-inputs/analysis-A-core.md` §context.
