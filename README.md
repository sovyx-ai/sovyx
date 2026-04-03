<p align="center">
  <h1 align="center">🔮 Sovyx</h1>
  <p align="center"><strong>Sovereign Minds Engine</strong></p>
  <p align="center">Build AI minds that remember, learn, and evolve — on your own infrastructure.</p>
</p>

<p align="center">
  <a href="https://github.com/sovyx-ai/sovyx/actions"><img src="https://github.com/sovyx-ai/sovyx/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/sovyx-ai/sovyx/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License"></a>
  <a href="https://pypi.org/project/sovyx/"><img src="https://img.shields.io/pypi/v/sovyx" alt="PyPI"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python"></a>
</p>

---

## What is Sovyx?

Sovyx is a cognitive engine for building AI minds with persistent memory, personality, and learning capabilities. Each mind runs locally on your hardware — no cloud dependency, no data leaving your machine.

**Key difference:** Most AI frameworks are stateless wrappers around LLM APIs. Sovyx gives your AI a brain that remembers conversations, learns from interactions, and develops understanding over time.

## Features

- 🧠 **Persistent Brain** — Concepts, episodes, and relations stored in SQLite with vector embeddings
- 🔄 **Cognitive Loop** — Perception → Attention → Thinking → Action → Reflection pipeline
- 💡 **Working Memory** — Activation-based with spreading activation and decay
- 📚 **Hebbian Learning** — Connections strengthen between co-occurring concepts
- 🎭 **Personality** — OCEAN model shapes communication style
- 🔌 **Multi-Provider LLM** — Anthropic, OpenAI, Ollama with automatic failover
- 💬 **Telegram Integration** — Connect your mind to Telegram with one token
- 🛡️ **Graceful Degradation** — Every component has a fallback chain
- 📊 **Observable** — Structured logging, health checks, performance metrics
- 🔒 **Sovereign** — AGPL-3.0, runs on your hardware, your data stays yours

## Quick Start

### Install

```bash
# Via uv (recommended)
uv tool install sovyx

# Via pip
pip install sovyx

# Via Docker
docker pull ghcr.io/sovyx-ai/sovyx:0.1.0
```

### Initialize

```bash
sovyx init Aria
```

This creates `~/.sovyx/` with your mind configuration.

### Configure

Set your LLM provider (at least one required):

```bash
export SOVYX_ANTHROPIC_API_KEY="sk-ant-..."
# or
export SOVYX_OPENAI_API_KEY="sk-..."
# or run Ollama locally (no key needed)
```

Optional — connect Telegram:

```bash
export SOVYX_TELEGRAM_TOKEN="123456:ABC..."
```

### Start

```bash
sovyx start
```

### Check Status

```bash
sovyx status
sovyx doctor
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Sovyx Engine                       │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Telegram  │  │   CLI    │  │  Future Channels │  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       └──────────────┼─────────────────┘             │
│                      ▼                               │
│              ┌───────────────┐                       │
│              │ Bridge Manager│                       │
│              └───────┬───────┘                       │
│                      ▼                               │
│              ┌───────────────┐                       │
│              │ Cognitive Loop│                       │
│              │  Perceive     │                       │
│              │  Attend       │                       │
│              │  Think ──────►│── LLM Router         │
│              │  Act          │     ├─ Anthropic      │
│              │  Reflect      │     ├─ OpenAI         │
│              └───────┬───────┘     └─ Ollama         │
│                      ▼                               │
│              ┌───────────────┐                       │
│              │     Brain     │                       │
│              │  Concepts     │                       │
│              │  Episodes     │                       │
│              │  Relations    │                       │
│              │  Embeddings   │── E5-small-v2 (ONNX)  │
│              └───────┬───────┘                       │
│                      ▼                               │
│              ┌───────────────┐                       │
│              │    SQLite     │── sqlite-vec           │
│              └───────────────┘                       │
└──────────────────────────────────────────────────────┘
```

## Mind Configuration

Each mind has a `mind.yaml`:

```yaml
name: Aria
language: en
personality:
  openness: 0.7
  conscientiousness: 0.8
  extraversion: 0.5
  agreeableness: 0.7
  neuroticism: 0.3
brain:
  consolidation_interval_hours: 6
llm:
  default_model: claude-sonnet-4-20250514
  fast_model: claude-3-5-haiku-20241022
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `sovyx init [name]` | Initialize Sovyx with a mind |
| `sovyx start` | Start the daemon |
| `sovyx stop` | Graceful shutdown |
| `sovyx status` | Show daemon status |
| `sovyx doctor` | Run health checks |
| `sovyx brain search <query>` | Search concepts |
| `sovyx brain stats` | Brain statistics |
| `sovyx mind list` | List active minds |
| `sovyx mind status [name]` | Mind details |

## Docker

```bash
docker compose up -d
```

Or build from source:

```bash
docker build -t sovyx .
docker run -v sovyx-data:/data -e SOVYX_ANTHROPIC_API_KEY=sk-... sovyx
```

## Development

```bash
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx
uv sync --dev
uv run pytest
uv run mypy src/
uv run ruff check src/
```

## Performance

| Metric | Value |
|--------|-------|
| Cold start | 142ms |
| RSS idle | 41.6MB |
| Token counting | 269µs/call |
| Budget allocation | 3.3µs/call |
| Working memory (1K items) | 0.9ms |

## License

[AGPL-3.0](LICENSE) — Your freedom is non-negotiable.

Built with 🔮 by [Sovyx AI](https://github.com/sovyx-ai)
