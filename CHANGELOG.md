# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] — 2026-04-08

### Added
- Dashboard chat — `POST /api/chat` endpoint with `ChannelType.DASHBOARD`, full cognitive loop integration
- Chat page — `/chat` route with optimistic UI, auto-scroll, conversation continuity
- CLI `sovyx token` command — display dashboard authentication token
- Startup banner — prints dashboard URL and token on `sovyx start`
- Welcome banner — 3-step onboarding for fresh engines (choose model, set API key, start chatting)
- Channel status card — real-time indicator for configured channels via `/api/channels`
- Request ID middleware — `X-Request-Id` header on every request/response for tracing
- Dashboard build step in CI workflow
- E2E integration tests for full dashboard bootstrap + chat flow
- Attack testing suite — 74 security tests across 10 categories (XSS, token exposure, CSP, auth bypass, input sanitization, CORS, information disclosure, WebSocket, devtools)
- `publish.yml` workflow — tag-triggered PyPI release via OIDC trusted publishing
- Dashboard quickstart documentation (`docs/dashboard-quickstart.md`)
- Smoke test checklist for manual validation

### Fixed
- FastAPI version hardcoded as `"0.1.0"` — now reads from `__version__`
- Error detail leak in chat endpoint — `str(exc)` replaced with generic message
- `BridgeManager._mind_id` private access — exposed as `mind_id` property (returns `MindId`)
- 4 additional private attribute accesses (`SLF001`) resolved with public properties on `PersonalityEngine`, `CloudBackupService`, `MigrationRunner`, `DatabasePool`
- 9 unnecessary `type: ignore` suppressions eliminated (lambda keys, explicit casts, timezone union, intermediate typed variables)

### Changed
- Dashboard `package.json` version synced to `0.5.0` (was `0.0.0`)
- Token modal command updated from `cat ~/.sovyx/token` to `sovyx token`
- Dashboard static assets rebuilt (37 chunks, chat: 6.13kB / 2.26kB gzip)

### Technical
- 4,396 backend tests (pytest), 381 frontend tests (vitest)
- 98% coverage on `chat.py`, 95%+ on all modified files
- 28 remaining `type: ignore` — all audited and documented (optional deps, upstream stub limitations)
- Zero `SLF001` violations remaining
- CI green: ruff, mypy strict, bandit, pytest, dashboard build

## [0.5.0] — 2026-04-06

### Added
- Voice pipeline — wake word detection (Silero VAD), streaming STT (Moonshine), TTS (Piper, Kokoro), Home Assistant Wyoming protocol
- Dashboard — real-time web UI with brain visualization, conversations, logs, settings, system status, WebSocket live updates
- Cloud backup — zero-knowledge encrypted (Argon2id + AES-256-GCM) to Cloudflare R2, with Stripe billing and usage metering
- Signal integration — via signal-cli-rest-api
- Observability — SLO monitoring, Prometheus `/metrics` endpoint, structured logging, cost tracking
- Zero-downtime upgrades — blue-green pipeline with automatic rollback, schema migrations
- Performance benchmarks — hardware-tier budgets (Pi5, N100, GPU), baseline regression detection
- Plugin system — architecture ready (v1.0 feature)
- Security headers middleware — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- Token-based dashboard authentication with timing-safe comparison

### Technical
- 54 tasks completed across 4 development phases
- 4,246 tests at release
- Published to PyPI (`pip install sovyx`), Docker (`ghcr.io/sovyx-ai/sovyx`), GitHub Releases

## [0.1.0] — 2026-04-03

### Added
- Cognitive Loop — perception, attention, thinking, action, reflection (OODA)
- Brain system — concept/episode/relation storage with SQLite + sqlite-vec embeddings
- Working memory — activation-based with geometric decay
- Spreading activation — multi-hop concept retrieval
- Hebbian learning — co-occurrence strengthening
- Ebbinghaus decay — forgetting curve with rehearsal reinforcement
- Hybrid retrieval — RRF fusion of FTS5 text search + vector KNN
- Memory consolidation — scheduled decay and pruning cycles
- Personality engine — OCEAN model with 3-level descriptors
- Context assembly — token-budget-aware with Lost-in-Middle ordering (Liu et al. 2023)
- LLM router — multi-provider failover (Anthropic, OpenAI, Ollama) with circuit breaker
- Cost guard — per-conversation and daily budget limits
- Telegram channel — aiogram 3.x with exponential backoff reconnect
- Person resolver — auto-create identity on first contact
- Conversation tracker — 30-minute timeout, 50-turn history
- CLI — `sovyx init/start/stop/status/doctor/brain/mind` commands (typer + rich)
- Daemon — JSON-RPC 2.0 over Unix socket (0o600 permissions)
- Lifecycle manager — PID lock, SIGTERM/SIGINT graceful shutdown, sd_notify
- Health checker — 10 concurrent checks (SQLite, brain, LLM, disk, memory)
- Service registry — lightweight DI with singleton factories
- Event bus — typed pub/sub for system events
- Docker — multi-stage build, non-root user, healthcheck
- systemd unit file with security hardening

### Technical
- 1,138 tests (1,130 passed, 8 skipped)
- 95%+ code coverage
- mypy strict, ruff, bandit — zero errors
- Python 3.11 + 3.12 CI matrix
- Property-based tests (Hypothesis) for core algorithms

[0.5.1]: https://github.com/sovyx-ai/sovyx/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/sovyx-ai/sovyx/compare/v0.1.0...v0.5.0
[0.1.0]: https://github.com/sovyx-ai/sovyx/releases/tag/v0.1.0
