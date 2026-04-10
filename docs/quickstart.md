# Quick Start

## Prerequisites

- Python 3.11+
- At least one LLM provider API key (Anthropic, OpenAI, or local Ollama)

## Install

```bash
# Recommended
uv tool install sovyx

# Alternative
pip install sovyx
```

## Initialize

```bash
sovyx init Aria
```

This creates:
- `~/.sovyx/system.yaml` — Global configuration
- `~/.sovyx/aria/mind.yaml` — Mind personality and settings

## Configure LLM

Set at least one provider:

```bash
# Anthropic (recommended)
export ANTHROPIC_API_KEY="sk-ant-..."

# OpenAI
export OPENAI_API_KEY="sk-..."

# Google
export GOOGLE_API_KEY="AIza..."
```

### Local LLM (Ollama) — free, no API key

```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.1
```

Sovyx auto-detects Ollama on startup and configures it as your provider.
You can change the model in **Dashboard → Settings → Providers**.

## Start

```bash
sovyx start
```

## Connect Telegram (optional)

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Set the token:
   ```bash
   export SOVYX_TELEGRAM_TOKEN="123456:ABC..."
   ```
3. Restart: `sovyx stop && sovyx start`
4. Message your bot — it will respond with full cognitive processing

## Verify

```bash
sovyx status   # Check daemon is running
sovyx doctor   # Run health checks
```

## Next Steps

- Edit `~/.sovyx/aria/mind.yaml` to customize personality
- See [Configuration](configuration.md) for all options
- See [Architecture](architecture.md) for how it works
