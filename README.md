# Sovyx — The AI Mind That Remembers You

Sovyx is a self-hosted AI companion engine that builds persistent memory of the people it talks to. Unlike chatbots that forget everything between sessions, Sovyx creates a cognitive model of each person — their preferences, history, personality, and context — and uses it to have meaningful conversations.

Your data stays on your hardware. Always.

---

## What It Does

**Core Engine**
- Persistent memory — concepts, episodes, and relationships in a brain-inspired architecture
- Personality engine — OCEAN model with configurable traits
- Semantic search — FTS5 + sqlite-vec embeddings with hybrid retrieval
- Adaptive context — Lost-in-Middle ordering, token budgets, 6-slot assembly
- Multi-provider LLM — Claude, GPT, Gemini, Ollama — automatic failover and complexity routing
- Cost control — daily and per-conversation budgets with persistent tracking

**Dashboard** (v0.5)
- Real-time web UI — brain visualization, conversations, logs, settings, live chat
- Single command — `sovyx start` opens the dashboard at `http://localhost:7777`

**Voice** (v0.5)
- Full pipeline — wake word detection, VAD, STT, TTS (Silero, Moonshine, Piper, Kokoro)
- Home Assistant — Wyoming protocol integration for voice assistants

**Channels**
- Telegram, Signal, Dashboard (browser) — talk to your mind from anywhere

**Infrastructure** (v0.5)
- Cloud backup — zero-knowledge encrypted (Argon2id + AES-256-GCM)
- Zero-downtime upgrades — blue-green pipeline with automatic rollback
- Observability — Prometheus metrics, SLO monitoring, structured logging

---

## Quick Start

### Install

```bash
pip install sovyx
```

Or with Docker:

```bash
docker run -d --name sovyx \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -p 7777:7777 \
  ghcr.io/sovyx-ai/sovyx:latest
```

### Initialize

```bash
sovyx init MyMind
```

Creates `~/.sovyx/` with your mind configuration.

### Configure

Edit `~/.sovyx/mymind/mind.yaml`:

```yaml
name: MyMind
language: en
timezone: UTC

personality:
  tone: warm
  humor: 0.4
  empathy: 0.8

llm:
  default_provider: anthropic
  default_model: claude-sonnet-4-20250514
  budget_daily_usd: 2.0

channels:
  telegram:
    token_env: SOVYX_TELEGRAM_TOKEN
```

### Set API Keys

```bash
# Cloud provider (pick one)
export ANTHROPIC_API_KEY=sk-ant-...    # or OPENAI_API_KEY, GOOGLE_API_KEY
export SOVYX_TELEGRAM_TOKEN=123456:ABC...  # from @BotFather (optional)

# Or use Ollama (free, auto-detected):
# curl -fsSL https://ollama.ai/install.sh | sh && ollama pull llama3.1
```

### Start

```bash
sovyx start
```

Open `http://localhost:7777` to chat via the dashboard, or message your Telegram bot.

To get your dashboard token:

```bash
sovyx token
```

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Channels    │────▶│  Cognitive   │────▶│  LLM Router │
│  (Telegram,  │◀────│  Loop        │◀────│  (Claude,   │
│   Signal,    │     │  (OODA)      │     │   GPT,      │
│   Dashboard) │     │              │     │   Ollama)   │
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                    ┌──────▼───────┐
                    │    Brain     │
                    │  ┌─────────┐ │
                    │  │Concepts │ │  FTS5 + sqlite-vec
                    │  │Episodes │ │  Spreading activation
                    │  │Relations│ │  Hebbian learning
                    │  └─────────┘ │
                    └──────────────┘
```

**Cognitive Loop** (OODA): Perceive, Attend, Think, Act, Reflect.

Each message triggers the full loop: perception extracts intent, attention prioritizes, thinking generates a response via LLM with full context, action delivers, and reflection learns concepts from the exchange.

See [docs/architecture.md](docs/architecture.md) for the detailed data flow.

---

## Requirements

- Python 3.11+
- SQLite 3.35+ (with FTS5)
- 512MB RAM minimum (Raspberry Pi 5 compatible)
- LLM API key (Anthropic, OpenAI, or Google) **or** local Ollama (auto-detected)

---

## CLI

```bash
sovyx init [name]     # Initialize a new mind
sovyx start           # Start the daemon + dashboard
sovyx stop            # Stop the daemon
sovyx status          # Check daemon status
sovyx doctor          # Run health checks
sovyx token           # Show dashboard auth token
```

---

## Development

```bash
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx
uv sync --dev
uv run pytest                    # 4,396 tests
uv run ruff check src/ tests/   # Lint
uv run mypy src/sovyx --strict   # Type check
```

**Quality gates (CI-enforced):** 95%+ coverage per file, mypy strict, ruff, bandit. All tests pass in under 3 minutes.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development guide.

---

## Roadmap

| Version | Status | Key Features |
|---------|--------|-------------|
| v0.1 | Released | Core engine, brain, Telegram, CLI |
| v0.5 | Released | Voice pipeline, dashboard, Signal, cloud backup, zero-downtime upgrades |
| v0.5.1 | Released | Dashboard chat, security hardening, attack testing, CI/CD pipeline |
| v1.0 | Planned | Multi-tenant, JWT auth, plugin system, emotional engine, REST API |
| v1.1 | Planned | Multi-language voice, barge-in, conversation branching |
| v2.0 | Planned | Multi-agent platform, federated memory |

---

## License

AGPL-3.0 — See [LICENSE](LICENSE).

---

## Links

- [Repository](https://github.com/sovyx-ai/sovyx)
- [Documentation](docs/)
- [Dashboard Quickstart](docs/dashboard-quickstart.md)
- [API Reference](docs/api.md)
- [Architecture](docs/architecture.md)
- [PyPI](https://pypi.org/project/sovyx/)
