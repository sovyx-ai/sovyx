# Roadmap

This is a rolling roadmap for Sovyx. Dates are best-effort and may shift; the
scope of each version is stable and only widens with explicit notes in release
announcements.

The current stable line is **v0.16**. The next major cut is **v0.6 — "The Mind
That Connects"**, followed by **v1.0 — "The Mind That Remembers"**.

---

## v0.11–v0.16 — current

The v0.11–v0.16 line shipped the enterprise-hardening pass, streaming
LLM-to-voice, 10-provider routing, and closed every major spec
divergence. Key releases:

- **DREAM phase** (v0.11.6) — nightly pattern discovery from recent
  episodes, derived concepts at low confidence, cross-episode Hebbian.
- **Interactive CLI REPL** (v0.11.7) — `sovyx chat` over the existing
  Unix-socket RPC, with prompt_toolkit history and slash commands.
- **Home Assistant integration** (v0.11.8) — first-party plugin, 4
  domains, 8 tools, REST-only.
- **CalDAV integration** (v0.11.9) — first-party plugin, 6 read-only
  tools, supports Nextcloud / iCloud / Fastmail / Radicale / SOGo /
  Baikal (Google discontinued CalDAV in 2023).
- **Conversation importers** (v0.11.4–5, v0.12.0) — ChatGPT, Claude,
  Gemini, Grok, plus Obsidian vault import.
- **PAD 3D emotional model** (v0.12.1) — concepts + episodes carry
  pleasure, arousal, dominance. ADR-001 closed.
- **LLM streaming** (v0.13.0) — token-level streaming from router to
  voice pipeline for ~300 ms perceived latency.
- **6 new LLM providers** (v0.13.1) — xAI, DeepSeek, Mistral,
  Together AI, Groq, Fireworks via shared base class.
- **First-run onboarding wizard + SSE chat** (v0.14.0) — `/api/onboarding/*`
  step machine and `POST /api/chat/stream` for token-level dashboard chat.
- **Emotion telemetry + plugin setup flow** (v0.15.x) — `/api/emotions/*`
  current state / timeline / triggers / distribution and the `/api/setup/*`
  install-time wizard separate from `/api/plugins/*` runtime control.
- **Voice device test + Windows parity** (v0.16.0–v0.16.11) — meter
  WebSocket, TTS playback jobs, Kokoro auto-download (CPU-pinned),
  audio device dropdowns, TCP RPC on Windows, psutil-based health.

This set of files reflects **v0.16.11**.

Release criteria:

- Test coverage >= 95% (~8,500 tests: ~7,700 pytest, ~767 vitest).
- Zero `ruff` errors, zero `mypy --strict` errors, zero `bandit` HIGH.
- Multi-arch Docker build (`linux/amd64`, `linux/arm64`) green.
- Dashboard `npx tsc -b` with zero errors.
- Full pytest + vitest suite passing.

---

## v0.6 — "The Mind That Connects"

v0.6 is the connectivity cut. The theme is moving Sovyx out of the single
laptop and into the real communication surfaces people use every day. It also
pays down the main architectural divergences accumulated during v0.5.

### Planned — bridges and channels

- **Audio relay over WebSocket.** A relay client with Opus encoding, a 60 ms
  audio ring buffer, 16 kHz ↔ 48 kHz resampling, an offline queue, and
  exponential backoff on reconnect. Unlocks the mobile companion.

### Already shipped in v0.11 (was originally v0.6 scope)

- **Home Assistant** — shipped as a plugin in v0.11.8 (4 domains, 8 tools,
  REST). Architectural change from the original spec: HA is a device API,
  not a conversational channel, so it lives in `plugins/` rather than
  `bridge/`. WebSocket subscriptions for real-time state push remain a
  next-PR follow-up.
- **CalDAV** — shipped as a plugin in v0.11.9 (6 read-only tools). Same
  architectural rationale as HA. Incremental sync via `ctag` + `etag` is
  deferred to a follow-up; the v0 plugin reissues a full REPORT per
  refresh window (cheap enough — ~50 KB per request).

### Already shipped in v0.12 (was originally v0.6 scope)

- **Obsidian vault importer** — shipped in v0.12.0. Reads Markdown with
  YAML frontmatter, wiki links, nested tags. Two-pass resolution for
  forward references. Lives in `sovyx.upgrade.vault_import`.
- **Grok importer** — shipped in v0.12.0. Fifth conversation platform.
- **SMF exporter** — shipped earlier. Complete Sovyx Mind Format export
  for GDPR Art. 20 data portability.

### Planned — voice

- **Speaker recognition.** ECAPA-TDNN biometrics with enrollment and
  verification. Enables multi-user voice interactions.
- **Voice cloning.** Speaker adaptation as a premium feature.

### Already shipped in v0.12–v0.13 (was originally v0.6 scope)

- **PAD 3D emotional model** — shipped in v0.12.1 (ADR-001 closed).
  Concepts + episodes carry valence, arousal, dominance (all [-1,+1]).
  Migration 006 on brain.db, no LLM backfill. Importance scorer uses
  sub-weights 0.45/0.30/0.25.
- **Configurable emotional baseline** — `EmotionalBaselineConfig` in
  `MindConfig` with homeostasis_rate. Present since v0.12.0.
- **LLM streaming → TTS** — shipped in v0.13.0. Token-level streaming
  from all 10 providers through `VoiceCognitiveBridge` to the voice
  pipeline's `stream_text()`. ~300 ms perceived latency.
- **6 new LLM providers** — shipped in v0.13.1. xAI (Grok), DeepSeek,
  Mistral, Together AI, Groq, Fireworks. Shared `OpenAICompatibleProvider`
  base class.
- **Consolidation** — `ConsolidationScheduler` runs every 6 h as a
  background job. Wired in bootstrap.

### Already shipped in v0.11 (was originally v0.6 scope)

- **Dream phase** — shipped in v0.11.6 as `brain/dream.py`. Time-of-day
  scheduler (default `02:00` in the mind's timezone), 1 LLM call per run
  extracts up to 5 recurring themes from the last 24 h of episodes,
  derives concepts at low confidence, attenuated cross-episode Hebbian
  on co-occurring concepts. Kill-switch via `dream_max_patterns: 0`.

### Planned — tooling

- **BYOK token isolation.** Per-user API-key routing at the LLM layer.

---

## v1.0 — "The Mind That Remembers"

v1.0 is the general-availability cut. The theme is stability, a published
plugin marketplace, and the deeper sandbox.

### Planned — sandbox hardening

- **Plugin sandbox v2.** A kernel-level enforcement layer on top of the
  existing five: seccomp-BPF on Linux, Linux namespaces (pid, net, mount),
  macOS Seatbelt, and Windows AppContainer tracked as an experiment.
- **Subprocess IPC for plugins.** Move plugins from in-process execution to
  isolated subprocesses with a JSON-RPC pipe.
- **Zero-downtime plugin update and rollback.** Blue-green atomic switch
  with automatic rollback on a post-upgrade health-check failure.

### Planned — platform

- **Optional Redis caching.** For multi-instance deployments and
  high-throughput workloads. Not required for single-user setups.
- **Full GDPR compliance suite.** Depends on the importers and exporter
  from v0.6 and on an audit-trail hardening pass.
- **Enterprise identity.** SSO (SAML 2.0, OIDC, LDAP), SCIM provisioning,
  and audit log.
- **Multi-mind daemon.** Multiple minds running under the same daemon with
  strong isolation boundaries.

### Planned — ecosystem

- **Public plugin marketplace.** Discoverable, permissioned, with signed
  manifests. Monetization handled by Sovyx Cloud (separate commercial
  offering).
- **Third-party security audit.** External pen-test of the sandbox and
  the cognitive safety stack.
- **Foundation governance.** Dual-license structure with an independent
  foundation for long-term stewardship.

Release criteria for v1.0 extend the v0.5.x list with:

- Sandbox penetrated by an external security audit with no outstanding
  critical findings.
- At least fifty community plugins in the marketplace, or ten official
  plugins plus a working public SDK.
- Cloud SLA targets tracked in sovyx-cloud (separate repo).

---

## Quarterly timeline

Best effort with the current team size. Scope is the contract; dates are
signals.

| Quarter         | Milestone         | Main scope                                               |
| --------------- | ----------------- | -------------------------------------------------------- |
| **2026 Q2**     | v0.16.x (shipped) | Enterprise hardening, DREAM, REPL, HA, CalDAV, importers, PAD 3D, streaming, 10 providers, onboarding wizard, emotions API, voice device test, Windows parity |
| **2026 Q3**     | v0.6 preview      | Audio relay, speaker recognition                         |
| **2026 Q3–Q4** | v0.6 GA           | BYOK isolation, voice cloning                            |
| **2027 Q1**     | v1.0 preview      | Sandbox v2, subprocess IPC, multi-mind                   |
| **2027 Q1–Q2** | v1.0 GA           | Security audit, foundation, marketplace launch           |

---

## Design principles that will not change

These are load-bearing. They survive every roadmap review.

- **Local-first.** Default is local. Every core feature works offline. The
  cloud is always opt-in and never a requirement.
- **Bring your own key.** Users choose which LLM provider to use and supply
  their own credentials. Sovyx never forces a specific provider.
- **Open-core under AGPL-3.0.** The library, the daemon, the dashboard, and
  all official plugins are open source. Commercial services (hosted cloud,
  backups, marketplace) fund development without privatising the core.
- **Zero fake metrics.** We optimise for contributors, downloads, real users.
  Not stars, not vanity.
- **Underpromise, overdeliver.** The README reflects what already works.
  Roadmap items above are clearly marked as planned.
- **Multi-provider everywhere.** LLM, STT, TTS, bridge channels, storage.
  No vendor lock-in.
- **Ship iteratively.** Never a perpetual rewrite. Every week ends on a
  green `main`.
