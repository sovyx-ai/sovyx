# Getting Started

Sovyx is a self-hosted AI companion engine with persistent memory. It runs as a
Python daemon on your own machine, stores its brain in SQLite, and talks to any
LLM provider you point it at (Anthropic, OpenAI, Google, or a local Ollama).

This guide gets you from zero to a running daemon and a working chat in about
five minutes.

## Requirements

| Requirement | Version / note |
|---|---|
| Python | 3.11 or newer (3.12 recommended) |
| SQLite | 3.35+ with FTS5 compiled in (default on modern distros) |
| RAM | 512 MB minimum (runs on Raspberry Pi 5) |
| Disk | ~200 MB for the engine and ONNX models |
| LLM access | One of: Anthropic, OpenAI, or Google API key, **or** a local Ollama install |

Sovyx runs on Linux, macOS, and Windows.

## Install

Using pip:

```bash
pip install sovyx
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install sovyx
```

Verify the install:

```bash
sovyx --version
```

## First Run

### 1. Initialize a mind

```bash
sovyx init Aria
```

This creates:

```
~/.sovyx/
├── system.yaml           # engine config (optional, has defaults)
├── logs/                 # daemon logs (JSON)
└── aria/
    └── mind.yaml         # mind config — personality, LLM, brain, channels
```

You can pass any name. `sovyx init MyMind` creates `~/.sovyx/mymind/mind.yaml`.

### 2. Set an API key

Pick one provider and export its key. Sovyx auto-detects which one is present
at start-up:

```bash
# Anthropic (default if present)
export ANTHROPIC_API_KEY=sk-ant-...

# or OpenAI
export OPENAI_API_KEY=sk-...

# or Google
export GOOGLE_API_KEY=...
```

No cloud key? Install [Ollama](https://ollama.ai) and pull a model:

```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.2:1b
```

Sovyx will detect Ollama on `http://localhost:11434` automatically.

### 3. Start the daemon

```bash
sovyx start
```

You should see:

```
Starting Sovyx daemon...
Sovyx daemon started
```

By default the dashboard binds to `http://127.0.0.1:7777`.

## Talk to It

### Via the dashboard

1. Open `http://localhost:7777` in your browser.
2. Get your auth token:

   ```bash
   sovyx token
   ```

3. Paste the token on the login page.

The dashboard has a chat page, a live brain graph, conversation history, logs,
and plugin management.

### Via Telegram

Add your bot token to `~/.sovyx/aria/mind.yaml`:

```yaml
channels:
  telegram:
    token_env: SOVYX_TELEGRAM_TOKEN
```

Set the environment variable and restart:

```bash
export SOVYX_TELEGRAM_TOKEN=123456:ABC-DEF...
sovyx stop
sovyx start
```

Then message your bot on Telegram.

## Check It's Healthy

```bash
sovyx status
sovyx doctor
```

`doctor` runs a tiered health check: disk, RAM, CPU, model files, and
configuration (offline), plus database, brain, LLM connectivity, channels, and
cost budget (online, requires the daemon). Use `sovyx doctor --json` for
machine-readable output.

## Common Commands

| Command | Does |
|---|---|
| `sovyx init [name]` | Create a new mind (default name: `Aria`) |
| `sovyx start` | Start the daemon and dashboard |
| `sovyx stop` | Stop the daemon |
| `sovyx status` | Show daemon status |
| `sovyx doctor` | Run health checks |
| `sovyx token` | Print the dashboard auth token |
| `sovyx logs --follow` | Follow the daemon log stream |
| `sovyx brain search "query"` | Search concepts in the brain |
| `sovyx brain stats` | Brain counts and growth |
| `sovyx mind list` | List active minds |
| `sovyx plugin list` | List installed plugins |

## Next Steps

- **[Configuration](configuration.md)** — everything you can set in
  `mind.yaml` and `system.yaml`, plus the `SOVYX_*` env vars.
- **[Architecture](architecture.md)** — how the cognitive loop, brain graph,
  and router fit together.
- **[LLM Router](llm-router.md)** — how Sovyx picks a model for each message.
- **[Plugin Developer Guide](plugin-developer-guide.md)** — write your own
  tools the LLM can call.
