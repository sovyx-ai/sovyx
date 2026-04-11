# Sovyx

## The AI Mind That Remembers You

Sovyx is a self-hosted AI companion engine that builds genuine, persistent memory of the people it talks to. Unlike chatbots that forget everything between sessions, Sovyx creates a cognitive model of each person — their preferences, history, personality, and context — and uses it to have meaningful conversations.

## Features

- **Persistent Memory** — Concepts, episodes, and relationships stored in a brain-inspired architecture
- **Personality Engine** — OCEAN model + configurable traits define how your Mind communicates
- **Semantic Search** — FTS5 + sqlite-vec embedding for intelligent recall
- **Adaptive Context** — Lost-in-Middle ordering, adaptive token budgets, 6-slot context assembly
- **Multi-Provider LLM** — Anthropic Claude, OpenAI GPT, Google Gemini, local Ollama — with automatic failover
- **Cost Control** — Daily and per-conversation budgets with persistent tracking
- **Dashboard** — Real-time web UI for brain visualization, plugin management, logs, and settings
- **Plugin System** — SDK, CLI, built-in plugins, ReAct loop, hot-reload, dashboard management
- **Voice Pipeline** — Wake word, STT, TTS with hardware-adaptive model selection
- **Telegram & Signal** — Multi-channel bridge with per-channel identity resolution
- **Self-Hosted** — Your data stays on your hardware. Always.

## Quick Install

```bash
pip install sovyx
sovyx init MyMind
sovyx start
```

See the [Quick Start](quickstart.md) guide for full setup instructions.

## Architecture at a Glance

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Telegram    │────▶│  Cognitive   │────▶│  LLM Router │
│  Bridge      │◀────│  Loop        │◀────│  (Claude/   │
│              │     │  (OODA)      │     │   GPT/Local)│
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                    ┌──────▼───────┐
                    │    Brain     │
                    │  Concepts    │  FTS5 + sqlite-vec
                    │  Episodes    │  Spreading activation
                    │  Relations   │  Hebbian learning
                    └──────────────┘
```

**Cognitive Loop** (OODA): Perceive → Attend → Think → Act → Reflect

Learn more in [Architecture](architecture.md).

## Requirements

- Python 3.11+
- SQLite 3.35+ (with FTS5)
- 512MB RAM minimum (Raspberry Pi 5 compatible)
- LLM API key (Anthropic, OpenAI, or Google) or local Ollama

## License

AGPL-3.0 — See [LICENSE](https://github.com/sovyx-ai/sovyx/blob/main/LICENSE) for details.
