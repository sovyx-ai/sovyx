# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-04-03

### Added
- **Cognitive Loop**: Full perception → attention → thinking → action → reflection pipeline
- **Brain System**: Concept/episode/relation storage with SQLite + sqlite-vec embeddings
- **Working Memory**: Activation-based with geometric decay
- **Spreading Activation**: Multi-hop concept retrieval
- **Hebbian Learning**: Strengthen connections between co-occurring concepts
- **Ebbinghaus Decay**: Forgetting curve with rehearsal factor
- **Hybrid Retrieval**: RRF fusion of FTS5 + vector search
- **Memory Consolidation**: Scheduled decay + pruning cycles
- **Personality Engine**: OCEAN model with 3-level descriptors
- **Context Assembly**: Token-budget-aware with Lost-in-Middle ordering (Liu et al. 2023)
- **LLM Router**: Multi-provider failover (Anthropic, OpenAI, Ollama) with circuit breaker
- **Cost Guard**: Per-conversation and daily budget limits
- **Telegram Channel**: aiogram 3.x with exponential backoff reconnect
- **Person Resolver**: Auto-create identity on first contact
- **Conversation Tracker**: 30min timeout, 50-turn history
- **CLI**: `sovyx init/start/stop/status/doctor/brain/mind` commands (typer + rich)
- **Daemon**: JSON-RPC 2.0 over Unix socket (0o600 permissions)
- **Lifecycle Manager**: PID lock, SIGTERM/SIGINT graceful shutdown, sd_notify
- **Health Checker**: 10 concurrent checks (sqlite, brain, LLM, disk, memory)
- **Graceful Degradation**: Fallback chains for each component
- **Service Registry**: Lightweight DI with singleton factories
- **Event Bus**: Typed pub/sub for system events
- **Structured Logging**: structlog with JSON output
- **Docker**: Multi-stage build, non-root user, healthcheck
- **systemd**: Unit file with security hardening
- **Installer**: One-liner shell script via uv

### Technical
- 1138 tests (1130 passed, 8 skipped)
- 95%+ code coverage
- mypy strict mode (zero errors)
- ruff lint + format (zero warnings)
- bandit security scan (zero high/critical)
- Python 3.11 + 3.12 CI matrix
- Property-based tests (Hypothesis) for core algorithms
- Performance benchmarks with threshold validation

[0.1.0]: https://github.com/sovyx-ai/sovyx/releases/tag/v0.1.0
