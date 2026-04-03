# Architecture

## Overview

Sovyx is a cognitive engine built around a central loop that processes messages through multiple phases, using a persistent brain for memory and learning.

## Core Pipeline

```
Input → Bridge → Cognitive Loop → Brain → LLM → Response
```

### 1. Bridge Layer

The bridge receives messages from channels (Telegram, CLI) and routes them through identity resolution and conversation tracking.

- **PersonResolver**: Maps channel users to persistent Person records
- **ConversationTracker**: Manages conversation sessions with 30-min timeout
- **BridgeManager**: Orchestrates the full inbound pipeline

### 2. Cognitive Loop

Five phases process each message:

1. **Perceive**: Extract metadata, detect language, estimate complexity
2. **Attend**: Safety filtering, attention routing, priority scoring
3. **Think**: Context assembly → LLM call → response generation
4. **Act**: Tool execution (v0.5+ — framework ready)
5. **Reflect**: Extract concepts, create episodes, Hebbian learning

### 3. Brain

Persistent memory with three storage types:

- **Concepts**: Named knowledge nodes with embeddings (FTS5 + sqlite-vec)
- **Episodes**: Timestamped interaction records
- **Relations**: Weighted connections between concepts

Key algorithms:
- **Spreading Activation**: Multi-hop retrieval from working memory
- **Hebbian Learning**: "Neurons that fire together wire together"
- **Ebbinghaus Decay**: Forgetting curve with rehearsal reinforcement
- **Hybrid Retrieval**: RRF fusion of FTS5 text search + vector KNN

### 4. Context Assembly

Token-budget-aware context building with Lost-in-Middle ordering (Liu et al. 2023):
- Most relevant information at start and end of context
- Least relevant in the middle
- Adaptive allocation based on conversation length and complexity

### 5. LLM Router

Multi-provider with automatic failover:
- Anthropic (primary) → OpenAI (fallback) → Ollama (local)
- Circuit breaker per provider (3 failures → 60s cooldown)
- Cost guard with per-conversation and daily limits

## Data Flow

```
Telegram/CLI Message
       │
       ▼
PersonResolver.resolve()
       │
       ▼
ConversationTracker.get_or_create()
       │
       ▼
CogLoopGate.submit()  ← Priority queue, single worker
       │
       ▼
CognitiveLoop.process()
  ├── PerceivePhase
  ├── AttendPhase (safety check)
  ├── ThinkPhase
  │     ├── ContextAssembler (brain recall + history)
  │     └── LLMRouter.complete()
  ├── ActPhase (v0.5+)
  └── ReflectPhase
        ├── Extract concepts
        ├── Create episode
        └── Hebbian co-activation
       │
       ▼
ConversationTracker.add_turn()
       │
       ▼
Response → Channel
```

## Storage

All data in SQLite (one DB per mind):

- `~/.sovyx/system.db` — Global state
- `~/.sovyx/<mind>/brain.db` — Concepts, episodes, relations, embeddings
- `~/.sovyx/<mind>/conversations.db` — People, conversations, turns
