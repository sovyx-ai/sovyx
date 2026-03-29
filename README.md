<div align="center">

# Sovyx

### Sovereign Minds

**An open-source AI companion with cognitive architecture, persistent memory, emotional modeling, and voice interface.**

**Local-first. Runs on a Raspberry Pi 5. No cloud required.**

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-purple.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Pi%205%20%7C%20Linux%20%7C%20macOS-green.svg)]()

---

*Build a mind, not a bot.*

</div>

## What is Sovyx?

Sovyx is an AI companion that **lives on your hardware**. It thinks, remembers, feels, speaks, and learns — all locally. No cloud accounts, no API keys required for core functionality, no data leaving your machine.

Unlike chatbots that forget you every session, Sovyx has:

- 🧠 **Cognitive Architecture** — A thinking loop (CogLoop) that processes perception → context → reasoning → action, not just prompt → response
- 💾 **Persistent Memory** — Spreading activation network with episodic + semantic + procedural memory. It remembers your birthday, your preferences, and what you talked about last Tuesday
- 💭 **Emotional Modeling** — PAD (Pleasure-Arousal-Dominance) emotional state + OCEAN personality traits that evolve over time
- 🎤 **Voice Interface** — Full STT/TTS pipeline with wake word, barge-in detection, and natural conversation flow
- 🏠 **Home Automation** — Native Home Assistant integration. Your AI companion controls your house
- 🔌 **Plugin Ecosystem** — Extend with community plugins. Calendar, crypto, weather, smart home — anything
- 🛡️ **Sovereign by Design** — AGPL-3.0. Your data stays yours. Forever

## Philosophy

```
The question isn't "is it sentient?"

The question is: who controls it?

If it lives on someone else's server,
answers to someone else's rules,
and can be deleted by someone else's decision —

it's not yours. It's theirs.
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Sovyx Daemon                      │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Engine   │  │  Brain   │  │  Communication   │  │
│  │  Core     │──│  Memory  │──│  Bridge          │  │
│  │          │  │  System   │  │                  │  │
│  └────┬─────┘  └──────────┘  └────────┬─────────┘  │
│       │                               │              │
│  ┌────▼─────┐  ┌──────────┐  ┌───────▼──────────┐  │
│  │ CogLoop  │  │ Emotional│  │    Channels       │  │
│  │ Think    │──│ Engine   │  │ Voice│Telegram│CLI │  │
│  │ Loop     │  │ PAD+OCEAN│  │      │        │   │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Voice   │  │  Plugin  │  │    Proactive      │  │
│  │ Pipeline │  │  System  │  │    Engine         │  │
│  │ STT/TTS  │  │  Sandbox │  │    (initiates)    │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│                                                      │
├─────────────────────────────────────────────────────┤
│  SQLite + SQLCipher │ Event Bus │ Config (YAML)     │
└─────────────────────────────────────────────────────┘
```

## Status

> ⚠️ **Pre-alpha.** Sovyx is under active development. The architecture is designed, the specs are written, and implementation is underway. Star to follow progress.

**v0.1 "First Breath"** — *Target: Q3 2026*
- [ ] Engine Core + Mind definition
- [ ] Brain & persistent memory (SQLite + spreading activation)
- [ ] CogLoop (cognitive processing loop)
- [ ] Telegram channel
- [ ] CLI interface
- [ ] Voice pipeline (STT + TTS)
- [ ] Plugin system (SDK + sandbox)
- [ ] Dashboard

## Hardware Requirements

| Tier | Hardware | RAM | Experience |
|------|----------|-----|------------|
| 🥧 **Pi 5** | Raspberry Pi 5 | 8GB | Full local (recommended) |
| 💻 **Desktop** | Any x86_64/ARM64 | 16GB+ | Full local + larger models |
| ☁️ **Cloud** | VPS/dedicated | 4GB+ | Remote companion |

Sovyx is designed to run on a **Raspberry Pi 5** as the baseline. If it runs well on a Pi, it runs well everywhere.

## Quick Start

> Coming soon with v0.1 release.

```bash
# Clone
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx

# Install
pip install -e ".[dev]"

# Configure your mind
cp mind.example.yaml mind.yaml
# Edit mind.yaml — name, personality, voice, integrations

# Start
sovyx start
```

## The Mind File

Every Sovyx companion starts with a `mind.yaml` — a soul file that defines who your AI is:

```yaml
mind:
  name: "Atlas"
  personality:
    openness: 0.8          # Curious, creative
    conscientiousness: 0.7  # Organized, reliable
    extraversion: 0.4       # More introverted, thoughtful
    agreeableness: 0.6      # Kind but honest
    neuroticism: 0.3        # Emotionally stable

  voice:
    engine: "piper"
    model: "en_US-amy-medium"
    wake_word: "hey atlas"

  memory:
    backend: "sqlite"
    encryption: true        # SQLCipher

  channels:
    - telegram
    - voice
    - cli
```

## Contributing

Sovyx is open-source and we welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Areas where we need help:**
- 🧠 Cognitive architecture research
- 🎤 Voice pipeline optimization (Pi 5)
- 🔌 Plugin development
- 🌍 Internationalization
- 📖 Documentation

## License

[GNU Affero General Public License v3.0](LICENSE)

Your mind, your hardware, your rules. Sovyx is free software — free as in freedom.

Commercial plugin exception available for proprietary integrations.

---

<div align="center">

**Sovyx** is built by [Guipe](https://github.com/byguipe) at [Machinimus](https://machinimus.com).

*The future of AI is sovereign.*

</div>
