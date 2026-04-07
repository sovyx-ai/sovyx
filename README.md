# 🧠 Sovyx — The AI Mind That Remembers You

**Sovyx** is a self-hosted AI companion engine that builds genuine, persistent memory of the people it talks to. Unlike chatbots that forget everything between sessions, Sovyx creates a cognitive model of each person — their preferences, history, personality, and context — and uses it to have meaningful conversations.

## ✨ Features

### Core
- **Persistent Memory** — Concepts, episodes, and relationships stored in a brain-inspired architecture
- **Personality Engine** — OCEAN model + configurable traits define how your Mind communicates
- **Semantic Search** — FTS5 + sqlite-vec embedding for intelligent recall
- **Adaptive Context** — Lost-in-Middle ordering, adaptive token budgets, 6-slot context assembly
- **Multi-Provider LLM** — Anthropic Claude, OpenAI GPT, Google Gemini, local Ollama — with automatic failover and complexity-based routing
- **Cost Control** — Daily and per-conversation budgets with persistent tracking
- **Self-Hosted** — Your data stays on your hardware. Always.

### v0.5 "First Words" — NEW
- **🎙️ Voice Pipeline** — Wake word detection, VAD, local STT/TTS (Silero, Moonshine, Piper, Kokoro), Home Assistant integration via Wyoming
- **📊 Dashboard** — Real-time web UI with brain visualization, conversations, logs, settings, and system status
- **☁️ Cloud Backup** — Zero-knowledge encrypted backups (Argon2id + AES-256-GCM), Stripe billing, usage metering
- **📡 Signal Integration** — Talk to your Mind via Signal (via signal-cli-rest-api)
- **📈 Observability** — SLO monitoring, Prometheus /metrics, alerting, cost tracking
- **🔄 Zero-Downtime Upgrades** — Blue-green upgrade pipeline with automatic rollback
- **🏋️ Benchmarks** — Performance budgets per hardware tier (Pi5, N100, GPU), baseline regression detection
- **🔌 Telegram + Signal** — Multi-channel messaging

## 🚀 Quick Start

### 1. Install

```bash
pip install sovyx
```

Or with Docker:

```bash
docker run -d --name sovyx \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e SOVYX_TELEGRAM_TOKEN=123456:ABC... \
  ghcr.io/sovyx-ai/sovyx:latest
```

### 2. Initialize

```bash
sovyx init MyMind
```

This creates `~/.sovyx/` with your Mind configuration.

### 3. Configure

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

### 4. Set API Keys

```bash
export ANTHROPIC_API_KEY=sk-ant-...       # or OPENAI_API_KEY
export SOVYX_TELEGRAM_TOKEN=123456:ABC...  # from @BotFather
```

### 5. Start

```bash
sovyx start
```

Now message your bot on Telegram. Sovyx will remember you.

## 🏗️ Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Telegram    │────▶│  Cognitive   │────▶│  LLM Router │
│  Bridge      │◀────│  Loop        │◀────│  (Claude/   │
│              │     │  (OODA)      │     │   GPT/Local)│
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

**Cognitive Loop** (OODA): Perceive → Attend → Think → Act → Reflect

Each message triggers the full loop: perception extracts intent, attention prioritizes, thinking generates response via LLM with full context, action delivers, and reflection learns concepts from the exchange.

## 📋 Requirements

- Python 3.11+
- SQLite 3.35+ (with FTS5)
- 512MB RAM minimum (Pi 5 compatible)
- LLM API key (Anthropic or OpenAI) or local Ollama

## 🔧 CLI Commands

```bash
sovyx init [name]     # Initialize Mind
sovyx start           # Start daemon
sovyx stop            # Stop daemon
sovyx status          # Check status
sovyx doctor          # Health check (v0.5)
```

## 🧪 Development

```bash
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx
uv sync --dev
uv run pytest                    # 1233 tests
uv run ruff check src/ tests/   # Lint
uv run mypy src/sovyx --strict   # Type check
```

**Quality:** 96% coverage, mypy strict, ruff, bandit — enforced in CI.

## 🗺️ Roadmap

| Version | Codename | Key Features |
|---------|----------|-------------|
| **v0.1** | First Breath | Core engine, brain, Telegram, CLI |
| **v0.5** | First Words | Voice pipeline, dashboard, Signal, cloud backup |
| **v1.0** | The Mind That Remembers | Emotional engine, plugins, Home Assistant, REST API |
| v1.1 | The Mind That Speaks | Multi-language voice, barge-in |
| v1.2 | The Mind That Acts | Financial intelligence, autonomy |
| v1.3 | The Mind In Your Pocket | Flutter mobile app |
| v2.0 | The Mind That Grows | Multi-agent platform |

## 📄 License

AGPL-3.0 — See [LICENSE](LICENSE) for details.

## 🔗 Links

- **GitHub:** [github.com/sovyx-ai/sovyx](https://github.com/sovyx-ai/sovyx)
- **Docs:** Coming soon
- **Discord:** Coming soon
