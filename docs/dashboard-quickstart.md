# Dashboard Quickstart Guide

> Get from zero to chatting with your mind in 2 minutes.

## Requirements

- Python ≥ 3.11
- An LLM API key (OpenAI, Anthropic, Google, or compatible)
- A modern browser (Chrome, Firefox, Safari, Edge)

## Install

```bash
pip install sovyx
```

## Initialize

```bash
sovyx init
```

This creates `~/.sovyx/` with default configuration.

## Configure LLM

Edit `~/.sovyx/system.yaml`:

```yaml
llm:
  provider: openai        # or: anthropic, google, ollama
  api_key: sk-...         # your API key
  model: gpt-4o-mini      # or: claude-3-haiku, gemini-1.5-flash
```

## Start

```bash
sovyx start -f
```

You'll see a startup banner:

```
╔══════════════════════════════════════════════╗
║           🔮 Sovyx — Mind Engine             ║
╠══════════════════════════════════════════════╣
║  Dashboard:  http://127.0.0.1:7777          ║
║  Token:      abc123...                       ║
╠══════════════════════════════════════════════╣
║  Paste the token in the dashboard login.     ║
║  Or run: sovyx token                         ║
╚══════════════════════════════════════════════╝
```

## Open Dashboard

1. Open **http://127.0.0.1:7777** in your browser
2. Paste the token from the startup banner (or run `sovyx token`)
3. Click **Connect**

## Chat

1. Click **Chat** in the sidebar
2. Type a message and hit Enter
3. Your mind responds — learning from every interaction

## CLI Reference

| Command | Description |
|---------|-------------|
| `sovyx init [name]` | Initialize Sovyx (default mind: Aria) |
| `sovyx start -f` | Start daemon in foreground |
| `sovyx stop` | Stop daemon |
| `sovyx status` | Show daemon status |
| `sovyx token` | Show dashboard auth token |
| `sovyx token --copy` | Copy token to clipboard |
| `sovyx dashboard` | Show dashboard URL info |
| `sovyx doctor` | Run health checks |

## Channels

Sovyx supports multiple communication channels:

| Channel | Status | How to connect |
|---------|--------|----------------|
| **Dashboard** | Built-in | Always available via browser |
| **Telegram** | Optional | Configure bot token in `system.yaml` |
| **Signal** | Optional | Configure signal-cli in `system.yaml` |

Check channel status in the Dashboard overview or via `GET /api/channels`.

## API

The dashboard exposes a REST API at `http://127.0.0.1:7777`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Engine status |
| `/api/chat` | POST | Send chat message |
| `/api/channels` | GET | Channel status |
| `/api/conversations` | GET | Conversation list |
| `/api/brain/graph` | GET | Brain knowledge graph |
| `/api/health` | GET | Health checks |
| `/api/settings` | GET | Current settings |
| `/api/config` | GET/PUT | Mind configuration |

All endpoints require `Authorization: Bearer <token>` header.

## Troubleshooting

**Token not generated?**
Start the daemon first: `sovyx start -f`

**Dashboard not loading?**
Check that port 7777 is not in use: `lsof -i :7777`

**LLM not responding?**
Run `sovyx doctor` to check API key and connectivity.

---

*Sovyx v0.5 — Sovereign Minds Engine*
