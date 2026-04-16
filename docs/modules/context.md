# Module: context

## What it does

`sovyx.context` assembles the final message list sent to the LLM on every call. It fills six slots (system prompt, temporal, memory concepts, memory episodes, conversation history, current message), allocates a token budget per slot, and orders content using the Lost-in-Middle pattern so the most relevant information sits at the edges of the context window.

## Key classes

| Name | Responsibility |
|---|---|
| `ContextAssembler` | Orchestrator — retrieval, slot allocation, trimming, formatting. |
| `AssembledContext` | Output dataclass — messages, tokens_used, budget_breakdown, sources. |
| `TokenBudgetManager` | Adaptive per-slot allocation (6 rules). |
| `TokenCounter` | Token estimation — tiktoken when available, chars/4 heuristic fallback. |
| `ContextFormatter` | Renders concepts/episodes/temporal into LLM-ready text. Lost-in-Middle ordering. |

## Six slots

Assembled in this order (never reordered — LLM attention is strongest at start and end):

| # | Slot | Cuttable? | Base % | Content |
|---|---|---|---|---|
| 1 | System prompt | Never | 15% | Personality + rules + safety guardrails |
| 2 | Temporal | Never | 2% | Date/time in the mind's timezone |
| 3 | Memory (concepts) | Yes | 20% | Relevant concepts from brain recall |
| 4 | Memory (episodes) | Yes | 13% | Recent episodes from brain recall |
| 5 | Conversation | Yes (oldest first) | 37% | Message history |
| 6 | Current message | Never | — | User's input (response reserve: 13%) |

## Adaptive budget

`TokenBudgetManager` adjusts slot allocations based on 6 runtime signals:

| Signal | Effect |
|---|---|
| Long conversation (>10 turns) | Conversation slot gets more budget |
| Short conversation (<3 turns) | Memory slots get more budget |
| High complexity (>0.7) | Response reserve increases |
| Many brain results (>15) | Memory concept slot grows |
| High mean confidence (>0.8) | Memory slots shrink (fewer, better results) |
| Small context window (<8K) | System prompt and temporal get minimum floors |

Minimum floors prevent any slot from disappearing: system prompt ≥200 tokens, conversation ≥500 tokens, response reserve ≥256 tokens, temporal ≥50 tokens, context window ≥2048 tokens.

## Lost-in-Middle ordering

Research shows LLMs attend most to the start and end of their context. The formatter places the highest-scoring concepts at the top and bottom, with lower-scoring ones in the middle.

## Configuration

No dedicated config — `ContextAssembler` reads `MindConfig` (timezone, personality) and `context_window` from the LLM router. Budget proportions are hardcoded (the adaptive rules cover all practical scenarios).

## Roadmap

- Expose token budget breakdown in dashboard for debugging.
- Configurable base proportions via `MindConfig.context`.

## See also

- Source: `src/sovyx/context/assembler.py`, `src/sovyx/context/formatter.py`, `src/sovyx/context/tokenizer.py`, `src/sovyx/context/budget.py`.
- Tests: `tests/unit/context/`.
- Related: [`cognitive`](./cognitive.md) (`ThinkPhase` is the only caller), [`brain`](./brain.md) (recall provides memory slots), [`mind`](./mind.md) (personality for system prompt).
